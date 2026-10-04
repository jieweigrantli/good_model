"""
10_01_run_scenarios_S0_S3.py

S0–S3 nested GOOD solves.

Horizons
  four_week  (default)  concatenate 4 seasonal weeks (672 h) — CAPEX co-optimized
                        against all four weeks in one LP. Feasibility test.
  8760                  single-shot full-year LP. Do not run on 32 GB until
                        four_week succeeds; coded as the primary production path.
  weekly                four separate 168 h solves (legacy screening).

Gurobi: barrier (Method=2), NodefileStart, MPS I/O with duals disabled
(importing duals makes the solver interface build a {ConstrName: Pi} dict over
all ~14M constraints, which MemoryErrors at this model size).

Run from repository root. Does not launch the 8760 LP unless --horizon 8760.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO))

import astr_v2
import common as C
import importlib.util

_spec = importlib.util.spec_from_file_location("nest_meso", PKG / "09_02_nest_meso_in_good.py")
_nest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nest)


def _transform_nlg_profiles(nlg: dict, horizon: str, week: dict | None = None) -> dict:
    """Slice every profile to the requested horizon.

    Hydro is normalized to a mean-1 per-unit shape *before* slicing, over the
    full year. GOOD 2.x expects hydro as such a shape and `migrate.from_v1`
    produces one by calling `hydro_shape`, but that function pads to 8,760 hours
    by repeating the last value and then divides by the mean of all 8,760. Run
    after slicing it would take the mean over 168 real hours and 8,592 copies of
    the 168th, which is not the year's mean and would scale the hydro fleet by an
    arbitrary factor that changes with the chosen week. Normalising here, on the
    real year, gives the shape upstream intends; `astr_v2.to_v2` then keeps the
    sliced arrays out of `hydro_shape`'s way.
    """
    from good import migrate

    g = deepcopy(nlg)

    def _one(arr):
        if horizon == "8760":
            return C.pad_or_wrap_hours(arr, C.HOURS_YEAR).tolist()
        if horizon == "four_week":
            return C.concat_seasonal_weeks(arr).tolist()
        start = int((week or C.SEASONAL_WEEKS[0])["start_hour"])
        return C.slice_hours(arr, start, C.NUM_HOURS_WEEK).tolist()

    n_hydro = 0
    for node in g.get("nodes") or []:
        profiles = node.get("profiles") or {}
        for key, val in list(profiles.items()):
            if not isinstance(val, (list, np.ndarray)):
                continue
            if str(key).endswith(":hydro"):
                val = migrate.hydro_shape(val).tolist()
                n_hydro += 1
            profiles[key] = _one(val)
        for asset in (node.get("assets") or {}).values():
            prof = asset.get("profile")
            if isinstance(prof, (list, np.ndarray)):
                asset["profile"] = _one(prof)
    if n_hydro:
        print(f"  hydro profiles normalized over the full year before slicing: {n_hydro:,}")
    return g


def _hub_load_arrays(horizon: str, week: dict | None = None):
    """Return (hub_ids, ev_kW, total_kW) for the requested horizon."""
    if horizon == "8760":
        ids = np.load(C.MESO_DIR / "substation_ids.npy", allow_pickle=True).astype(str)
        ev = np.load(C.MESO_DIR / "substation_hourly_ev_kW_8760.npy")
        tot = np.load(C.MESO_DIR / "substation_hourly_total_kW_8760.npy")
        hub_ids = [f"SUB_{s}" for s in ids]
        return hub_ids, ev, tot

    weeks = [week] if (horizon == "weekly" and week is not None) else C.SEASONAL_WEEKS
    parts_ev, parts_tot, hub_ids = [], [], None
    for w in weeks:
        wdir = C.MESO_DIR / "seasonal" / w["name"]
        ids = [str(h) for h in np.load(wdir / "meso_hub_ids.npy", allow_pickle=True).tolist()]
        ev = np.load(wdir / "meso_hourly_kW.npy")
        tot_path = wdir / "meso_hourly_total_kW.npy"
        tot = np.load(tot_path) if tot_path.is_file() else ev
        if hub_ids is None:
            hub_ids = ids
        parts_ev.append(ev)
        parts_tot.append(tot)
    ev = np.concatenate(parts_ev, axis=1)
    tot = np.concatenate(parts_tot, axis=1)
    return hub_ids, ev, tot


def _num_hours(horizon: str) -> int:
    return C.horizon_hours("weekly" if horizon == "weekly" else horizon)


def _graph_fingerprint() -> str:
    """Short content hash of the nested graph, to tag results by model build."""
    import hashlib

    path = C.MESO_DIR / "wecc_ca_nested_graph.json"
    if not path.is_file():
        return "unknown"
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _solver_kw(horizon: str, scenario: str, log_path: Path | None = None, crossover: int = 0) -> dict:
    """Gurobi parameters for one solve, as linopy wants them.

    Most of the v1 tuning here was working around the $/J formulation, and does
    not carry over unexamined. That model's objective coefficients spanned ~13
    orders of magnitude (7e-10 to 1e3) with right-hand sides to 9e13, and the
    parameters below existed to keep Gurobi stable inside that range:

      * ``ScaleFlag=1`` was the single change that made the 672 h horizon
        solvable at all -- three failures at the 14,400 s limit under the
        aggressive ``ScaleFlag=2``, against ~1,150 s to certify under standard
        scaling, with the certified optimum 2% BELOW where the failed runs had
        crawled to. In MW/$/MWh the spread it was correcting is gone, so the
        default here is Gurobi's own choice (-1) and ScaleFlag is something to
        sweep, not something to pin.
      * ``OptimalityTol=1e-5`` and ``NumericFocus`` were loosened for the same
        reason: S1 and S3 hit the 2 h limit inside crossover, grinding from an
        already near-optimal point while dual infeasibility oscillated rather
        than fell. Left at Gurobi defaults now.
      * ``BarConvTol`` is kept overridable for the two-stage sizing pass, where
        stage 1 only needs capacity good to a few parts in ten thousand. Note
        loosening it made things WORSE once: at 1e-4 the barrier quit at
        iteration 87 with a primal residual of 6.70e6, Gurobi discarded the
        point, and crossover restarted from scratch. BarConvTol measures
        complementarity, not primal feasibility.

    Re-run 10_09_solver_param_sweep.py on the four-week horizon before pinning
    any of these again -- and sweep on the horizon that matters, since the
    concurrent method that won by 2.89x at one week lost 55% at four.

    **Do not read the objective as a convergence diagnostic.** Five certified-
    optimal solves of the identical PG&E March model returned objectives spanning
    0.214% ($572,238):

        Crossover=-1   44.8 s   2.675762e8   shortfall 14.333 GWh
        Crossover=1    44.0 s   2.676289e8             14.338
        Crossover=1    ~45  s   2.680771e8             14.383
        Crossover=1    43.7 s   2.681484e8             14.390
        Method=1       53.1 s   2.678213e8             14.358

    That is not a tolerance failure. The objective is dominated by unserved
    energy priced at a $10,000/MWh VOLL, and the 57.0 MWh of shortfall the five
    solves disagreed about is worth $570,000 by itself -- 100.4% of the observed
    objective spread. Total CO2 was identical across all five to the printed
    precision and curtailment agreed to within 0.2%.

    So the physical outputs are reproducible while the objective is not, and the
    quantities to compare between runs are CO2, shortfall GWh and curtailment
    GWh. An objective that moves a few tenths of a percent between runs means a
    few tens of MWh of shortfall moved, not that the solve is untrustworthy.
    Crossover=1 is also not reproducible run-to-run here (three runs, three
    objectives), which is why the default is Gurobi's own choice.
    """
    opts = {
        "OutputFlag": 1,
        # Barrier. Dual simplex (Method=1) stalled on the 672 h nested LP
        # (~8.6M rows) with dual infeasibility after an hour, and concurrent
        # (-1/3/5) splits threads across methods, which cost 55% at this size.
        "Method": 2,
        "Crossover": crossover,
        "BarHomogeneous": 1,
        "Presolve": 2,
        "NodefileStart": 0.5,
        "NodefileDir": os.path.abspath("./gurobi_nodefiles"),
        "BarConvTol": float(os.environ.get("ASTR_BARCONVTOL", 1e-8)),
    }

    if log_path is not None:
        opts["LogFile"] = str(log_path)

    opts["TimeLimit"] = (24 * 3600 if horizon == "8760"
                         else float(os.environ.get("ASTR_TIME_LIMIT_S", 4 * 3600)))

    # ASTR_GUROBI_PARAMS is a JSON object of raw Gurobi parameters applied last,
    # so it overrides anything set above. Parameters worth sweeping on this LP:
    #   Method=3/5   concurrent; only above ~8 spare cores, see the note above
    #   Sifting=2    for columns >> rows (presolved 1.20M x 4.38M, a 3.64x ratio)
    #   Aggregate=1  re-enable presolve aggregation
    #   ScaleFlag    0-3; now unpinned, so worth re-measuring in MW units
    extra = os.environ.get("ASTR_GUROBI_PARAMS")
    if extra:
        overrides = json.loads(extra)
        opts.update(overrides)
        print(f"  Gurobi param overrides: {overrides}")

    return {"solver": "gurobi", "tee": True, "options": {"io_api": "direct", **opts}}


# Where each asset's emission factor comes from, in order:
#
#   1. eGRID 2023, the plant's own CO2 output emission rate, read straight from
#      the raw file by ORIS code (common.egrid_plant_co2_kg_per_mwh).
#   2. For coal, gas and oil at a plant with no usable 2023 rate, the unit's heat
#      rate times the fuel's carbon content.
#   3. Otherwise the value published on the asset, and failing that the median
#      of the asset's fuel (see _emission_factors).
#
# The value on the asset is no longer first because it is wrong for every plant
# emitting 1,000 lb/MWh or more: the build that produced WEC_modified.json
# mis-read the thousands separators in the 2023 file, which lost the rate for 62
# of 63 coal units, 546 gas units and 112 oil units. Coal was being counted at
# 195 kg/MWh as dispatched. Read correctly it is 1,063, and gas is 465 where the
# asset values gave 368.
#
# The first attempt at this rebuilt coal alone from heat rates, before the cause
# was known. That landed close for coal (1,001) and left the gas and oil units
# with the same fault uncorrected.
#
# A plant rate is a plant average, and that is its limit. Every unit at a plant
# shares it, so a peaker beside a combined cycle, or gas units at a site that
# also reports solar, carry a rate their own heat rate does not support: 103 of
# 762 dispatched fossil units, 6% of fossil energy, sit outside 0.5 to 1.5 times
# what their heat rate implies. 10_11 reports a second set with those replaced
# by the heat-rate value, so the effect is visible as a range.
#
# The rule itself is common.emission_factor, shared with 10_11 so the two cannot
# drift apart. Fuels that burn nothing take zero whatever plant code they share.


def _generation_totals(solution_graph, graph=None, time_step: float = 1.0) -> pd.DataFrame:
    """Per-asset generation in MWh, with each asset's emission factor and fuel burn.

    Production comes back from the solution in MW, so energy is the hourly sum
    times the step length. ``fuel``, ``co2`` and ``heat_rate`` are input
    attributes and are NOT echoed into the v2 solution graph, so *graph* -- the
    built graph the solve ran on -- has to be passed to recover them.

    ``co2_kg_per_MWh`` is the factor the accounting uses and ``co2_source`` says
    where it came from. The eGRID plant rate and the value published on the asset
    are both kept beside it, with the ORIS code, heat rate and fuel burned, so any
    other set of factors can be applied after the solve.
    """
    egrid = C.egrid_plant_co2_kg_per_mwh()
    rows = []
    for node_name, node_data in solution_graph._node.items():
        source_assets = ((graph._node.get(node_name) or {}).get("assets") or {}) if graph is not None else {}
        for asset_name, asset_data in node_data.get("assets", {}).items():
            prod = asset_data.get("production", asset_data.get("net", None))
            source = source_assets.get(asset_name) or {}
            fuel = source.get("fuel", asset_data.get("fuel"))
            if prod is None or fuel is None:
                continue
            arr = np.asarray(prod, dtype=float).flatten()
            fuel = str(fuel).lower()
            energy = float(arr.sum() * time_step)
            published = pd.to_numeric(source.get("co2", asset_data.get("co2")), errors="coerce")
            # v2 heat rates are in Btu/kWh (good.migrate converts them from J/J).
            heat_rate = pd.to_numeric(source.get("heat_rate", asset_data.get("heat_rate")), errors="coerce")
            has_hr = bool(pd.notna(heat_rate) and heat_rate > 0)
            oris = C.oris_key(source.get("oris_code", asset_data.get("oris_code")))
            plant = egrid.get(oris) if oris is not None else None
            factor, origin = C.emission_factor(fuel, oris, heat_rate if has_hr else None, published)
            rows.append(
                {
                    "node": node_name,
                    "asset": asset_name,
                    "fuel": fuel,
                    "energy_MWh": energy,
                    "mean_MW": float(arr.mean()) if arr.size else 0.0,
                    # kg CO2 per MWh of electricity, as used. See _emissions_kg.
                    "co2_kg_per_MWh": factor,
                    "co2_source": origin,
                    "co2_kg_per_MWh_egrid2023": plant if plant is not None else np.nan,
                    "co2_kg_per_MWh_published": published,
                    "oris_code": oris,
                    "heat_rate_btu_per_kWh": float(heat_rate) if has_hr else np.nan,
                    "fuel_MMBtu": energy * float(heat_rate) / 1000.0 if has_hr else np.nan,
                    # What a portfolio standard is written on.
                    "jurisdiction": source.get("jurisdiction"),
                    "renewable": bool(source.get("renewable", False)),
                    "asset_class": source.get("_class"),
                    "asset_type": source.get("type"),
                    "hourly_MW": arr,
                }
            )
    return pd.DataFrame(rows)


def _emission_factors(gen_df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Per-row kg CO2/MWh as the accounting applies it, and which rows fell back.

    A row with no usable factor takes the median of its own fuel from the same
    table, so the fallback is internally consistent. ``ASTR_COAL_CO2`` (kg/MWh)
    overrides every coal row.
    """
    ef = pd.to_numeric(gen_df.get("co2_kg_per_MWh"), errors="coerce")
    positive = ef.fillna(0.0) > 0
    have = positive
    if "co2_source" in gen_df.columns:
        # a zero from a known source is a reported value, not a gap to fill
        have = have | (gen_df["co2_source"].isin(C.KNOWN_FACTOR_SOURCES) & ef.notna())
    # the median is taken over positive values only, as 10_11 does
    fuel_median = gen_df[positive].groupby("fuel")["co2_kg_per_MWh"].median() if positive.any() else pd.Series(dtype=float)
    fallback = gen_df["fuel"].map(fuel_median)
    used = ef.where(have, fallback).fillna(0.0)
    fell_back = (~have) & (fallback.fillna(0.0) > 0)
    coal_override = os.environ.get("ASTR_COAL_CO2")
    if coal_override:
        used = used.where(gen_df["fuel"] != "coal", float(coal_override))
    return used, fell_back


