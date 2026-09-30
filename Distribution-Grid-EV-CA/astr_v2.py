"""
astr_v2.py -- the GOOD 2.x boundary for the ASTR2026 pipeline.

Replaces the `ev_charging_project` package, which GOOD 2.0 deleted. The
pipeline used four things from it: `config.NETWORK_KW`, `config.SOLVER_KW`,
`utils.prepare_graph` and `utils.solution_to_dict` (plus
`utils.extract_capex_expansion`, which 10_01 had already superseded with its
own broader `_extract_capex_decisions`). All four are reproduced here against
the v2 API.

Where the units are converted
-----------------------------
Scripts 08_* and 09_* still build in GOOD 1.x units -- watts, joules, $/J --
and this module converts once, at the point the graph is handed to the solver,
by calling upstream's own `good.migrate.from_v1()`. Converting here rather than
rewriting every builder has three advantages:

* `from_v1` is the tested converter, exercised by upstream's suite. Rewriting
  ~2,000 lines of builder arithmetic by hand would put a unit slip in every one
  of them, and a unit slip in this model is silent: it changes dispatch without
  raising anything.
* The substation layer is *nested inside* the WECC graph, so the meso tier and
  the base WEC_modified.json tier are converted by the same pass, in the same
  units, with no chance of the two tiers disagreeing.
* It brings upstream's data repairs for free: the 25-hour-day wind artifact
  (`repair_25_hour_days`), the trailing-colon profile keys (1,360 fixed), the
  unresolvable profile references (2,010 dropped) and hydro normalisation.

The cost is that results are NOT expected to reproduce the v1 numbers exactly.
`from_v1` reclassifies solar and wind from must-take `Load` to curtailable
`Producer` (2,188 assets, 55,168 MW), which hands the optimiser a curtailment
decision it did not previously have and changes the RPS numerator. Curtailment
and RPS behaviour should move materially; that is upstream's fix landing, not a
porting error.

What from_v1 does NOT cover
---------------------------
Three things in this pipeline sit outside upstream's converter, and
`to_v2()` handles them after it runs:

1. `transformer_capacity` is a *node* attribute. `from_v1` rewrites assets,
   lines and profiles but passes node attributes through untouched, so the
   substation transformer limits would arrive in watts and be read as MW --
   a 1e6 error that would make every limit non-binding. Converted here.
2. `from_v1._store` replaces every storage spec with its own EPA Platform v6
   assumptions (`battery_existing`: 4 h, 85% round trip). The ASTR batteries
   are sized by 08_13/08_14 with an explicit `--duration-h`, so that duration
   has to be put back or the sizing scripts would silently do nothing.
3. Nuclear is held at constant output. This was `prepare_graph`'s first job in
   v1 and has no equivalent upstream.
"""

from __future__ import annotations

import hashlib
import os
from copy import deepcopy

import numpy as np

# --------------------------------------------------------------------------- units

W_PER_MW = 1.0e6
J_PER_MWH = 3.6e9

# v1 reported node shortfall/wastage in W, so energy summed over hourly steps
# came out in J and every consumer divided by 3.6e12 for GWh. v2 reports MW, so
# the same sum is in MWh. Downstream code multiplies by this instead.
GWH_PER_ENERGY_UNIT = 1.0e-3   # MWh -> GWh
MT_PER_KG = 1.0e-9             # kg -> Mt

# Retained so the v1-unit builders in 08_*/09_* stay readable next to this file.
V1_GWH_PER_ENERGY_UNIT = 1.0 / 3.6e12   # J -> GWh


# ------------------------------------------------------------------- network setup

# Seasonal week windows (hour-of-year start). Mirrored in common.SEASONAL_WEEKS.
SEASONAL_WEEKS = [
    {"name": "march", "start_hour": 1416, "month": "March"},
    {"name": "june", "start_hour": 3624, "month": "June"},
    {"name": "september", "start_hour": 5832, "month": "September"},
    {"name": "december", "start_hour": 8016, "month": "December"},
]

