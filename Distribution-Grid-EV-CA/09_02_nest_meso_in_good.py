"""
09_02_nest_meso_in_good.py

Nest the unclustered CA substation graph inside the WECC BA GOOD model.

Tier 1: non-CA balancing areas unchanged.
Tier 2: SUB_* nodes carry mapped CA generators, disaggregated base load, EV load,
        and (optionally) endogenous BESS. CA BA nodes keep optional renewable CAPEX
        and WECC interties; plant-level CA assets with coordinates are moved onto
        substations. Pipe-flow / NTC constraints on substation corridors.

Writes:
  data/meso/wecc_ca_nested_graph.json
  data/meso/scenario_capacity_scales.json
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

import common as C

REPO = C.REPO_ROOT


def make_ev_load_asset(hub_id: str, profile_kW: np.ndarray) -> dict:
    profile_W = np.asarray(profile_kW, dtype=float) * 1000.0
    peak = float(profile_W.max()) if profile_W.size else 0.0
    cap = max(peak, 1.0)
    profile = (profile_W / cap).tolist()
    return {
        "_class": "Load",
        "type": "load",
        "name": f"ev_load_{hub_id}",
        "installed_capacity": -cap,
        "profile": profile,
        "dispatchable": False,
        "extensible": False,
        "capex_capacity": 0,
        "capex_cost": 0,
    }


def make_base_load_asset(hub_id: str, profile_W: np.ndarray) -> dict:
    profile_W = np.asarray(profile_W, dtype=float)
    peak = float(np.abs(profile_W).max()) if profile_W.size else 0.0
    cap = max(peak, 1.0)
    profile = (profile_W / cap).tolist()
    return {
        "_class": "Load",
        "type": "load",
        "name": f"base_load_{hub_id}",
        "installed_capacity": -cap,
        "profile": profile,
        "dispatchable": False,
        "extensible": False,
        "capex_capacity": 0,
        "capex_cost": 0,
    }


def make_store_asset(hub_id: str, power_W: float, duration_h: float = 4.0) -> dict:
    return {
        "_class": "Store",
        "type": "battery",
        "fuel": "battery",
        "name": f"bess_{hub_id}",
        "installed_capacity": 0.0,
        "capex_capacity": power_W,
        "capex_cost": 2.1,
        "storage_duration": duration_h * 3600.0,
        "efficiency": 0.9,
        "dispatchable": True,
        "extensible": True,
    }


# Each corridor is modelled as two opposing directed arcs (Transmission holds a
# single NonNegativeReals flow variable, and applies `efficiency` only on the
# receiving side, so directional losses need two arcs). Nothing in an LP can
# forbid both arcs of a pair being active at once, so with operating_cost=0 a
# corridor pair is a perfectly flat direction in the objective: energy can be
# cycled A->B->A indefinitely, burning (1 - efficiency) on each pass, at zero
# cost. Measured consequence on the 672 h S0 model: 1,097 corridors carrying
# flow in *both* directions simultaneously, 293 TWh of gross flow against only
# 40 TWh of generation, and ~15 TWh of phantom generation under barrier-only
# solves (which inflated fossil dispatch 3.2x and CO2 2.5x vs a crossover
# solution -- the flat direction is exactly what an interior point smears
# across).
#
# A strictly positive per-unit flow cost removes the flatness: cancelling the
# common part of any bidirectional pair strictly lowers cost *and* frees the
# round-trip losses at both ends, so loop flow can never be optimal. The base
# WECC graph achieves the same thing by being lossless (efficiency=1) and
# charging 2.222e-9 $/J (~$8/MWh) on most BA lines.
#
# Published wheeling charges are ~$2/MWh, but those price a whole
# point-to-point transaction, not each line it crosses. The BA tier can charge
# transaction-scale rates ($8/MWh on 40 of 68 lines) because BA paths are a
# few hops; meso paths cross many of ~5,200 substation corridors, so applying a
# transaction-scale rate per hop would dominate generation costs and distort
# merit order. $0.36/MWh per corridor keeps a ~10-hop intra-CA path near
# $3.6/MWh -- the right order of magnitude for a transaction, and well below
# gas (~$38/MWh) -- while staying clear of solver tolerance.
# See docs/cost_calibration_methodology.tex.
_J_PER_MWH = 3.6e9
TRANSMISSION_OPERATING_COST = 0.36 / _J_PER_MWH  # 1e-10 $/J = $0.36/MWh per corridor


def make_transmission_line(source: str, target: str, capacity_W: float, handle: str) -> dict:
    return {
        "source": source,
        "target": target,
        "type": "line",
        "_class": "Transmission",
        "installed_capacity": float(capacity_W),
        "operating_cost": TRANSMISSION_OPERATING_COST,
        "efficiency": 0.97,
        "dispatchable": True,
        "extensible": False,
        "capex_limit": 0.0,
        "capex_capacity": 0.0,
        "capex_cost": 0.0,
        "name": handle,
    }


def _load_network() -> dict:
    C.require_file(C.CA_NETWORK_JSON, hint="Run 09_01_build_ca_meso_grid.py first.")
    with open(C.CA_NETWORK_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _restrict_network(network: dict, nested_bas: set[str]) -> dict:
    """Keep only substations, corridors, interfaces and assets inside nested_bas."""
    keep = {n["hub_id"] for n in (network.get("nodes") or [])
            if n.get("parent_ba") in nested_bas}
    out = dict(network)
    out["nodes"] = [n for n in (network.get("nodes") or []) if n["hub_id"] in keep]
    out["edges"] = [e for e in (network.get("edges") or [])
                    if e["source"] in keep and e["target"] in keep]
    out["ba_interfaces"] = [i for i in (network.get("ba_interfaces") or [])
                            if i["hub_id"] in keep]
    out["generators"] = [x for x in (network.get("generators") or [])
                         if x.get("hub_id") in keep]
    out["bess_candidates"] = [b for b in (network.get("bess_candidates") or [])
                              if b.get("hub_id") in keep]
    # Restriction cuts corridors that ran to substations in other BAs, which
    # can island parts of the kept network. Every component must still reach
    # its BA bus, or its demand becomes shortfall that reads as congestion.
    import networkx as nx

    gg = nx.Graph()
    gg.add_nodes_from(keep)
    gg.add_edges_from((e["source"], e["target"]) for e in out["edges"])
    have_iface = {i["hub_id"] for i in out["ba_interfaces"]}
    peak = {n["hub_id"]: float(n.get("total_peak_W") or 0.0) for n in out["nodes"]}
    ba_of = {n["hub_id"]: n.get("parent_ba") for n in out["nodes"]}

    added = 0
    # Components and their anchors are both ordered explicitly. `max()` over a
    # set breaks ties by iteration order, which for strings depends on the
    # per-process hash seed, so islanded components whose substations all share
    # the same peak -- typically all sitting on the 5 MW MIN_RATING_W floor --
    # got a different gateway substation in every process. Two runs of the same
    # scenario therefore built 3,090 arcs each but not the *same* 3,090: six
    # differed between S1 and S3. That silently moved a 5 MW synthetic feed from
    # one substation to another between scenarios, which is both a
    # reproducibility defect and enough to break any per-corridor join across
    # two runs. Tie-break on the handle so the choice is stable.
    for comp in sorted(nx.connected_components(gg), key=lambda c: sorted(c)[0]):
        if comp & have_iface:
            continue
        anchor = max(sorted(comp), key=lambda h: (peak.get(h, 0.0), h))
        cap = max(sum(peak.get(h, 0.0) for h in comp), 5e6)
        out["ba_interfaces"] = list(out["ba_interfaces"]) + [{
            "hub_id": anchor,
            "parent_ba": ba_of.get(anchor),
            "interface_capacity_W": cap,
            "n_substations": len(comp),
            "role": "restriction_gateway",
        }]
        added += 1

    print(f"  nesting only {sorted(nested_bas)}: {len(out['nodes'])} substations, "
          f"{len(out['edges'])} corridors, {len(out['ba_interfaces'])} interfaces"
          + (f" (+{added} gateways for components islanded by the restriction)" if added else ""))
    return out


def _strip_mapped_ca_assets(base: dict, generators: list[dict],
                            nested_bas: set[str] | None = None) -> dict:
    """Remove plant-level CA assets that were snapped to substations; drop BA base_load."""
    mapped = {(g["parent_ba"], g["handle"]) for g in generators if g.get("hub_id")}
    # Only a BA that is actually nested loses its aggregate load and its
    # mapped plants; a copper-plate BA must keep both.
    targets = set(nested_bas) if nested_bas else set(C.CALIFORNIA_REGIONS)
    g = deepcopy(base)
    for node in g["nodes"]:
        nid = node.get("id")
        if nid not in targets:
            continue
        assets = node.get("assets") or {}
        keep = {}
        for handle, asset in assets.items():
            if (nid, handle) in mapped:
                continue
            if asset.get("_class") == "Load" and str(asset.get("type", "")).lower() == "load":
                continue
            keep[handle] = asset
        node["assets"] = keep
    return g


def _load_transformer_ratings() -> dict[str, float]:
    """hub_id -> step-down rating in W, from 08_06_substation_ratings.py."""
    path = C.MESO_DIR / "substation_ratings.csv"
    if not path.is_file():
        print("  no substation_ratings.csv; substations run without a transformer limit")
        return {}
    df = pd.read_csv(path)
    out = {
        str(h): float(w)
        for h, w in zip(df["hub_id"], pd.to_numeric(df["rating_W"], errors="coerce"))
        if pd.notna(w) and w > 0
    }
    # Report every source separately. This used to print "published" vs
    # everything-else, which made the ICA ratings look like they had been
    # dropped the first time they were used: the count fell from 1,215
    # published to 589 purely because 626 nodes had been retagged "ica", while
    # the values themselves were applied correctly. Nothing here filters on
    # rating_source -- every row with a positive rating becomes a limit.
    if "rating_source" in df:
        counts = df.loc[pd.to_numeric(df["rating_W"], errors="coerce") > 0, "rating_source"]
        breakdown = ", ".join(f"{n:,} {src}" for src, n in counts.value_counts().items())
    else:
        breakdown = "source not recorded"
    print(f"  transformer limits on {len(out):,} substations ({breakdown})")
    return out


def _drop_internal_ca_edges(g: dict, nested_bas: set[str] | None = None) -> dict:
    """Remove BA-to-BA edges whose two ends are both California BAs.

    The base WECC graph carries aggregate paths between the CA balancing
    areas (SCE<->CALN 3.0/3.7 GW, SCE<->LADWP 3.8 GW, SCE<->SDG&E 1.3 GW,
    BANC<->CALN 2.8 GW, IID<->SCE 0.6 GW, IID<->SDG&E 0.1 GW -- 12 directed
    edges). Once the substation network is nested inside those same BAs,
    every one of those corridors is represented twice: once as the detailed
    substation-level path and once as an uncongestible bulk edge between the
    BA buses. The aggregate copy is always the cheaper route, so power flows
    around the detailed network instead of through it and no modeled
    corridor can ever bind -- which is the whole quantity these scenarios
    exist to measure. Interfaces to BAs outside California are kept: there
    the BA node still legitimately represents an external system.
    """
    # An aggregate path is a duplicate only when the substation network
    # already carries it, i.e. when BOTH ends are nested. With a single BA
    # nested, every CA-internal path still connects to a copper plate and
    # must be kept.
    ca = set(nested_bas) if nested_bas else set(C.CALIFORNIA_REGIONS)
    kept, dropped = [], []
    for e in g.get("edges") or []:
        if e.get("source") in ca and e.get("target") in ca:
            dropped.append(e["id"])
        else:
            kept.append(e)
    g["edges"] = kept
    if dropped:
        print(f"  dropped {len(dropped)} CA-internal BA-to-BA edges (now carried by the substation network)")
    return g


def build_nested_graph(
    base: dict,
    network: dict,
    hub_ids: list[str],
    ev_kW: np.ndarray,
    total_kW: np.ndarray | None,
    include_bess: bool,
    include_ev: bool,
    capacity_scale_meso: float,
    capacity_scale_interface: float,
    capacity_scale_transformer: float = 1.0,
    bess_spec: dict | None = None,
    bess_top_n: int = 0,
    bess_hub_override: set[str] | None = None,
    only_ba: set[str] | None = None,
    line_spec: dict | None = None,
) -> dict:
    """``only_ba`` nests just those balancing areas at substation level.

    The rest of California stays a single copper-plate BA node, as the
    non-California WECC areas already are. This is a framework test rather
    than a reduced production run: it keeps the tier where our data is
    strongest (PG&E: 366 measured substation profiles, 689 of 900 published
    bank ratings) and collapses the tier where corridors are mostly inferred
    and 20 substations cannot be supplied at their own peak. It also cuts the
    LP from ~4.4M rows to ~0.9M, which is what makes S1 tractable after two
    crossover timeouts.

    The cost is real: copper-plating the rest removes inter-BA congestion, so
    P_cong from this configuration is a lower bound and a test of the
    mechanism, not a headline number.
    """
    generators = network.get("generators") or []
    ratings = _load_transformer_ratings()
    nested_bas = set(only_ba) if only_ba else set(C.CALIFORNIA_REGIONS)
    if only_ba:
        network = _restrict_network(network, nested_bas)
        generators = network.get("generators") or []
    g = _strip_mapped_ca_assets(base, generators, nested_bas)
    g = _drop_internal_ca_edges(g, nested_bas)
    existing_ids = {n["id"] for n in g["nodes"]}
    hub_index = {h: i for i, h in enumerate(hub_ids)}

    gen_by_hub: dict[str, list] = {}
    for rec in generators:
        hid = rec.get("hub_id")
        if not hid:
            continue
        gen_by_hub.setdefault(hid, []).append(rec)

    # Copy original CA assets so we can re-attach mapped plants onto substations
    orig_assets = {}
    orig_profiles = {}
    for node in base["nodes"]:
        if node.get("id") in C.CALIFORNIA_REGIONS:
            orig_assets[node["id"]] = node.get("assets") or {}
            orig_profiles[node["id"]] = node.get("profiles") or {}

    peaks = {
        hid: float(np.asarray(ev_kW[hub_index[hid]]).max()) if hid in hub_index else 0.0
        for hid in hub_ids
    }
    if bess_spec is not None:
        # Explicit per-node sizing (08_13), which replaces both the hub set and
        # the per-site rating. Deliberately NOT filtered to EV-positive hubs:
        # this fleet is sized against the deficit the LP leaves, and the worst
        # deficits are base-load delivery failures that may carry little or no
        # EV load. Filtering on EV peak is exactly what produced a fleet a
        # tenth the size of the deficit it was meant to cover.
        bess_hubs = {hid for hid in bess_spec if hid in hub_index}
        print(f"  BESS sized to deficit: {len(bess_hubs):,} nodes, "
              f"{sum(float(bess_spec[h].get('capex_capacity_W', 0)) for h in bess_hubs) / 1e6:,.0f} MW")
    elif bess_hub_override is not None:
        # Restrict to EV-positive hubs even when an explicit (e.g. congestion-
        # ranked) override is supplied, so BESS still lands where there's
        # local EV demand to smooth rather than on a zero-load pass-through.
        bess_hubs = {hid for hid in bess_hub_override if peaks.get(hid, 0.0) > 0}
    elif bess_top_n and bess_top_n > 0:
        bess_hubs = set(sorted(peaks, key=peaks.get, reverse=True)[:bess_top_n])
    else:
        bess_hubs = {hid for hid, p in peaks.items() if p > 0}

    bess_lookup = {b["hub_id"]: b for b in (network.get("bess_candidates") or [])}

    for rec in network.get("nodes") or []:
        hid = rec["hub_id"]
        if hid in existing_ids:
            continue
        i = hub_index.get(hid)
        assets = {}
        profiles = {}
        if i is not None:
            ev_prof = ev_kW[i] if include_ev else np.zeros(ev_kW.shape[1], dtype=float)
            if total_kW is not None:
                # total_kW = base + EV always (by construction in 08_03), so
                # the true non-EV baseline is total_kW - ev_kW regardless of
                # include_ev. Previously this only subtracted EV when
                # include_ev was True, so an "no EV" (include_ev=False) run
                # silently folded the EV load into base_load instead of
                # removing it, making the no-EV scenario serve identical
                # total demand to the with-EV scenario.
                base_kw = np.clip(np.asarray(total_kW[i], dtype=float) - np.asarray(ev_kW[i], dtype=float), 0, None)
            else:
                base_kw = np.zeros_like(ev_prof)
            assets[f"base_load_{hid}"] = make_base_load_asset(hid, base_kw * 1000.0)
            if include_ev:
                assets[f"ev_load_{hid}"] = make_ev_load_asset(hid, ev_prof)
            if include_bess and hid in bess_hubs:
                spec = (bess_spec or {}).get(hid) or bess_lookup.get(hid, {})
                peak_w = max(peaks.get(hid, 0.0) * 1000.0 * 0.5, 1e6)
                assets[f"bess_{hid}"] = make_store_asset(
                    hid, float(spec.get("capex_capacity_W") or peak_w), float(spec.get("duration_h") or 4.0)
                )
        ba = rec.get("parent_ba")
        profiles.update(orig_profiles.get(ba, {}))
        # Profiles have to follow the generator, not the substation. A relocated
        # plant references a profile key on its *own* BA node ("WECC_SCE:solar:"),
        # and a switchyard's parent BA is not always the BA the plant belongs to:
        # the BA assignment comes from the substation's location while the
        # generator carries its own from the WECC data, so SCE plants do land on
        # PG&E switchyards. Copying only `ba`'s profiles left 39 relocated VRE
        # plants (588 MW) referencing a key their node did not have. GOOD 2.x then
        # resolves a missing profile to a flat 1.0 for every hour, so those 39
        # solar plants ran at nameplate through the night -- 588 MW x 672 h =
        # 395 GWh of phantom renewable generation on the four-week run, counting
        # into the RPS numerator and displacing fossil.
        for grec in gen_by_hub.get(hid, []):
            gen_ba = grec.get("parent_ba")
            if gen_ba and gen_ba != ba:
                profiles.update(orig_profiles.get(gen_ba, {}))
            src = orig_assets.get(gen_ba, {}).get(grec["handle"])
            if not src:
                continue
            asset = deepcopy(src)
            handle = f"{grec['handle']}__{hid}"
            assets[handle] = asset
        node = {
            "id": hid,
            "_class": "Region",
            "assets": assets,
            "profiles": profiles,
        }
        rating = ratings.get(hid)
        if rating is not None and rating > 0:
            # Scaled separately from line capacity. S3 relaxes corridors only,
            # which left substation transformers binding and made part of its
            # residual shortfall irreducible: SUB_02201 shed an identical
            # 53.0/53.4/53.4/53.4 GWh across S0/S1/S2/S3, unmoved by 10x lines,
            # because the limit was the step-down bank rather than any corridor.
            # Scaling the two independently is what separates corridor-
            # attributable congestion from transformer-attributable congestion,
            # and they imply different investments.
            node["transformer_capacity"] = float(rating) * capacity_scale_transformer
        g["nodes"].append(node)
        existing_ids.add(hid)

    # Per-corridor target capacity from 08_15, keyed by line handle. It overrides
    # the blanket scale factor rather than multiplying it: the two answer different
    # questions and applying both would silently compound them.
    n_sized = 0
    sized_w = 0.0

    def _capacity(handle: str, scaled_w: float) -> float:
        nonlocal n_sized, sized_w
        if line_spec is None:
            return scaled_w
        target = line_spec.get(handle)
        if target is None:
            return scaled_w
        n_sized += 1
        sized_w += float(target)
        return float(target)

    for e in network.get("edges") or []:
        cap = float(e["installed_capacity_W"]) * capacity_scale_meso
        s, t = e["source"], e["target"]
        for src, tgt, tag in ((s, t, "fwd"), (t, s, "rev")):
            handle = f"meso_{src}_{tgt}_{tag}"
            cap_w = _capacity(handle, cap)
            g["edges"].append(
                {
                    # The Link id must differ from the line handle. GOOD 2.x keeps
                    # one global handle namespace across every component type, so
                    # naming the Link and its single Transmission identically is a
                    # collision ("handle is used twice") -- on a PG&E-only week
                    # that rejected all 3,022 of them at once. The base WECC graph
                    # already uses "source:target" for the Link, so match it.
                    "id": f"{src}:{tgt}",
                    "_class": "Link",
                    "source": src,
                    "target": tgt,
                    "lines": {handle: make_transmission_line(src, tgt, cap_w, handle)},
                }
            )

    for e in network.get("ba_interfaces") or []:
        hid, ba = e["hub_id"], e["parent_ba"]
        cap = float(e["interface_capacity_W"]) * capacity_scale_interface
        for src, tgt, tag in ((ba, hid, "to_hub"), (hid, ba, "to_ba")):
            handle = f"iface_{src}_{tgt}_{tag}"
            cap_w = _capacity(handle, cap)
            g["edges"].append(
                {
                    "id": f"{src}:{tgt}",   # distinct from the line handle; see above
                    "_class": "Link",
                    "source": src,
                    "target": tgt,
                    "lines": {handle: make_transmission_line(src, tgt, cap_w, handle)},
                }
            )

    if line_spec is not None:
        print(f"  transmission sized to observed overload: {n_sized:,} of "
              f"{len(g['edges']):,} arcs set explicitly, {sized_w / 1e6:,.0f} MW total")

    return g


def _default_profiles():
    june_ids = C.MESO_DIR / "seasonal" / "june" / "meso_hub_ids.npy"
    june_ev = C.MESO_DIR / "seasonal" / "june" / "meso_hourly_kW.npy"
    june_tot = C.MESO_DIR / "seasonal" / "june" / "meso_hourly_total_kW.npy"
    if not june_ids.is_file():
        raise FileNotFoundError("Missing seasonal meso loads; run 08_03 and 09_01 first.")
    hub_ids = [str(h) for h in np.load(june_ids, allow_pickle=True).tolist()]
    ev = np.load(june_ev)
    tot = np.load(june_tot) if june_tot.is_file() else None
    return hub_ids, ev, tot


def main(only_ba: list[str] | None = None, out_path=None) -> None:
    wec_path = C.resolve_wec_json()
    print(f"Loading base graph {wec_path}")
    with open(wec_path, encoding="utf-8") as fh:
        base = json.load(fh)

    network = _load_network()
    hub_ids, ev, tot = _default_profiles()

    scales = {
        "S0": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": False},
        "S1": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": True},
        "S2": {"meso": 1.0, "interface": 1.0, "bess": True, "ev": True},
        "S3": {"meso": 10.0, "interface": 10.0, "bess": False, "ev": True},
    }
    C.ensure_dir(C.MESO_DIR)
    with open(C.SCENARIO_SCALES_JSON, "w", encoding="utf-8") as fh:
        json.dump(scales, fh, indent=2)

    nested = build_nested_graph(
        base,
        network,
        hub_ids,
        ev,
        tot,
        include_bess=False,
        include_ev=True,
        capacity_scale_meso=1.0,
        capacity_scale_interface=1.0,
        only_ba=set(only_ba) if only_ba else None,
    )
    target = out_path or C.NESTED_GRAPH_JSON
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(nested, fh)
    print(
        f"Wrote {target} "
        f"(nodes={len(nested['nodes'])}, edges={len(nested['edges'])})"
    )


if __name__ == "__main__":
    import argparse

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--only-ba", nargs="*", default=None,
                     help="Nest only these BAs at substation level; the rest stay copper plates.")
    _ap.add_argument("--out", default=None, help="Write the graph here instead of the default.")
    _a = _ap.parse_args()
    main(only_ba=_a.only_ba, out_path=_a.out)