def _emissions_kg(gen_df: pd.DataFrame) -> float:
    """CO2 in kg, using each generator's OWN emission factor where it has one.

    WEC.json carries a per-asset ``co2`` in kg per unit of electricity, derived
    from eGRID; ``good.migrate.from_v1`` rescales it from kg/J to kg/MWh. It
    should be preferred over a fuel-level constant, because the fuel-level
    constants this function previously used were far too high and inflated every
    emission result in the study (kg/MWh):

        fuel          hardcoded      asset median   ratio
        natural gas     720.0          374.4        1.9x
        coal           1209.6          249.5        4.8x
        oil             972.0          117.0        8.3x
        biomass         334.8           14.9       22.5x

    The hardcoded gas figure of 720 kg/MWh implies a heat rate of ~4 and 25%
    thermal efficiency, below even an old steam turbine. The per-asset values
    give a capacity-weighted 376 kg/MWh for gas, which is what a combined-cycle
    fleet actually emits and what CAISO reports on the margin. Measured effect:
    the consequential EV emission factor falls from 771 g/kWh to roughly the
    mid-300s, and every absolute tonnage roughly halves.

    That correction traded one error for another. The asset values are wrong
    for every plant emitting 1,000 lb/MWh or more, which is all of coal, and it
    went unnoticed because coal barely moved between the scenarios then being
    compared. Factors now come from eGRID 2023 directly; see
    ``common.emission_factor``. Two things remain handled here rather than
    silently:

    * ``ASTR_COAL_CO2`` overrides every coal unit with one value in kg/MWh.
    * **Fossil producers with no usable factor** fall back to the median of
      their own fuel from the same table, not to the old constants, so the
      fallback is internally consistent.

    CH4 and N2O are also on the assets and are not counted here; this is CO2,
    not CO2-equivalent.
    """
    if gen_df.empty:
        return 0.0
    used, fell_back = _emission_factors(gen_df)
    missing_mwh = float(gen_df.loc[fell_back, "energy_MWh"].sum())
    if missing_mwh > 0:
        print(f"  note: {missing_mwh / 1e3:,.1f} GWh from generators with no usable "
              f"co2 factor; used their fuel's median from the same table")
    return float((gen_df["energy_MWh"] * used).sum())


# Balancing areas whose load is one state's. Washington and Oregon share
# WECC_PNW with part of Idaho, so they have no load of their own here and are
# left out of the load-basis table.
STATE_LOAD_BAS = {
    "CA": ("WECC_IID", "WECC_SCE", "WEC_BANC", "WEC_CALN", "WEC_LADW", "WEC_SDGE"),
    "AZ": ("WECC_AZ",),
    "CO": ("WECC_CO",),
    "MT": ("WECC_MT",),
    "NM": ("WECC_NM",),
    "NV": ("WECC_NNV", "WECC_SNV"),
}


def _rps_compliance(gen: pd.DataFrame, policies: dict, basis: str = "generation",
                    load_by_ba: pd.DataFrame | None = None) -> pd.DataFrame:
    """Each portfolio standard against what was dispatched.

    ``generation`` mirrors the filters in Examples/policies.json: the ratio is
    applied to everything generated by plants in the state, stores and plain
    loads left out. ``load`` applies it to the load served in the state, which
    is what the ratios mean in the ReEDS file they come from. Either way the
    renewable side is the output of the state's renewable plants, and
    ``shortfall_MWh`` is the renewable energy the state is short of its ratio.
    """
    rows = []
    if gen.empty or "jurisdiction" not in gen.columns:
        return pd.DataFrame(rows)
    g = gen[(gen["asset_class"] != "Store") & (gen["asset_type"] != "load")]
    served = load_by_ba.set_index("ba")["served_MWh"] if load_by_ba is not None and len(load_by_ba) else None
    for handle, pol in sorted(policies.items()):
        if pol.get("_class") != "Portfolio_Standard" or not handle.startswith("rps_"):
            continue
        state = handle[len("rps_"):]
        s = g[g["jurisdiction"] == state]
        if s.empty:
            continue
        ren = float(s.loc[s["renewable"], "energy_MWh"].sum())
        oth = float(s.loc[~s["renewable"], "energy_MWh"].sum())
        ratio = float(pol.get("ratio") or 0.0)
        if basis == "load":
            if served is None or state not in STATE_LOAD_BAS:
                continue
            base = float(sum(served.get(b, 0.0) for b in STATE_LOAD_BAS[state]))
        else:
            base = ren + oth
        short = max(0.0, ratio * base - ren)
        cost = float(pol.get("non_compliance_cost") or 0.0)
        rows.append({
            "state": state, "basis": basis, "ratio_required": ratio,
            "renewable_MWh": ren, "other_in_state_MWh": oth, "base_MWh": base,
            "share": ren / base if base > 0 else np.nan,
            "shortfall_MWh": short,
            "non_compliance_cost_per_MWh": cost,
            "non_compliance_cost_total": short * cost,
            # on the generation basis each MWh of in-state non-renewable output
            # raises the shortfall; on the load basis no plant does
            "adder_on_in_state_other_per_MWh": ratio * cost if (short > 1e-6 and basis == "generation") else 0.0,
        })
    return pd.DataFrame(rows)


_PARENT_BA: dict[str, str] | None = None


def _parent_ba(node: str) -> str:
    """Balancing area a node belongs to: its own id, or a substation's parent."""
    global _PARENT_BA
    if _PARENT_BA is None:
        _PARENT_BA = {}
        if C.CA_NETWORK_JSON.is_file():
            with open(C.CA_NETWORK_JSON, encoding="utf-8") as fh:
                _PARENT_BA = {n["hub_id"]: n.get("parent_ba") for n in json.load(fh).get("nodes") or []}
    return _PARENT_BA.get(node) or node