CALIFORNIA_REGIONS = [
    "WEC_BANC", "WEC_CALN", "WEC_LADW", "WEC_SDGE", "WECC_IID", "WECC_SCE",
]

TRANSMISSION_EFFICIENCY = 0.97
BATTERY_DURATION_HOURS = 4
PUMP_HYDRO_DURATION_HOURS = 8

NUM_HOURS = 7 * 24

# In v2 units these are plain $/MWh, which is the whole point of the migration:
# v1 carried the same two numbers as 2.78e-6 and 2.78e-10 $/J, and the 13
# orders of magnitude between them and the right-hand sides is what made the
# 672 h LP need ScaleFlag tuning to solve at all.
#
# shortfall_cost: $10,000/MWh is GenX's default value of lost load and sits in
# the lower-middle of the $9,000-$45,000/MWh literature range. Still ~260x the
# priciest generator, so load is served whenever it physically can be.
#
# wastage_cost: curtailment is conventionally valued at $0/MWh, but a strictly
# positive value is needed as a tie-breaker (zero leaves flat directions that
# an interior-point method smears across), so $1/MWh -- far below any
# generator, so it breaks ties without steering dispatch. NOTE this overrides
# the v2 Network default of $10,000/MWh, which would price curtailment equal to
# lost load and is wrong for this model.
NETWORK_KW = {
    "verbose": True,
    "steps": (0, NUM_HOURS),
    "time_step": 1.0,            # hours
    "shortfall_capacity": np.inf,
    "shortfall_cost": 10_000.0,   # $/MWh
    "wastage_capacity": np.inf,
    "wastage_cost": 1.0,          # $/MWh
    # v1 amortized capex over a fixed 20-year period; v2 annualizes from each
    # asset's own `lifetime` at this rate. Only matters when capex is open.
    "discount_rate": 0.07,
}

_NODEFILE_DIR = os.path.abspath("./gurobi_nodefiles")

# `linopy.Model.solve` takes `solver_options` as **kwargs, so Gurobi parameters
# are passed flat alongside `io_api` and are forwarded to `Model.setParam` one
# by one -- an unrecognised name raises there rather than being ignored.
# `io_api="direct"` builds the model in gurobipy in-process, which skips writing
# and re-reading an LP file of this size.
#
# The v1 parameter set (Method=1, Presolve=2, NumericFocus=3, ScaleFlag=1,
# Aggregate=0) was tuned against the badly conditioned $/J formulation, where
# ScaleFlag=1 was what finally made the 672 h horizon solve at all -- 1,012 s
# against three failures at the 14,400 s limit. In MW/$/MWh the conditioning
# problem those settings worked around is largely gone, so carrying them over
# unexamined risks paying their cost without their benefit. Defaults here are
# deliberately close to Gurobi's own; re-run 10_09_solver_param_sweep.py on the
# four-week horizon before trusting any tuned set.
SOLVER_OPTIONS = {
    "OutputFlag": 1,
    "Method": 2,          # barrier; concurrent (-1) split threads and lost 55% at 672 h
    "Threads": 0,
    "NodefileStart": 0.5,
    "NodefileDir": _NODEFILE_DIR,
}

SOLVER_KW = {
    "solver": "gurobi",
    "tee": True,
    "options": {"io_api": "direct", **SOLVER_OPTIONS},
}


def solver_kw(overrides: dict | None = None, env_var: str = "ASTR_GUROBI_PARAMS") -> dict:
    """SOLVER_KW with *overrides* and then $ASTR_GUROBI_PARAMS applied.

    The environment variable takes a comma-separated ``Name=value`` list and is
    applied last, so a sweep can retune without editing the runner:

        ASTR_GUROBI_PARAMS="Method=1,ScaleFlag=1,Crossover=0"
    """
    kw = deepcopy(SOLVER_KW)
    opts = kw["options"]

    for key, value in (overrides or {}).items():
        opts[key] = value

    raw = os.environ.get(env_var, "").strip()

    if raw:
        for item in raw.split(","):
            if "=" not in item:
                continue
            name, _, value = item.partition("=")
            name, value = name.strip(), value.strip()
            try:
                opts[name] = int(value)
            except ValueError:
                try:
                    opts[name] = float(value)
                except ValueError:
                    opts[name] = value
        print(f"  {env_var} applied: {raw}")

    return kw


# --------------------------------------------------------------- graph conversion


def _convert_node_params(graph) -> int:
    """Convert node-level power attributes from W to MW.

    `from_v1` rewrites assets, lines and profiles only, so anything carried on
    the node itself survives in v1 units. `transformer_capacity` is the one
    this pipeline sets (09_02, from the published step-down bank ratings);
    `shortfall_capacity` and `wastage_capacity` are included because
    RegionParams accepts per-node overrides in the same units.
    """
    n = 0

    for _, node in graph._node.items():
        for key in ("transformer_capacity", "shortfall_capacity", "wastage_capacity"):
            value = node.get(key)

            if value is None or not np.isfinite(float(value)):
                continue

            node[key] = float(value) / W_PER_MW
            n += 1

    return n


def _profiles_snapshot(graph) -> dict:
    """Record every node's profiles verbatim, before `from_v1` reshapes them.

    `from_v1` assumes 8,760-hour input: it pads short profiles to a full year
    (`_pad`) and renormalises `*:hydro` keys over that padded year
    (`hydro_shape`). The ASTR runs are solved on a 168- or 672-hour slice, and
    the profiles reaching this point are already sliced, so both would operate on
    the wrong length -- the hydro one destructively, since the mean it divides by
    would be dominated by 8,592 copies of the final sliced hour.

    Since 10_01 normalises hydro over the true year before slicing, the arrays
    arriving here are already the shape upstream wants. Restoring them after the
    conversion keeps the horizon intact and leaves the normalisation to the one
    place that can see a full year.
    """
    return {
        handle: deepcopy(node.get("profiles") or {})
        for handle, node in graph._node.items()
        if node.get("profiles")
    }


def _restore_profiles(graph, snapshot: dict) -> int:
    n = 0

    for handle, profiles in snapshot.items():
        node = graph._node.get(handle)

        if node is None:
            continue

        node["profiles"] = profiles
        n += len(profiles)

    # Assets hold a *resolved* copy of their profile, not a reference to the
    # node dictionary, so the same restore has to reach them too.
    for handle, node in graph._node.items():
        profiles = snapshot.get(handle) or {}

        for asset in (node.get("assets") or {}).values():
            key = asset.get("_profile_key")

            if key is not None and key in profiles:
                asset["profile"] = profiles[key]
                asset.pop("_profile_key", None)

    return n


def _tag_profile_keys(graph) -> None:
    """Remember which profile key each asset referred to, before from_v1 resolves it.

    `from_v1` rewrites an asset's `profile` when the key had a stray trailing
    colon or could not be resolved at all, so the original string is the only way
    to match an asset back to its node's restored profile.
    """
    for _, node in graph._node.items():
        profiles = node.get("profiles") or {}

        for asset in (node.get("assets") or {}).values():
            reference = asset.get("profile")

            if not isinstance(reference, str):
                continue

            if reference in profiles:
                asset["_profile_key"] = reference

            elif reference.rstrip(":") in profiles:
                asset["_profile_key"] = reference.rstrip(":")


def _store_specs(graph) -> dict:
    """Record each Store's pre-migration duration and efficiency, in v2 units."""
    specs = {}

    for handle, node in graph._node.items():
        for asset_handle, asset in (node.get("assets") or {}).items():
            if asset.get("_class") != "Store":
                continue

            spec = {}
            duration_s = asset.get("storage_duration")

            if duration_s:
                spec["duration"] = float(duration_s) / 3600.0

            # v1 `efficiency` was round-trip; v2 splits it one-way per direction.
            efficiency = asset.get("efficiency")

            if efficiency:
                one_way = float(np.sqrt(float(efficiency)))
                spec["charge_efficiency"] = one_way
                spec["discharge_efficiency"] = one_way

            if spec:
                specs[(handle, asset_handle)] = spec

    return specs