def _write_hourly_curves(scen_dir: Path, gen: pd.DataFrame, solution_graph, time_step: float = 1.0) -> dict:
    """Save what is needed to re-do the emission accounting without re-solving.

    The per-asset table gives totals. These give the shape: generation, fuel
    burned and CO2 by fuel for every hour, system-wide and by balancing area,
    plus the hourly energy balance. With them a different set of emission
    factors -- by fuel, by region, or by hour -- can be applied after the fact,
    and the difference between two scenarios is the consequential generation
    curve for whatever separates them.

      generation_hourly_by_fuel.csv     MW, hour x fuel
      heat_input_hourly_by_fuel.csv     MMBtu per hour, hour x fuel (units with a heat rate)
      co2_hourly_by_fuel.csv            kg per hour, hour x fuel, as this run accounts it
      generation_hourly_by_ba_fuel.csv.gz   long: hour, ba, fuel, generation_MW, heat_input_MMBtu_per_h
      balance_hourly.csv                MW: generation, wastage, line losses, served, shortfall
      balance.json                      the same as totals in MWh

    ``served`` is generation net of storage, less wastage and line losses: the
    energy that reached load. Scenarios that leave different amounts unserved
    are compared on it.
    """
    if gen.empty:
        return {}
    M = np.vstack(gen["hourly_MW"].to_numpy())
    H = M.shape[1]
    fuel = gen["fuel"].to_numpy()
    hr = pd.to_numeric(gen["heat_rate_btu_per_kWh"], errors="coerce").fillna(0.0).to_numpy() / 1000.0
    ef, _ = _emission_factors(gen)
    hours = pd.RangeIndex(H, name="hour")

    def by(keys, values) -> pd.DataFrame:
        return pd.DataFrame(values).groupby(keys).sum().T.set_index(hours)

    by(fuel, M).to_csv(scen_dir / "generation_hourly_by_fuel.csv", float_format="%.4f")
    heat = by(fuel, M * hr[:, None])
    heat.loc[:, (heat != 0).any()].to_csv(scen_dir / "heat_input_hourly_by_fuel.csv", float_format="%.3f")
    co2 = by(fuel, M * ef.to_numpy()[:, None])
    co2.loc[:, (co2 != 0).any()].to_csv(scen_dir / "co2_hourly_by_fuel.csv", float_format="%.2f")

    ba = np.array([_parent_ba(n) for n in gen["node"]])
    key = pd.MultiIndex.from_arrays([ba, fuel], names=["ba", "fuel"])
    g_long = pd.DataFrame(M, index=key).groupby(level=[0, 1]).sum().stack().rename("generation_MW")
    h_long = pd.DataFrame(M * hr[:, None], index=key).groupby(level=[0, 1]).sum().stack().rename("heat_input_MMBtu_per_h")
    long = pd.concat([g_long, h_long], axis=1).reset_index().rename(columns={"level_2": "hour"})
    long = long[(long["generation_MW"] != 0) | (long["heat_input_MMBtu_per_h"] != 0)]
    long[["hour", "ba", "fuel", "generation_MW", "heat_input_MMBtu_per_h"]].to_csv(
        scen_dir / "generation_hourly_by_ba_fuel.csv.gz", index=False, float_format="%.4f", compression="gzip")

    shortfall = np.zeros(H)
    wastage = np.zeros(H)
    for node in solution_graph._node.values():
        for name, acc in (("shortfall", shortfall), ("wastage", wastage)):
            v = np.asarray(node.get(name) or [], dtype=float).flatten()
            if v.size == H:
                acc += v
    losses = np.zeros(H)
    net_import: dict[str, float] = {}
    for s_name, adj in solution_graph._adj.items():
        for t_name, edge in adj.items():
            for line in (edge.get("lines") or {}).values():
                f = np.asarray(line.get("flow", line.get("transmission", [])), dtype=float).flatten()
                r = np.asarray(line.get("received", f), dtype=float).flatten()
                if f.size == H and r.size == H:
                    losses += f - r
                    net_import[t_name] = net_import.get(t_name, 0.0) + float(r.sum()) * time_step
                    net_import[s_name] = net_import.get(s_name, 0.0) - float(f.sum()) * time_step

    # Load served by balancing area: what was generated there, plus what came in
    # over lines, less what was dumped. Flows inside an area cancel.
    per_ba: dict[str, dict[str, float]] = {}

    def _acc(node: str, key: str, value: float) -> None:
        d = per_ba.setdefault(_parent_ba(node), {"generation_MWh": 0.0, "net_import_MWh": 0.0,
                                                 "wastage_MWh": 0.0, "shortfall_MWh": 0.0})
        d[key] += value

    for node, e in zip(gen["node"], gen["energy_MWh"]):
        _acc(node, "generation_MWh", float(e))
    for node, v in net_import.items():
        _acc(node, "net_import_MWh", v)
    for name, node in solution_graph._node.items():
        for key, field in (("wastage_MWh", "wastage"), ("shortfall_MWh", "shortfall")):
            v = np.asarray(node.get(field) or [], dtype=float).flatten()
            if v.size == H:
                _acc(name, key, float(v.sum()) * time_step)
    lb = pd.DataFrame([{"ba": k, **v} for k, v in sorted(per_ba.items())])
    lb["served_MWh"] = lb["generation_MWh"] + lb["net_import_MWh"] - lb["wastage_MWh"]
    lb["demand_MWh"] = lb["served_MWh"] + lb["shortfall_MWh"]
    lb.to_csv(scen_dir / "load_by_ba.csv", index=False)
    generation = M.sum(axis=0)
    served = generation - wastage - losses
    pd.DataFrame({"generation_MW": generation, "wastage_MW": wastage, "line_losses_MW": losses,
                  "served_MW": served, "shortfall_MW": shortfall}, index=hours).to_csv(
        scen_dir / "balance_hourly.csv", float_format="%.4f")
    totals = {
        "generation_MWh": float(generation.sum() * time_step),
        "wastage_MWh": float(wastage.sum() * time_step),
        "line_losses_MWh": float(losses.sum() * time_step),
        "served_MWh": float(served.sum() * time_step),
        "shortfall_MWh": float(shortfall.sum() * time_step),
    }
    (scen_dir / "balance.json").write_text(json.dumps(totals, indent=2), encoding="utf-8")
    totals["load_by_ba"] = lb
    return totals


def _line_records(solution_graph, graph=None) -> pd.DataFrame:
    """Per-line flow in MW and how many hours each line was at its rating.

    v2 names the flow variable ``flow`` (v1 called it ``transmission``) and does
    not echo ``installed_capacity`` into the solution, so the rating comes from
    *graph* -- the built graph the solve ran on. Without it every line looks
    unconstrained and ``binding_hours`` would be zero everywhere, which is the
    headline congestion diagnostic.
    """
    rows = []
    for src, adj in solution_graph._adj.items():
        for tgt, edge in adj.items():
            source_lines = ((graph._adj.get(src) or {}).get(tgt) or {}).get("lines") or {} if graph is not None else {}
            for handle, line in (edge.get("lines") or {}).items():
                flow = np.asarray(line.get("flow", line.get("transmission", [])), dtype=float).reshape(-1)
                cap = float((source_lines.get(handle) or {}).get("installed_capacity")
                            or line.get("installed_capacity") or 0.0)
                if flow.size == 0:
                    continue
                binding = (cap > 0) & (flow >= 0.99 * cap)
                rows.append(
                    {
                        "source": src,
                        "target": tgt,
                        "line": handle,
                        "capacity_MW": cap,
                        "mean_flow_MW": float(flow.mean()),
                        "peak_flow_MW": float(flow.max()),
                        "binding_hours": int(binding.sum()),
                        "n_hours": int(flow.size),
                    }
                )
    return pd.DataFrame(rows)


def _shortfall_by_node(solution_graph, time_step: float = 1.0) -> pd.DataFrame:
    """Per-node shortfall shape, for sizing storage against the actual deficit.

    A battery is bounded by two different things and the totals hide both.
    Its power rating has to cover the *peak* hourly deficit, and its energy
    rating only has to cover the deficit inside one contiguous run of short
    hours -- if the node recovers between runs the battery can recharge. So a
    node that is short 20 MW for six hours a night needs ~120 MWh, while one
    short 20 MW continuously for a week cannot be helped by storage at any
    size, because there is no surplus hour to charge from.

    That distinction is the whole question for S2: the prescribed fleet was
    sized at half the EV peak, which came to roughly a tenth of the deficit,
    and only 11 of 786 batteries discharged at all.

    v2's Region.solution() reports shortfall in MW per step, where v1 reported
    energy in J per step, so peak power is now the hourly maximum directly and
    energy is the sum times the step length.
    """
    rows = []
    for nid, node in solution_graph._node.items():
        sf = np.asarray(node.get("shortfall") or [], dtype=float)
        if not sf.size or sf.sum() <= 0:
            continue
        short = sf > 0
        # longest contiguous run of short hours, and its energy
        best_len = best_e = cur_len = cur_e = 0
        for j, s in zip(short, sf):
            if j:
                cur_len += 1
                cur_e += s
                if cur_e > best_e:
                    best_len, best_e = cur_len, cur_e
            else:
                cur_len = cur_e = 0
        rows.append({
            "node": nid,
            "shortfall_MWh": float(sf.sum()) * time_step,
            "shortfall_GWh": float(sf.sum()) * time_step / 1e3,
            "peak_MW": float(sf.max()),
            "hours_short": int(short.sum()),
            "longest_run_h": int(best_len),
            "longest_run_MWh": float(best_e) * time_step,
        })
    if not rows:
        return pd.DataFrame(columns=["node", "shortfall_MWh"])
    return pd.DataFrame(rows).sort_values("shortfall_MWh", ascending=False)


def _vre_curtailment(solution_graph, graph, time_step: float = 1.0):
    """Per-node hourly VRE curtailment in MW: what the fleet could have made, minus what it did.

    This has to be measured, not read off the node, because GOOD 2.x moved where
    curtailment lives. In v1 solar and wind were must-take ``Load`` assets, so
    surplus VRE had nowhere to go and appeared as node ``wastage``: energy pushed
    into the balance and dumped. ``migrate.from_v1`` reclassifies them as
    curtailable ``Producer`` assets (2,227 of them, ~55 GW), so the optimiser
    simply does not produce the surplus in the first place. Node wastage then
    goes to zero -- it did, exactly zero across all 962 nodes on the first v2
    week -- and reading it alone would report no curtailment at all in a system
    that is still spilling the same physical energy.

    Availability follows producer.py: ``capacity_factor * profile * installed_capacity``.
    Only assets that can actually be turned down are counted, so a must-run unit
    held at its profile contributes nothing here by construction.

    Two exclusions matter, and leaving either out inflates the number badly:

    * **Only solar and wind.** These are the assets `_vre` reclassified, and the
      population is exactly right: 2,188 units, 55,169 MW.
    * **Nothing with an `energy_budget_window`.** `from_v1` gives every hydro unit
      a 24-hour energy budget, so hydro routinely produces below its hourly
      availability -- it is holding water for later, not spilling it. Counting it
      swept in 1,360 units and 50,763 MW of hydro and reported 1,932 GWh of
      curtailment on a PG&E week where the real VRE figure is a fraction of that.
    """
    per_node: dict[str, np.ndarray] = {}

    for nid, node in solution_graph._node.items():
        source_assets = ((graph._node.get(nid) or {}).get("assets") or {})

        for aname, asset in (node.get("assets") or {}).items():
            source = source_assets.get(aname)

            if not source or source.get("_class") != "Producer":
                continue

            if not source.get("dispatchable") or source.get("energy_budget_window"):
                continue

            if str(source.get("type", "")).lower() not in ("solar", "wind"):
                continue

            profile = source.get("profile")

            if profile is None:
                continue

            prod = np.asarray(asset.get("production", []), dtype=float).reshape(-1)

            if prod.size == 0:
                continue

            avail = (np.asarray(profile, dtype=float).reshape(-1)[:prod.size]
                     * float(source.get("capacity_factor") or 1.0)
                     * float(source.get("installed_capacity") or 0.0))

            if avail.size != prod.size:
                continue

            spill = np.maximum(avail - prod, 0.0)

            if spill.sum() <= 0:
                continue

            if nid in per_node:
                per_node[nid] = per_node[nid] + spill
            else:
                per_node[nid] = spill

    return per_node