def _restore_store_specs(graph, specs: dict) -> int:
    """Put back the durations `from_v1._store` replaced with its own assumptions.

    08_13/08_14 size each substation battery with an explicit `--duration-h`,
    and 09_02 writes it as `storage_duration`. Upstream's converter discards
    that and substitutes 4 h (`battery_existing`) or 4 h (`battery_new`), so
    without this the sizing flag would be silently inert.
    """
    n = 0

    for (node_handle, asset_handle), spec in specs.items():
        asset = ((graph._node.get(node_handle) or {}).get("assets") or {}).get(asset_handle)

        if asset is None:
            continue

        asset.update(spec)
        asset.pop("storage_duration", None)
        n += 1

    return n


def _merge_key(asset):
    """Everything the LP can distinguish two assets by.

    Two assets sharing this key are interchangeable in the optimisation: the
    objective sees the same per-MWh cost, the constraints see the same
    availability shape and the same bounds, and the emission accounting sees the
    same factor. Merging them therefore changes neither the feasible set nor the
    optimum. Anything that could make two units behave differently has to appear
    here, so the key is deliberately over-specified rather than under.
    """
    profile = asset.get("profile")

    if profile is None:
        shape = None
    elif isinstance(profile, str):
        shape = ("key", profile)
    else:
        arr = np.asarray(profile, dtype=float)
        # Hash the shape itself: two units can reference different profile keys
        # that hold identical series, and those are still interchangeable.
        shape = ("hash", arr.shape, hashlib.sha1(np.ascontiguousarray(arr)).hexdigest())

    return (
        asset.get("_class"),
        asset.get("type"),
        asset.get("fuel"),
        shape,
        _round(asset.get("operating_cost")),
        _round(asset.get("co2")),
        _round(asset.get("capacity_factor"), 1.0),
        _round(asset.get("min_output")),
        _round(asset.get("ramp_rate")),
        asset.get("energy_budget_window"),
        bool(asset.get("dispatchable")),
        _round(asset.get("capex_cost")),
        _round(asset.get("fom_cost")),
        _round(asset.get("lifetime")),
        _round(asset.get("duration")),
        _round(asset.get("charge_efficiency")),
        _round(asset.get("discharge_efficiency")),
        _round(asset.get("efficiency")),
        _round(asset.get("capacity_credit")),
        asset.get("jurisdiction"),
        bool(asset.get("renewable")),
        float(asset.get("capex_capacity") or 0.0) > 0,
    )


def _round(value, default=None):
    if value is None:
        return default
    try:
        return round(float(value), 12)
    except (TypeError, ValueError):
        return value


# Capacity-like fields that add when two interchangeable assets are merged.
_ADDITIVE = ("installed_capacity", "capex_capacity", "installed_energy")