def _wastage_by_node(solution_graph, time_step: float = 1.0, curtailment: dict | None = None) -> pd.DataFrame:
    """Per-node curtailment shape, for sizing storage against spilled energy.

    The mirror of `_shortfall_by_node`, and needed because the batteries that
    actually cycled in S2 were at curtailment nodes rather than deficit ones:
    SUB_18295 (2,240 MW nuclear, 1,301 GWh spilled) and SUB_04314 (926 MW
    geothermal, 608 GWh). Storage at a deficit node cannot charge, because
    charging means importing through the path that is already capped; storage at
    a surplus node has energy available by definition.

    The symmetric question decides whether surplus-side storage works: a battery
    charging from curtailment has to *discharge* somewhere later, which needs an
    hour when the outbound corridor has slack. Curtailment happens precisely
    when that corridor is full, so if a node is spilling in nearly every hour
    there may be no discharge window either. `longest_run_h` is therefore read
    the opposite way here: a LONG curtailment run is bad for storage, because it
    means the corridor is never free.

    Spill is the sum of two things under GOOD 2.x: node ``wastage`` (surplus
    dumped out of the energy balance, which is now usually zero) and VRE
    curtailment (production held below availability). ``curtailment`` carries the
    second from `_vre_curtailment`. Adding them keeps this file measuring the same
    physical quantity it did in v1, which is what 08_14 sizes batteries against.
    """
    curtailment = curtailment or {}
    nodes = set(solution_graph._node) | set(curtailment)
    rows = []
    for nid in nodes:
        node = solution_graph._node.get(nid) or {}
        ws = np.asarray(node.get("wastage") or [], dtype=float)
        cu = np.asarray(curtailment.get(nid, []), dtype=float)
        if ws.size and cu.size:
            n = min(ws.size, cu.size)
            ws = ws[:n] + cu[:n]
        elif cu.size:
            ws = cu
        if not ws.size or ws.sum() <= 0:
            continue
        spill = ws > 0
        best_len = best_e = cur_len = cur_e = 0
        for j, s in zip(spill, ws):
            if j:
                cur_len += 1
                cur_e += s
                if cur_e > best_e:
                    best_len, best_e = cur_len, cur_e
            else:
                cur_len = cur_e = 0
        rows.append({
            "node": nid,
            "wastage_MWh": float(ws.sum()) * time_step,
            "wastage_GWh": float(ws.sum()) * time_step / 1e3,
            "peak_MW": float(ws.max()),
            "mean_MW": float(ws.mean()),
            "hours_spilling": int(spill.sum()),
            "hours_free": int((~spill).sum()),
            "longest_run_h": int(best_len),
            "longest_run_MWh": float(best_e) * time_step,
        })
    if not rows:
        return pd.DataFrame(columns=["node", "wastage_MWh"])
    return pd.DataFrame(rows).sort_values("wastage_MWh", ascending=False)


def _shortfall_wastage_totals(solution_graph, time_step: float = 1.0,
                              curtailment: dict | None = None) -> dict:
    """Sum node-level shortfall and spill energy (MWh) across the whole graph.

    Region.solution() puts 'shortfall'/'wastage' directly on each node dict
    (not under 'assets') as a per-hour list. In v2 these are MW, so energy is
    the sum times the step length; v1 reported per-step Joules and needed no
    multiplication.

    ``wastage_GWh`` is reported as total spill -- dumped surplus plus VRE
    curtailment -- and the two components are broken out beside it. They have to
    be added to stay comparable with the v1 results, where must-take solar and
    wind made all spill show up as dumped surplus. See `_vre_curtailment`.
    """
    curtailment = curtailment or {}
    shortfall_mwh = 0.0
    dumped_mwh = 0.0
    curtailed_mwh = 0.0
    top_shortfall = []
    top_wastage = []
    for nid in set(solution_graph._node) | set(curtailment):
        node = solution_graph._node.get(nid) or {}
        sf = np.asarray(node.get("shortfall") or [], dtype=float)
        ws = np.asarray(node.get("wastage") or [], dtype=float)
        cu = np.asarray(curtailment.get(nid, []), dtype=float)
        if sf.size:
            s = float(sf.sum()) * time_step
            shortfall_mwh += s
            if s > 0:
                top_shortfall.append((nid, s))
        dumped = float(ws.sum()) * time_step if ws.size else 0.0
        curtailed = float(cu.sum()) * time_step if cu.size else 0.0
        dumped_mwh += dumped
        curtailed_mwh += curtailed
        if dumped + curtailed > 0:
            top_wastage.append((nid, dumped + curtailed))
    top_shortfall.sort(key=lambda x: x[1], reverse=True)
    top_wastage.sort(key=lambda x: x[1], reverse=True)
    wastage_mwh = dumped_mwh + curtailed_mwh
    return {
        "shortfall_MWh": shortfall_mwh,
        "wastage_MWh": wastage_mwh,
        "shortfall_GWh": shortfall_mwh / 1e3,
        "wastage_GWh": wastage_mwh / 1e3,
        "dumped_surplus_GWh": dumped_mwh / 1e3,
        "vre_curtailment_GWh": curtailed_mwh / 1e3,
        "top_shortfall_nodes": top_shortfall[:10],
        "top_wastage_nodes": top_wastage[:10],
        "n_wastage_nodes": len(top_wastage),
    }


def _bess_records(solution_graph, graph=None, time_step: float = 1.0) -> pd.DataFrame:
    """Per-battery planned power, built power and energy discharged, in MW/MWh.

    v2 reports storage as separate ``charge``/``discharge`` series plus a ``net``,
    where v1 folded them into one signed ``production``, and names the built
    capacity ``new_capacity`` rather than ``capex``. Reading ``discharge``
    directly is more accurate than the old ``max(production, 0)``: that clipped
    the net series, so any hour with simultaneous charge and discharge
    understated the throughput that sets the cycle count.
    """
    rows = []
    for nid, node in solution_graph._node.items():
        source_assets = ((graph._node.get(nid) or {}).get("assets") or {}) if graph is not None else {}
        for aname, asset in (node.get("assets") or {}).items():
            if not str(aname).startswith("bess"):
                continue
            discharge = asset.get("discharge")
            if discharge is None:
                net = np.asarray(asset.get("net", asset.get("production", [0.0])), dtype=float).reshape(-1)
                discharge = np.maximum(net, 0.0)
            discharge = np.asarray(discharge, dtype=float).reshape(-1)
            charge = np.asarray(asset.get("charge", [0.0]), dtype=float).reshape(-1)
            new = asset.get("new_capacity", asset.get("capex", [0.0]))
            built = float(np.asarray(new, dtype=float).reshape(-1)[0]) if new is not None else 0.0
            source = source_assets.get(aname) or {}
            rows.append(
                {
                    "node": nid,
                    "planned_MW": float(source.get("capex_capacity") or asset.get("capex_capacity") or 0.0),
                    "installed_MW": float(source.get("installed_capacity") or 0.0),
                    "built_MW": built,
                    "discharge_MWh": float(discharge.sum() * time_step),
                    "charge_MWh": float(charge.sum() * time_step),
                }
            )
    return pd.DataFrame(rows)


def _solve_nlg(nlg, policies, network_kw, solver_kw, label: str):
    """Convert to v2 units, build, solve, and return (solution_graph, graph, objective).

    The built graph is returned alongside the solution because v2's solution
    graph carries results only: ``fuel``, ``co2`` and ``installed_capacity`` stay
    on the input, and the post-processing needs all three.
    """
    import good

    # Merge indistinguishable assets in the out-of-state copper plates. GOOD 2.x
    # builds the region energy balance as one dense (region, step, term) array padded
    # to the worst-connected node, and WECC_PNW holds 918 individual assets while the
    # median node has 8 -- 0.9% utilisation, 7.78 GB, and the six-balancing-area
    # California model dies allocating it. The merge is exact rather than lossy: with
    # no unit commitment in this model, assets sharing profile shape, per-MWh cost,
    # emission factor and every bound are one asset of the summed capacity. Verified
    # on a 48 h model, 5,590 assets against 4,165 with the objective identical to
    # 5.9e-14 relative. California regions are never merged, since per-substation
    # resolution there is the point of the study.
    non_ca = [n for n in (nlg.get("nodes") or [])
              if n.get("id") not in astr_v2.CALIFORNIA_REGIONS]
    graph = astr_v2.to_v2(
        nlg,
        transmission_efficiency=astr_v2.TRANSMISSION_EFFICIENCY,
        aggregate_regions=[n["id"] for n in non_ca if not str(n["id"]).startswith("SUB_")],
    )
    print(f"  Building network [{label}] nodes={graph.number_of_nodes()} edges={graph.number_of_edges()}")
    t0 = time.time()
    network = good.Network(**network_kw).from_graph(graph, policies)
    network.build()
    print(f"    built in {time.time()-t0:.1f}s; steps={network.steps}")
    print(f"  Solving [{label}] ...")
    t0 = time.time()
    network.solve(**solver_kw)
    print(f"    solved in {time.time()-t0:.1f}s")

    obj = None
    try:
        obj = float(network.objective_value)
    except Exception:
        obj = None

    return network.solution_graph(), graph, obj


def _disable_solar_wind_capex(nlg: dict) -> dict:
    """Turn off CAPEX expansion on all solar/wind assets.

    All 783 extensible solar/wind slots in the base WEC model are
    "optional_"-prefixed speculative-buildout assets with capex_capacity
    bounds up to ~3.2 TW each (37.8 TW summed) and near-zero operating/capex
    cost. With Crossover=0 the barrier method has almost no gradient to pin
    these down, so individual slots land on wildly different, physically
    absurd values between otherwise-similar scenario solves (e.g. one CA
    solar slot: 0.4 W in an S0 solve vs 18.6 GW in the matching S1 solve)
    while the median slot is untouched. No already-installed (non-optional)
    solar/wind asset is extensible, so this only removes the speculative
    buildout headroom, not real existing plant dispatch.
    """
    g = deepcopy(nlg)
    n_disabled = 0
    for node in g.get("nodes") or []:
        for asset in (node.get("assets") or {}).values():
            if str(asset.get("fuel", "")).lower() not in ("solar", "wind"):
                continue
            if asset.get("extensible"):
                asset["extensible"] = False
                asset["capex_capacity"] = 0
                n_disabled += 1
    print(f"  Disabled CAPEX expansion on {n_disabled} solar/wind assets")
    return g