def aggregate_region_assets(graph, regions, quiet: bool = False) -> int:
    """Merge indistinguishable assets within each named region. Exact, not lossy.

    Why this exists
    ---------------
    GOOD 2.x builds its region energy balance as one dense ``(region, step, term)``
    array, padded on the third axis to the worst-connected node. WECC_PNW, a
    copper-plate balancing area *outside* California, carries 918 individual
    assets, so every one of the 3,120 nodes is padded to 928 terms while the median
    node has 8 -- 0.9% utilisation, 7.78 GB, and the six-balancing-area California
    model dies allocating it. The padding is set by a node that is not part of the
    California network at all.

    Why it is exact
    ---------------
    This model has no unit commitment: no minimum up or down time, no start-up
    cost, no integer variables. A producer is a continuous variable bounded by
    ``capacity_factor x profile x installed_capacity`` with a linear per-MWh cost.
    Two producers agreeing on all of that are one producer of the summed capacity,
    exactly -- the feasible set and the objective are unchanged, and because the
    emission factor is part of the key, so is the CO2 accounting.

    That is why no approximation reference is needed. The clustering literature
    (Palmintier & Webster 2014, IEEE Trans. Power Systems 29(3), on heterogeneous
    unit clustering) addresses the *lossy* problem of merging units with different
    costs and commitment constraints. Here there is nothing to trade away.

    Measured: WECC_PNW 918 -> 257 groups, all non-California regions 3,130 -> 1,079,
    dense block 7.78 GB -> 2.16 GB.

    The one thing lost is reporting resolution: per-unit output for merged assets is
    no longer separable. Aggregates by fuel, region and emission factor are intact,
    which is what every consumer in this pipeline uses.
    """
    merged = 0

    for handle in regions:
        node = graph._node.get(handle)

        if node is None:
            continue

        assets = node.get("assets") or {}
        groups: dict = {}

        for name, asset in assets.items():
            groups.setdefault(_merge_key(asset), []).append((name, asset))

        if len(groups) == len(assets):
            continue

        out = {}

        for members in groups.values():
            name, first = members[0]

            if len(members) == 1:
                out[name] = first
                continue

            combined = deepcopy(first)

            for field in _ADDITIVE:
                total = sum(float(a.get(field) or 0.0) for _, a in members)

                if any(a.get(field) is not None for _, a in members):
                    combined[field] = total

            # inf capex_capacity must survive summation as inf, not a large float
            if any(not np.isfinite(float(a.get("capex_capacity") or 0.0)) for _, a in members):
                combined["capex_capacity"] = np.inf

            combined["merged_from"] = len(members)
            combined["name"] = f"{name}__x{len(members)}"
            out[f"{name}__x{len(members)}"] = combined
            merged += len(members) - 1

        node["assets"] = out

        if not quiet:
            print(f"    {handle}: {len(assets):,} assets -> {len(out):,} groups")

    return merged


def _nuclear_baseload(graph) -> int:
    """Hold nuclear at constant output.

    `prepare_graph`'s first job in v1, with no upstream equivalent. Nuclear
    units in WEC_modified.json carry an hourly profile that dips; the fleet is
    modelled as must-run baseload, so the profile is dropped and the unit is
    made non-dispatchable at capacity_factor 1.
    """
    n = 0

    for _, node in graph._node.items():
        for asset in (node.get("assets") or {}).values():
            if asset.get("_class") != "Producer":
                continue

            if str(asset.get("fuel", "")).lower() != "nuclear":
                continue

            asset["profile"] = None
            asset["capacity_factor"] = 1.0
            asset["dispatchable"] = False
            n += 1

    return n


def _transmission_efficiency(graph, efficiency: float) -> int:
    """Set a uniform line efficiency.

    `from_v1._line` only fills `efficiency` in when the line does not already
    carry one, using EPA's 2.8% WECC inter-regional loss. The meso corridors
    set 0.97 explicitly in 09_02 and keep it; this makes the BA tier match
    rather than mixing two loss conventions in one graph.
    """
    n = 0

    for source in graph._adj:
        for _, edge in graph._adj[source].items():
            for line in (edge.get("lines") or {}).values():
                line["efficiency"] = float(efficiency)
                n += 1

    return n


def graph_from_nlg(nlg):
    """Build a NetworkX graph from node-link data under either edge key.

    `good.graph.graph_from_nlg` hardcodes ``edges="links"``, but Examples/
    WEC_modified.json and everything 09_02 writes store edges under ``"edges"``
    -- which worked in v1 only because that call passed no key at all and
    NetworkX 3.4+ defaults to ``"edges"``. Reading the key off the data keeps
    both conventions loadable without rewriting the graph files.
    """
    import networkx as nx

    key = "links" if "links" in nlg else "edges"

    try:
        return nx.node_link_graph(nlg, edges=key)

    except TypeError:  # NetworkX < 3.4 has no "edges" keyword
        return nx.node_link_graph(nlg)