def _save_baseline_expansions(path: Path, expansions: dict) -> None:
    payload = [[region, handle, val] for (region, handle), val in expansions.items()]
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_baseline_expansions(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {(region, handle): val for region, handle, val in payload}


def _disable_capex_nlg(nlg: dict, prescribe_bess: bool = True) -> dict:
    """Close every CAPEX decision: dispatch the as-built system only.

    Every extensible asset in WEC.json carries ``installed_capacity = 0`` and
    unlimited ``capex_capacity``, so switching CAPEX off removes all 799 of
    them -- the 16 BA-level ``optional_storage_*`` above all, which the sizing
    pass was building out to 7.3 GW in S0 and 12.5 GW in S3. What survives is
    the genuinely installed fleet: 4.22 GW of pumped hydro and 0.60 GW of
    battery, plus all real generation. That is the point of the mode -- ask
    what the grid as it stands does with the demand, with no investment
    allowed to paper over a shortage.

    S2's substation batteries are pure CAPEX too (``installed_capacity = 0``,
    everything in ``capex_capacity``), so a blanket switch-off would make S2
    byte-identical to S1 and force M_BESS to zero by construction. With
    ``prescribe_bess`` their per-site cap is moved into ``installed_capacity``
    instead, giving a fleet fixed exogenously by the rule already in
    ``09_01::candidate_bess`` (half the substation's mean EV peak, 1 MW floor).
    M_BESS then measures the value of a prescribed fleet rather than of an
    optimally-sized one.

    Removing the 16 optional_storage assets also removes the 16 dense columns
    the four-week barrier log reported. A capex variable sits in every hour's
    capacity constraint, which is the classic pathology for Cholesky
    factorisation, and the four-week solve stalled with exactly that count.
    """
    g = deepcopy(nlg)
    n_off = n_bess = 0
    bess_w = 0.0
    for node in g.get("nodes") or []:
        for handle, asset in (node.get("assets") or {}).items():
            if not asset.get("extensible"):
                continue
            headroom = float(asset.get("capex_capacity") or 0.0)
            if prescribe_bess and handle.startswith("bess_") and np.isfinite(headroom) and headroom > 0:
                asset["installed_capacity"] = float(asset.get("installed_capacity") or 0.0) + headroom
                bess_w += headroom
                n_bess += 1
            asset["capex_capacity"] = 0.0
            asset["extensible"] = False
            n_off += 1
    msg = f"  CAPEX disabled on {n_off} assets (as-built dispatch only)"
    if n_bess:
        msg += f"; {n_bess} substation BESS prescribed at {bess_w / 1e6:,.0f} MW"
    print(msg)
    return g


def _extract_capex_decisions(solution, nlg: dict) -> dict:
    """TOTAL built capacity in MW for every extensible asset, keyed (region, handle).

    Deliberately broader than upstream's ``migrate``-era capex helper, which only
    matched handles beginning ``optional_``. The substation batteries
    scenario S2 adds are named ``bess_<hub>`` and that filter misses them
    entirely, so a two-stage run built on it would silently re-decide every
    battery in each stage-2 week instead of holding stage 1's build fixed --
    which is precisely the thing two-stage exists to prevent.

    Records the absolute total (``installed_capacity + capex``) rather than the
    solved ``capex`` delta, because that delta is measured against whatever
    baseline the scenario was handed and the baseline is not the same for every
    scenario. S1-S3 run with ``_apply_capex_floor_nlg`` having already folded
    S0's build into ``installed_capacity``, so their ``capex`` is only the
    increment above S0. Stage 2 rebuilds each graph from the unflooded base, so
    replaying a delta there would drop the floored-in capacity: in the first
    stage-1 run S0 recorded 7,220 MW of storage and S1 recorded 1,261 MW, which
    read as S1 building less storage than S0 despite serving strictly more
    load. Absolute totals mean the same number regardless of which baseline
    produced them.

    Units: the graph in *nlg* is still in v1 watts, the solution is in v2 MW, so
    the installed base is converted before the two are added. The file this
    feeds is therefore in MW throughout, and ``_fix_capex_nlg`` converts back on
    the way in.
    """
    base_ic = {
        (n["id"], h): float(a.get("installed_capacity") or 0.0) / astr_v2.W_PER_MW
        for n in (nlg.get("nodes") or [])
        for h, a in (n.get("assets") or {}).items()
        if a.get("extensible")
    }
    out: dict = {}
    for region, node in solution._node.items():
        for handle, asset in (node.get("assets") or {}).items():
            if (region, handle) not in base_ic:
                continue
            v = asset.get("new_capacity", asset.get("capex", [0]))
            if isinstance(v, (list, tuple)):
                mw = float(v[0]) if v else 0.0
            else:
                mw = float(v)
            out[(region, handle)] = max(0.0, base_ic[(region, handle)] + mw)
    return out


def _save_stage1_capex(path: Path, scen: str, decisions: dict) -> None:
    """Merge one scenario's stage-1 build into the shared stage-1 capex file.

    Keyed by scenario because the build is scenario-specific: S2 sizes BESS
    that S1 does not have at all, and S3's relaxed transmission changes where
    generation is worth adding.
    """
    payload = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    payload[scen] = [[region, handle, val] for (region, handle), val in decisions.items()]
    C.ensure_dir(path.parent)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_stage1_capex(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        scen: {(region, handle): val for region, handle, val in rows}
        for scen, rows in payload.items()
    }


def _fix_capex_nlg(nlg: dict, decisions: dict) -> dict:
    """Stage 2: hold stage 1's build fixed rather than re-deciding it per week.

    Every extensible asset is closed out. Assets stage 1 built get their
    ``installed_capacity`` *set* to stage 1's total -- set, not incremented,
    because ``_extract_capex_decisions`` records the absolute built capacity.
    Assets stage 1 declined, and any asset stage 1 never saw, are frozen at
    their installed base. Leaving even a few assets open would let each seasonal
    week buy its own capacity, so the four weeks would no longer describe one
    consistent system and their sum would not be an annual result.

    ``decisions`` is in MW (see ``_extract_capex_decisions``) while this graph is
    still in v1 watts, so the capacities are converted on the way in.
    """
    g = deepcopy(nlg)
    n_built = n_frozen = 0
    built_mw = 0.0
    for node in g.get("nodes") or []:
        for handle, asset in (node.get("assets") or {}).items():
            if not asset.get("extensible"):
                continue
            built = decisions.get((node["id"], handle))
            asset["capex_capacity"] = 0.0
            asset["extensible"] = False
            if built:
                asset["installed_capacity"] = float(built) * astr_v2.W_PER_MW
                built_mw += float(built)
                n_built += 1
            else:
                n_frozen += 1
    print(f"  CAPEX fixed from stage 1: {n_built} assets held at {built_mw:,.0f} MW, "
          f"{n_frozen} frozen at base")
    return g


def _congestion_ranked_bess_hubs(line_flows_csv: Path, network_json: Path, frac: float) -> set[str]:
    """Top ``frac`` of EV-positive substations, ranked by incident-line congestion.

    Congestion score per substation = sum of binding_hours (from line_flows_csv,
    itself produced by a prior S1 solve) over every line touching that
    substation. Restricted to the existing EV-positive BESS candidate pool
    (network_json's bess_candidates) so BESS still lands where there is local
    EV demand to smooth, just prioritized by transmission stress.
    """
    lf = pd.read_csv(line_flows_csv)
    with open(network_json, encoding="utf-8") as fh:
        net = json.load(fh)
    ev_positive = {c["hub_id"] for c in (net.get("bess_candidates") or [])}
    score: dict[str, float] = {}
    for _, r in lf.iterrows():
        for col in ("source", "target"):
            v = r[col]
            if isinstance(v, str) and v.startswith("SUB_"):
                score[v] = score.get(v, 0.0) + float(r["binding_hours"])
    ranked = sorted(ev_positive, key=lambda h: score.get(h, 0.0), reverse=True)
    k = max(1, int(round(len(ranked) * frac)))
    return set(ranked[:k])


def _apply_capex_floor_nlg(nlg: dict, expansions: dict) -> dict:
    """Lock later scenarios to at least S0 renewable/storage expansion (nlg dict).

    ``expansions`` holds the MW *increment* each asset built in S0, as
    ``astr_v2.extract_capex_expansion`` reports it; this graph is still in v1
    watts, so each increment is converted before being folded in.
    """
    g = deepcopy(nlg)
    by_id = {n["id"]: n for n in g.get("nodes") or []}
    for (region, handle), expansion_mw in expansions.items():
        node = by_id.get(region)
        if not node:
            continue
        asset = (node.get("assets") or {}).get(handle)
        if not asset:
            continue
        expansion_w = float(expansion_mw) * astr_v2.W_PER_MW
        orig_ic = float(asset.get("installed_capacity") or 0.0)
        orig_cap = float(asset.get("capex_capacity") or 0.0)
        asset["installed_capacity"] = orig_ic + expansion_w
        asset["capex_capacity"] = max(0.0, orig_cap - expansion_w)
        if asset["capex_capacity"] == 0:
            asset["extensible"] = False
    return g


def _compact_run_rows(horizon: str, tag: str, scen: str, gen: pd.DataFrame, lines: pd.DataFrame, co2: float, obj):
    fuel = (
        gen.groupby("fuel", as_index=False)["energy_MWh"].sum()
        if not gen.empty
        else pd.DataFrame(columns=["fuel", "energy_MWh"])
    )
    binding = int(lines["binding_hours"].sum()) if not lines.empty else 0
    row = {
        "horizon": horizon,
        "tag": tag,
        "scenario": scen,
        "co2_kg": co2,
        "objective": obj,
        "binding_line_hours": binding,
        "n_assets": int(len(gen)),
        "n_lines": int(len(lines)),
    }
    for _, r in fuel.iterrows():
        row[f"energy_MWh_{r['fuel']}"] = r["energy_MWh"]
    return row


def run_horizon(
    horizon: str,
    scales: dict,
    dry_run: bool = False,
    skip_existing: bool = False,
    save_json: bool = False,
    week: dict | None = None,
    bess_top_n: int = 0,
    bess_congestion_frac: float | None = None,
    crossover: int | str = "auto",
    only_ba: set[str] | None = None,
    stage1_capex_out: Path | None = None,
    fix_capex_from: Path | None = None,
    no_capex: bool = False,
    bess_csv: Path | None = None,
    line_csv: Path | None = None,
    out_suffix: str | None = None,
    rps_noncompliance_cost: float | None = None,
    rps_basis: str = "generation",
    tag_suffix: str | None = None,
) -> pd.DataFrame:
    import good
    from good import migrate

    solution_to_dict = astr_v2.solution_to_dict

    tag = week["name"] if week is not None else horizon
    if only_ba:
        tag = f"{tag}_{'+'.join(sorted(only_ba))}"
    if tag_suffix:
        # A whole variant of the run (a different treatment of the RPS, say)
        # gets its own results directory, beside the reference one.
        tag = f"{tag}{tag_suffix}"
    out_root = C.ensure_dir(C.ASTR_RESULTS_DIR / tag)
    n_hours = _num_hours(horizon) if horizon != "weekly" else C.NUM_HOURS_WEEK

    # No deep_reload(good) here. v1 reloaded the package so edits to the solver
    # core were picked up without restarting, but v2 resolves a component's class
    # through a registry populated at import time and then checks membership with
    # issubclass. A deep reload rebinds every class to a new object, so the
    # registry holds the pre-reload classes while the graph is validated against
    # the post-reload ones, and every single component is rejected as "not a
    # Node, Edge, Asset, Line or Policy subclass" -- 13,771 of them on a PG&E-only
    # week, which reads like a schema failure rather than a stale import.
    wec_path = C.resolve_wec_json()
    with open(wec_path, encoding="utf-8") as fh:
        base_nlg = json.load(fh)
    base_nlg = _transform_nlg_profiles(base_nlg, "weekly" if week else horizon, week)
    base_nlg = _disable_solar_wind_capex(base_nlg)

    network_blob = _nest._load_network()
    hub_ids, ev_kW, tot_kW = _hub_load_arrays("weekly" if week else horizon, week)

    with open(C.POLICIES_JSON, encoding="utf-8") as fh:
        policies = json.load(fh)

    # GOOD 2.0 replaced the lambda-string `inclusion_criteria` / `exclusion_criteria`
    # with declarative `include` / `exclude` filters. convert_policies() does the
    # translation, so the published policy file stays in its original form and
    # the RPS ratios remain traceable to it. All 32 state standards convert.
    policies = migrate.convert_policies(policies)

    # See astr_v2.RPS_NONCOMPLIANCE_COST. None leaves each standard as the hard
    # limit it is in the published file.
    if rps_noncompliance_cost is not None:
        n_rps = 0
        for pol in policies.values():
            if pol.get("_class") == "Portfolio_Standard":
                pol["non_compliance_cost"] = float(rps_noncompliance_cost)
                pol["non_compliance_capacity"] = astr_v2.RPS_NONCOMPLIANCE_CAPACITY_MWH
                n_rps += 1
        print(f"  RPS: non-compliance allowed at ${rps_noncompliance_cost:,.0f}/MWh on {n_rps} state standards")
    else:
        print("  RPS: hard limit, no non-compliance allowed")

    # On the load basis the standard reads: renewable output >= ratio x load
    # served in the state. In as-built dispatch both sides are fixed -- nothing
    # can be built, wind and solar are must-take, and load is served up to the
    # network's limits whatever the standard says -- so no dispatch choice
    # changes compliance and the constraint has nothing to act on. It is
    # therefore left out of the optimisation and reported from the result. That
    # is exact for this mode and would not be if capacity could be built.
    solver_policies = policies
    if rps_basis == "load":
        solver_policies = {h: pol for h, pol in policies.items()
                           if pol.get("_class") != "Portfolio_Standard"}
        print(f"  RPS: on load served; {len(policies) - len(solver_policies)} standards reported "
              f"after the solve and not constraining dispatch")

    network_kw = dict(astr_v2.NETWORK_KW)
    network_kw["steps"] = (0, n_hours)
    time_step = float(network_kw.get("time_step", 1.0))
    # shortfall_cost / wastage_cost are deliberately NOT overridden here. They
    # are benchmark-calibrated in astr_v2.NETWORK_KW (VOLL $10,000/MWh and
    # curtailment $1/MWh, now plain $/MWh rather than $/J), which is the single
    # source of truth; see docs/cost_calibration_methodology.tex. Note that the
    # v2 Network default for wastage_cost is $10,000/MWh -- equal to lost load,
    # which would price curtailment as if it were a blackout -- so leaving it
    # unset would silently change dispatch.

    # v1 applied ev_charging_project.utils.prepare_graph here, which did five
    # things. GOOD 2.x's own migrate.from_v1 now covers three of them: storage
    # duration, per-line efficiency, and the wind/solar capex rescale (its
    # vre_capex_rescale is 1e3 x 1e6, matching v1's WIND_SOLAR_MULT=1000 and the
    # $/W -> $/MW conversion). Nuclear baseload is applied by astr_v2.to_v2 at
    # solve time, and the optional_* capex flags are handled by
    # _disable_solar_wind_capex above and _disable_capex_nlg below. So there is
    # nothing left to pre-apply to base_nlg, and the throwaway round trip
    # through NetworkX that used to happen here is gone.

    summary_rows = []
    compact_rows = []
    baseline_capex_path = out_root / "baseline_capex_expansion.json"
    baseline_expansions = None
    stage1_capex = _load_stage1_capex(fix_capex_from) if fix_capex_from else None
    if stage1_capex is not None:
        print(f"  Stage 2: CAPEX fixed from {fix_capex_from} "
              f"({', '.join(sorted(stage1_capex))})")
    if not no_capex and stage1_capex is None and "S0" not in scales and baseline_capex_path.is_file():
        baseline_expansions = _load_baseline_expansions(baseline_capex_path)
        print(f"  Loaded cached baseline CAPEX floor from {baseline_capex_path} ({len(baseline_expansions)} entries)")

    # Explicit per-node BESS sizing from 08_13, replacing the EV-peak rule.
    bess_spec = None
    if bess_csv is not None:
        bs = pd.read_csv(bess_csv)
        bess_spec = {
            str(r["node"]): {
                "capex_capacity_W": float(r["capex_capacity_W"]),
                "duration_h": float(r["duration_h"]),
            }
            for _, r in bs.iterrows()
        }
        print(f"  BESS spec from {bess_csv}: {len(bess_spec):,} nodes, "
              f"{bs['power_MW'].sum():,.0f} MW / {bs['energy_MWh'].sum():,.0f} MWh")

    # Per-corridor target capacity from 08_15, replacing the blanket 10x scale.
    line_spec = None
    if line_csv is not None:
        ls = pd.read_csv(line_csv)
        line_spec = {str(r["line"]): float(r["target_capacity_W"]) for _, r in ls.iterrows()}
        need = ls["need_MW"] if "need_MW" in ls.columns else None
        print(f"  transmission spec from {line_csv}: {len(line_spec):,} arcs"
              + (f", {int((need >= 0.1).sum()):,} upgraded, {need.sum():,.0f} MW added"
                 if need is not None else ""))

    bess_hub_override = None
    if bess_congestion_frac is not None:
        s1_line_flows = out_root / "S1" / "line_flows_summary.csv"
        C.require_file(s1_line_flows, hint="Run S1 in this horizon/tag first to rank congestion.")
        bess_hub_override = _congestion_ranked_bess_hubs(s1_line_flows, C.CA_NETWORK_JSON, bess_congestion_frac)
        print(f"  Congestion-ranked BESS: top {bess_congestion_frac*100:.0f}% -> {len(bess_hub_override)} substations")

    for scen, sc in scales.items():
        print(f"\n=== {tag.upper()} / {scen}  horizon={horizon} hours={n_hours} ===")
        scen_bess_override = bess_hub_override if (bess_hub_override is not None and sc.get("bess")) else None
        nlg = _nest.build_nested_graph(
            base_nlg,
            network_blob,
            hub_ids,
            ev_kW,
            tot_kW,
            include_bess=bool(sc.get("bess")),
            include_ev=bool(sc.get("ev")),
            capacity_scale_meso=float(sc["meso"]),
            capacity_scale_interface=float(sc["interface"]),
            capacity_scale_transformer=float(sc.get("transformer", 1.0)),
            bess_spec=bess_spec,
            bess_top_n=bess_top_n,
            bess_hub_override=scen_bess_override,
            only_ba=only_ba,
            line_spec=line_spec,
        )
        if no_capex:
            nlg = _disable_capex_nlg(nlg, prescribe_bess=True)
        elif stage1_capex is not None:
            if scen not in stage1_capex:
                raise SystemExit(
                    f"Stage 2 asked for {scen} but {fix_capex_from} has no stage-1 build "
                    f"for it (has: {', '.join(sorted(stage1_capex))}). Run stage 1 for "
                    f"{scen} first."
                )
            nlg = _fix_capex_nlg(nlg, stage1_capex[scen])
        elif baseline_expansions and scen != "S0":
            nlg = _apply_capex_floor_nlg(nlg, baseline_expansions)

        scen_out_name = scen
        if scen_bess_override is not None:
            scen_out_name = f"{scen}_bess{bess_congestion_frac*100:.0f}pct"
        if out_suffix:
            # A variant of a scenario (a different prescribed fleet, say) gets
            # its own directory and summary row, so it cannot overwrite the
            # reference run it is compared against.
            scen_out_name = f"{scen_out_name}_{out_suffix}"
        scen_dir = C.ensure_dir(out_root / scen_out_name)

        if crossover == "auto":
            # -1 lets Gurobi choose, for both scenario families.
            #
            # The old rule (crossover on without EV, off with it) existed only
            # to dodge Pyomo: Crossover=1 reproducibly hung for 12+ hours in
            # Pyomo's solution-loading step, walking ~10M variables one at a
            # time, once EV load made the solution dense. GOOD 2.x reads the
            # solution back as whole xarray arrays, so that failure mode is gone
            # with Pyomo itself.
            #
            # Measured on the PG&E March week, all four certified optimal:
            #   Crossover=-1   44.8 s   objective 2.675762e8   shortfall 14.333 GWh
            #   Crossover=1    44.0 s   objective 2.676289e8   shortfall 14.338 GWh
            #   Crossover=1    ~45 s    objective 2.680771e8   shortfall 14.383 GWh
            #   Method=1       53.1 s   objective 2.678213e8   shortfall 14.358 GWh
            # -1 is both the fastest and the lowest objective, so it is the
            # default now. See the note in _solver_kw on why the objective
            # spread across these is not a convergence problem.
            scen_crossover = -1
        else:
            scen_crossover = int(crossover)
        n_nodes = len(nlg["nodes"])
        n_edges = len(nlg["edges"])
        if dry_run:
            print(f"  dry-run: nodes={n_nodes} edges={n_edges}")
            summary_rows.append(
                {
                    "tag": tag,
                    "horizon": horizon,
                    "scenario": scen,
                    "status": "dry_run",
                    "n_nodes": n_nodes,
                    "n_edges": n_edges,
                    "n_hours": n_hours,
                }
            )
            continue

        obj_path = scen_dir / "objective.txt"
        if skip_existing and obj_path.is_file() and (scen_dir / "generation_by_asset.csv").is_file():
            obj = co2 = None
            for line in obj_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("objective="):
                    try:
                        obj = float(line.split("=", 1)[1])
                    except ValueError:
                        pass
                if line.startswith("co2_kg="):
                    try:
                        co2 = float(line.split("=", 1)[1])
                    except ValueError:
                        pass
            summary_rows.append(
                {
                    "tag": tag,
                    "horizon": horizon,
                    "scenario": scen_out_name,
                    "status": "ok",
                    "objective": obj,
                    "co2_kg": co2,
                    "n_nodes": n_nodes,
                    "n_edges": n_edges,
                    "n_hours": n_hours,
                }
            )
            print(f"  skip-existing: co2_kg={co2} objective={obj}")
            continue

        try:
            solver_kw = _solver_kw(horizon, scen, log_path=scen_dir / "gurobi.log", crossover=scen_crossover)
            solution, built_graph, obj = _solve_nlg(nlg, solver_policies, network_kw, solver_kw, f"{tag}-{scen}")
            if stage1_capex_out is not None:
                decisions = _extract_capex_decisions(solution, nlg)
                _save_stage1_capex(stage1_capex_out, scen, decisions)
                built_mw = sum(v for v in decisions.values() if v)
                print(f"  stage-1 build: {sum(1 for v in decisions.values() if v)}"
                      f"/{len(decisions)} extensible assets, {built_mw:,.0f} MW"
                      f" total capacity -> {stage1_capex_out}")
            if scen == "S0" and stage1_capex is None and not no_capex:
                baseline_expansions = astr_v2.extract_capex_expansion(solution, built_graph)
                _save_baseline_expansions(baseline_capex_path, baseline_expansions)
            gen = _generation_totals(solution, built_graph, time_step)
            gen.drop(columns=["hourly_MW"], errors="ignore").to_csv(
                scen_dir / "generation_by_asset.csv", index=False
            )
            curves = _write_hourly_curves(scen_dir, gen, solution, time_step)
            _rps_compliance(gen, policies, rps_basis, curves.get("load_by_ba")).to_csv(
                scen_dir / "rps_compliance.csv", index=False)
            (scen_dir / "settings.json").write_text(json.dumps({
                "no_capex": bool(no_capex),
                "rps_noncompliance_cost_per_MWh": rps_noncompliance_cost,
                "rps_basis": rps_basis,
                "bess_csv": str(bess_csv) if bess_csv is not None else None,
                "line_csv": str(line_csv) if line_csv is not None else None,
                "out_suffix": out_suffix,
            }, indent=2), encoding="utf-8")
            lines = _line_records(solution, built_graph)
            lines.to_csv(scen_dir / "line_flows_summary.csv", index=False)
            bess = _bess_records(solution, built_graph, time_step)
            if not bess.empty:
                bess.to_csv(scen_dir / "bess_summary.csv", index=False)
            curtailment = _vre_curtailment(solution, built_graph, time_step)
            sfw = _shortfall_wastage_totals(solution, time_step, curtailment)
            (scen_dir / "shortfall_wastage.json").write_text(json.dumps(sfw, indent=2), encoding="utf-8")
            # Both tables are written even when empty. Skipping the write left the
            # previous run's table in place whenever a scenario reached zero: S4
            # solved to 0.000 GWh of shortfall and its directory still listed three
            # Merced substations short by 4.06 GWh from the run before.
            sbn = _shortfall_by_node(solution, time_step)
            if sbn.empty:
                sbn = pd.DataFrame(columns=["node", "shortfall_MWh", "shortfall_GWh", "peak_MW",
                                            "hours_short", "longest_run_h", "longest_run_MWh"])
            sbn.to_csv(scen_dir / "shortfall_by_node.csv", index=False)
            wbn = _wastage_by_node(solution, time_step, curtailment)
            if wbn.empty:
                wbn = pd.DataFrame(columns=["node", "wastage_MWh", "wastage_GWh", "peak_MW", "mean_MW",
                                            "hours_spilling", "hours_free", "longest_run_h",
                                            "longest_run_MWh"])
            wbn.to_csv(scen_dir / "wastage_by_node.csv", index=False)
            print(
                f"  shortfall={sfw['shortfall_GWh']:.3f} GWh "
                f"({sfw['shortfall_MWh']*float(network_kw.get('shortfall_cost') or 0):.3e} $)  "
                f"wastage={sfw['wastage_GWh']:.3f} GWh "
                f"(dumped {sfw['dumped_surplus_GWh']:.3f} + VRE curtailed "
                f"{sfw['vre_curtailment_GWh']:.3f})"
            )
            co2 = _emissions_kg(gen)
            obj_path.write_text(f"objective={obj}\nco2_kg={co2}\nn_hours={n_hours}\n", encoding="utf-8")
            if save_json:
                with open(scen_dir / "solution.json", "w", encoding="utf-8") as fh:
                    json.dump(solution_to_dict(solution), fh)
            compact_rows.append(_compact_run_rows(horizon, tag, scen_out_name, gen, lines, co2, obj))
            summary_rows.append(
                {
                    "tag": tag,
                    "horizon": horizon,
                    "scenario": scen_out_name,
                    "status": "ok",
                    "objective": obj,
                    "co2_kg": co2,
                    "n_nodes": n_nodes,
                    "n_edges": n_edges,
                    "n_hours": n_hours,
                    "binding_line_hours": int(lines["binding_hours"].sum()) if not lines.empty else 0,
                }
            )
            print(f"  co2_kg={co2:.3e} objective={obj}")
        except Exception as exc:
            (scen_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            # Retire this scenario's previous result files. A failed solve used
            # to leave them untouched, so the directory still looked like a
            # complete run and downstream readers picked up numbers from a
            # different model: after S2 hit its time limit here, its
            # objective.txt still held the CAPEX-enabled stage-1 value (matching
            # to ten digits), and M_BESS was computed against it. The parquet
            # rows carry a graph_build fingerprint that would have caught this;
            # these per-scenario files carry none, so move them aside instead.
            stale_dir = scen_dir.parent / f"{scen_out_name}.stale"
            moved = []
            for fname in ("objective.txt", "shortfall_wastage.json",
                          "generation_by_asset.csv", "line_flows_summary.csv",
                          "bess_summary.csv", "solution.json",
                          "generation_hourly_by_fuel.csv", "heat_input_hourly_by_fuel.csv",
                          "co2_hourly_by_fuel.csv", "generation_hourly_by_ba_fuel.csv.gz",
                          "balance_hourly.csv", "balance.json",
                          "rps_compliance.csv", "settings.json", "load_by_ba.csv"):
                src = scen_dir / fname
                if src.is_file():
                    C.ensure_dir(stale_dir)
                    src.replace(stale_dir / fname)
                    moved.append(fname)
            if moved:
                print(f"  solve failed; retired {len(moved)} stale result file(s) "
                      f"to {stale_dir.name}/ so they are not read as this run's")
            summary_rows.append(
                {
                    "tag": tag,
                    "horizon": horizon,
                    "scenario": scen_out_name,
                    "status": f"error: {exc}",
                    "n_nodes": n_nodes,
                    "n_edges": n_edges,
                    "n_hours": n_hours,
                }
            )
            print(f"  ERROR: {exc}")
        gc.collect()

    summary = pd.DataFrame(summary_rows)
    summary_path = out_root / "scenario_summary.csv"
    if summary_path.is_file():
        # Upsert on (tag, scenario) so a scenario-subset invocation (e.g. a
        # single-scenario BESS-sweep run) doesn't wipe out rows for
        # scenarios it didn't touch this time.
        prev = pd.read_csv(summary_path)
        prev = prev[~prev["scenario"].isin(summary["scenario"])]
        summary = pd.concat([prev, summary], ignore_index=True)
    summary.to_csv(summary_path, index=False)
    if compact_rows:
        C.ensure_dir(C.RESULTS_DIR)
        compact = pd.DataFrame(compact_rows)
        # Stamp every row with the network build that produced it.
        #
        # The upsert below deliberately preserves rows for scenarios this
        # invocation did not run, so a subset run does not wipe the rest. But
        # that also preserves rows from an *older model*, and those are not
        # comparable: after the topology and demand rebuild, a retained S1 row
        # showed a lower objective than a freshly solved S0, which is
        # impossible when S1 serves strictly more demand. Without a stamp the
        # only clue was an n_lines mismatch.
        compact["graph_build"] = _graph_fingerprint()
        compact["built_at"] = pd.Timestamp.now().isoformat(timespec="seconds")
        out_pq = C.RUNS_8760_PARQUET if horizon == "8760" else C.RUNS_FOUR_WEEK_PARQUET
        if horizon == "weekly":
            out_pq = C.RESULTS_DIR / "S0_S3_weekly_runs.parquet"
        if out_pq.is_file():
            prev = pd.read_parquet(out_pq)
            prev = prev[~prev["tag"].eq(tag) | ~prev["scenario"].isin(compact["scenario"])]
            if "graph_build" in prev.columns and len(prev):
                stale = prev[prev["graph_build"] != compact["graph_build"].iloc[0]]
                if len(stale):
                    print(
                        f"  WARNING: {len(stale)} retained row(s) come from an earlier "
                        f"network build and are NOT comparable with this run: "
                        + ", ".join(sorted(set(stale["tag"] + "/" + stale["scenario"])))
                    )
            elif len(prev):
                print(f"  WARNING: {len(prev)} retained row(s) predate build stamping "
                      f"and are NOT comparable with this run")
            compact = pd.concat([prev, compact], ignore_index=True)
        compact.to_parquet(out_pq, index=False)
        print(f"Wrote {out_pq}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--horizon",
        default="four_week",
        choices=["four_week", "8760", "weekly"],
        help="four_week = 4 seasonal weeks in one LP (default test). "
        "8760 = full year (do not run until four_week is feasible).",
    )
    parser.add_argument(
        "--only-ba",
        nargs="*",
        default=None,
        help="Nest only these BAs at substation level; the rest stay copper plates. "
             "Use for framework tests: P_cong from a restricted run is a lower bound.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--save-json", action="store_true", help="Write full solution.json (large).")
    parser.add_argument("--seasons", nargs="*", default=None, help="With --horizon weekly, subset of seasons.")
    parser.add_argument("--bess-top-n", type=int, default=0, help="If >0, only top-N EV nodes get S2 BESS.")
    parser.add_argument(
        "--bess-congestion-frac",
        type=float,
        default=None,
        help="If set, restrict S2 BESS to the top FRAC of EV-positive substations ranked by "
        "S1 line congestion (sum of binding_hours over incident lines), instead of all EV-"
        "positive substations. Requires S1 already solved in this horizon/tag (reads its "
        "line_flows_summary.csv). Writes to a 'S2_bess<pct>pct' subdirectory so multiple "
        "sweep points don't overwrite each other.",
    )
    parser.add_argument(
        "--scenarios",
        nargs="*",
        default=None,
        help="Subset of scenario keys to run (e.g. --scenarios S2). Default: all in scales.",
    )
    parser.add_argument(
        "--crossover",
        default="auto",
        help="Gurobi Crossover policy. 'auto' (default): Crossover=1 (push to a basic/vertex "
        "solution, trustworthy per-asset values) for scenarios without EV load (e.g. S0), and "
        "Crossover=0 (interior-point only, guarded against unloadable results) for EV-inclusive "
        "scenarios, which reproducibly hang for 12+ hours in Pyomo's solution-loading step under "
        "Crossover=1 once EV load makes the solution much denser. Pass 0 or 1 to force that "
        "value for every scenario instead.",
    )
    parser.add_argument(
        "--stage1-capex-out",
        default=None,
        help="Two-stage sizing pass. Write the solved capacity of every extensible asset, "
        "per scenario, to this JSON. Pair with --horizon four_week --crossover 0: the "
        "barrier alone settles the build in ~6 min, where pushing the same model to a "
        "vertex does not finish in 4 h.",
    )
    parser.add_argument(
        "--fix-capex-from",
        default=None,
        help="Two-stage dispatch pass. Hold every extensible asset at the build in this "
        "JSON (from --stage1-capex-out) instead of re-deciding it. Pair with "
        "--horizon weekly --crossover 1: each seasonal week is a quarter the size and "
        "does reach a vertex, and all four share one consistent build.",
    )
    parser.add_argument(
        "--bess-csv",
        default=None,
        help="Per-node BESS sizing from 08_13_size_bess_to_deficit.py. Replaces the "
        "EV-peak rule in 09_01::candidate_bess, which sized the fleet to a tenth of "
        "the deficit and left 775 of 786 batteries idle.",
    )
    parser.add_argument(
        "--line-csv",
        default=None,
        help="Per-corridor target capacity from 08_15_size_transmission_to_overload.py. "
             "Overrides the blanket --scenarios S3/S4 scale factor for the arcs it names, "
             "so the relaxation is sized to the overload actually observed rather than "
             "multiplying every corridor by ten. Use with --scenarios S5.",
    )
    parser.add_argument(
        "--out-suffix",
        default=None,
        help="Write each scenario to '<scenario>_<suffix>' instead of '<scenario>', and "
             "record it under that name. For variants of a scenario, such as a different "
             "--bess-csv fleet, that must not overwrite the reference run.",
    )
    parser.add_argument(
        "--rps-basis",
        choices=("generation", "load"),
        default="generation",
        help="What each state RPS ratio is a share of. 'generation' (default) is the published "
             "form, all generation by plants in the state. 'load' is load served in the state, "
             "which is what the ratios mean in the ReEDS file they come from; in as-built "
             "dispatch that cannot be changed by dispatch, so the standard is reported and "
             "does not constrain the solve.",
    )
    parser.add_argument(
        "--tag-suffix",
        default=None,
        help="Appended to the results tag, so a variant of the whole run writes beside the "
             "reference results, e.g. '.rps_load'.",
    )
    parser.add_argument(
        "--rps-noncompliance-cost",
        default="auto",
        help="Cost in $/MWh of falling short of a state RPS. 'auto' (default) uses "
             "astr_v2.RPS_NONCOMPLIANCE_COST with --no-capex, where a hard RPS can only "
             "cap in-state generation, and the hard limit otherwise. 'hard' forces the "
             "published hard limit. A number sets the cost.",
    )
    parser.add_argument(
        "--no-capex",
        action="store_true",
        help="Dispatch the as-built system: close every CAPEX decision so no new "
        "capacity can be built. Keeps the 4.22 GW of installed pumped hydro and "
        "0.60 GW of battery; drops the 799 optional assets. S2's substation "
        "batteries are prescribed at their per-site cap instead of being built, "
        "otherwise S2 would collapse into S1.",
    )
    args = parser.parse_args()

    choice = str(args.rps_noncompliance_cost).lower()
    if choice == "hard":
        rps_cost = None
    elif choice == "auto":
        rps_cost = astr_v2.RPS_NONCOMPLIANCE_COST if args.no_capex else None
    else:
        rps_cost = float(args.rps_noncompliance_cost)

    if args.no_capex and (args.stage1_capex_out or args.fix_capex_from):
        raise SystemExit("--no-capex forbids any CAPEX decision, so the two-stage flags do not apply.")
    if args.stage1_capex_out and args.fix_capex_from:
        raise SystemExit("--stage1-capex-out and --fix-capex-from are the two stages; run them separately.")

    if args.horizon == "8760":
        print(
            "WARNING: --horizon 8760 is the production full-year LP. "
            "It is memory-heavy; the default four_week run is the feasibility test."
        )

    if not C.SCENARIO_SCALES_JSON.is_file():
        scales = {
            "S0": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": False},
            "S1": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": True},
            "S2": {"meso": 1.0, "interface": 1.0, "bess": True, "ev": True},
            "S3": {"meso": 10.0, "interface": 10.0, "bess": False, "ev": True},
        }
    else:
        with open(C.SCENARIO_SCALES_JSON, encoding="utf-8") as fh:
            scales = json.load(fh)

    # S0R is the relaxed-transmission counterfactual with no EV. It exists to
    # decontaminate P_cong. Comparing S3 against S0 conflates two effects:
    # relaxing transmission lets the network deliver *all* load better, not
    # just EV load, and in the PG&E June week the base load dwarfs the EV load
    # (5,628 GWh vs 156 GWh), so the relaxation credit swamps the EV signal.
    # With S0R,
    #     P_cong = (E_S1 - E_S0) - (E_S3 - E_S0R)
    # differences each EV effect against its own transmission regime, so the
    # non-EV relaxation benefit cancels instead of being charged to EVs.
    # It is off by default because it is a fifth full solve; request it with
    # --scenarios S0R.
    optional_scales = {
        "S0R": {"meso": 10.0, "interface": 10.0, "bess": False, "ev": False},
        # S4 relaxes the substation step-down banks as well as the corridors.
        # S3 scales lines only, so transformers stay at their published rating
        # and keep binding: SUB_02201 sheds an identical ~53 GWh in S0, S1, S2
        # and S3 alike, and five such nodes are 76.3 of S3's residual 79.6 GWh.
        # That part of the shortfall is not corridor congestion at all, and no
        # existing scenario can distinguish the two. S3 vs S4 splits the
        # congestion penalty into corridor-attributable and
        # transformer-attributable halves, which matter separately because one
        # implies new lines and the other implies substation upgrades.
        "S4": {"meso": 10.0, "interface": 10.0, "transformer": 10.0,
               "bess": False, "ev": True},
        # S5 is S3's question answered with a build instead of a multiplier. The
        # scale factors are 1.0 because --line-csv sets each corridor explicitly
        # from the overload 08_15 measured, following Li & Jenn's feeder rule
        # (upgrade = maximum overload over the horizon, cost only the overload).
        # S3 grants 6,521,774 MW to get its 31,700 t; only 11,830 MW of that does
        # any work, so S5 is what makes P_cong costable and comparable with the
        # 550 MW storage fleet in S2. Requires --line-csv.
        "S5": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": True},
    }
    for key, spec in optional_scales.items():
        if args.scenarios and key in args.scenarios and key not in scales:
            scales[key] = spec

    if args.scenarios:
        scales = {k: v for k, v in scales.items() if k in args.scenarios}

    if not args.dry_run:
        try:
            import gurobipy  # noqa: F401
        except Exception as exc:
            print(f"Gurobi not available ({exc}); falling back to --dry-run.")
            C.ensure_dir(C.ASTR_RESULTS_DIR)
            (C.ASTR_RESULTS_DIR / "GUROBI_BLOCKER.txt").write_text(
                f"Gurobi import failed: {exc}\n", encoding="utf-8"
            )
            args.dry_run = True

    os.makedirs(os.path.abspath("./gurobi_nodefiles"), exist_ok=True)

    needed = [
        C.CA_NETWORK_JSON,
        C.MESO_DIR / "seasonal" / "june" / "meso_hub_ids.npy",
        C.MESO_DIR / "seasonal" / "june" / "meso_hourly_kW.npy",
    ]
    if args.horizon == "8760":
        needed += [
            C.MESO_DIR / "substation_ids.npy",
            C.MESO_DIR / "substation_hourly_ev_kW_8760.npy",
            C.MESO_DIR / "substation_hourly_total_kW_8760.npy",
        ]
    missing = [str(p) for p in needed if not p.is_file()]
    if missing:
        print("Missing nested-layer artifacts; run 08_01–09_02 before 10_01:")
        for p in missing:
            print(f"  - {p}")
        raise SystemExit(1)

    if args.horizon == "weekly":
        seasons = C.SEASONAL_WEEKS
        if args.seasons:
            seasons = [s for s in seasons if s["name"] in args.seasons]
        for week in seasons:
            run_horizon(
                "weekly",
                scales,
                dry_run=args.dry_run,
                skip_existing=args.skip_existing,
                save_json=args.save_json,
                week=week,
                bess_top_n=args.bess_top_n,
                bess_congestion_frac=args.bess_congestion_frac,
                crossover=args.crossover,
                only_ba=set(args.only_ba) if args.only_ba else None,
                stage1_capex_out=Path(args.stage1_capex_out) if args.stage1_capex_out else None,
                fix_capex_from=Path(args.fix_capex_from) if args.fix_capex_from else None,
                no_capex=args.no_capex,
                bess_csv=Path(args.bess_csv) if args.bess_csv else None,
                line_csv=Path(args.line_csv) if args.line_csv else None,
                out_suffix=args.out_suffix,
                rps_noncompliance_cost=rps_cost,
                rps_basis=args.rps_basis,
                tag_suffix=args.tag_suffix,
            )
    else:
        run_horizon(
            args.horizon,
            scales,
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
            save_json=args.save_json,
            bess_top_n=args.bess_top_n,
            bess_congestion_frac=args.bess_congestion_frac,
            crossover=args.crossover,
            only_ba=set(args.only_ba) if args.only_ba else None,
            stage1_capex_out=Path(args.stage1_capex_out) if args.stage1_capex_out else None,
            fix_capex_from=Path(args.fix_capex_from) if args.fix_capex_from else None,
            no_capex=args.no_capex,
            bess_csv=Path(args.bess_csv) if args.bess_csv else None,
            line_csv=Path(args.line_csv) if args.line_csv else None,
            out_suffix=args.out_suffix,
            rps_noncompliance_cost=rps_cost,
            rps_basis=args.rps_basis,
            tag_suffix=args.tag_suffix,
        )
    print(f"\nResults under {C.ASTR_RESULTS_DIR}")


if __name__ == "__main__":
    main()