def to_v2(nlg, *, nuclear_baseload: bool = True, transmission_efficiency: float | None = None,
          aggregate_regions=None, quiet: bool = False):
    """Convert an ASTR node-link graph (v1 units) into a GOOD 2.x NetworkX graph.

    `nlg` is the dictionary 09_02_nest_meso_in_good.py writes. Returns a graph
    ready for `good.Network(...).from_graph(graph, policies)`.
    """
    from good import migrate

    graph = graph_from_nlg(nlg) if isinstance(nlg, dict) else deepcopy(nlg)

    if not quiet:
        print(f"  Converting to v2 units: {graph.number_of_nodes()} nodes, "
              f"{graph.number_of_edges()} edges")

    specs = _store_specs(graph)
    snapshot = _profiles_snapshot(graph)
    _tag_profile_keys(graph)
    graph = migrate.from_v1(graph)

    n_profiles = _restore_profiles(graph, snapshot)
    n_nodes = _convert_node_params(graph)
    n_stores = _restore_store_specs(graph, specs)
    n_nuclear = _nuclear_baseload(graph) if nuclear_baseload else 0
    n_merged = 0

    if aggregate_regions:
        if not quiet:
            print("    merging indistinguishable assets (exact; see aggregate_region_assets):")
        n_merged = aggregate_region_assets(graph, aggregate_regions, quiet=quiet)
    n_lines = (_transmission_efficiency(graph, transmission_efficiency)
               if transmission_efficiency is not None else 0)

    if not quiet:
        notes = graph.graph.get("migration_notes", {})

        for key in sorted(notes):
            print(f"    migrate: {key} = {notes[key]:,}")

        print(f"    profiles kept at horizon    : {n_profiles:,}")

        if aggregate_regions:
            print(f"    assets merged (exact)       : {n_merged:,}")
        print(f"    node power attributes W->MW : {n_nodes:,}")
        print(f"    store specs restored        : {n_stores:,}")

        if nuclear_baseload:
            print(f"    nuclear held at baseload    : {n_nuclear:,}")

        if transmission_efficiency is not None:
            print(f"    line efficiency = {transmission_efficiency}   : {n_lines:,}")

    return graph


# -------------------------------------------------------------------- solution I/O


def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]

    if isinstance(obj, np.ndarray):
        return obj.tolist()

    if isinstance(obj, (np.integer,)):
        return int(obj)

    if isinstance(obj, (np.floating,)):
        value = float(obj)
        return value if np.isfinite(value) else None

    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None

    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj

    return str(obj)


def solution_to_dict(solution) -> dict:
    """Serialize a v2 `solution_graph()` to a JSON-safe dictionary.

    Edges are a list carrying explicit source and target so the graph can be
    rebuilt without relying on dictionary key shapes, matching what the v1
    helper of the same name produced.
    """
    return {
        "graph": _json_safe(dict(solution.graph)),
        "nodes": {str(handle): _json_safe(data) for handle, data in solution.nodes(data=True)},
        "edges": [
            {"source": str(source), "target": str(target), **_json_safe(data)}
            for source, target, data in solution.edges(data=True)
        ],
    }


def extract_capex_expansion(solution, graph=None) -> dict:
    """New capacity built by each asset, in MW, keyed by `(node, asset_handle)`.

    This is the *increment* above whatever installed base the scenario was
    handed, matching the v1 helper of the same name and the one caller that
    wants it: `_apply_capex_floor_nlg`, which adds the increment onto the
    installed base of a freshly built graph.

    The two-stage path needs the opposite -- an absolute capacity that means the
    same number no matter which baseline produced it -- and gets it from
    `_extract_capex_decisions` in 10_01, which adds the installed base itself.
    Keeping the two apart matters: a delta replayed as an absolute silently
    drops the floored-in capacity, which is how S0 once recorded 7,220 MW of
    storage against S1's 1,261 MW while S1 served strictly more load.

    `graph` is accepted and ignored, so the v1 call signature still works.
    """
    out = {}

    for handle, node in solution.nodes(data=True):
        for asset_handle, asset in (node.get("assets") or {}).items():
            new = asset.get("new_capacity", asset.get("capex"))

            if not new:
                continue

            built = float(np.asarray(new, dtype=float).reshape(-1)[0])

            if built > 0:
                out[(handle, asset_handle)] = built

    return out
