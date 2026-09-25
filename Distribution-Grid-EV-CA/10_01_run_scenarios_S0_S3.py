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

import common as C
import importlib.util

_spec = importlib.util.spec_from_file_location("nest_meso", PKG / "09_02_nest_meso_in_good.py")
_nest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nest)


def _transform_nlg_profiles(nlg: dict, horizon: str, week: dict | None = None) -> dict:
    g = deepcopy(nlg)

    def _one(arr):
        if horizon == "8760":
            return C.pad_or_wrap_hours(arr, C.HOURS_YEAR).tolist()
        if horizon == "four_week":
            return C.concat_seasonal_weeks(arr).tolist()
        start = int((week or C.SEASONAL_WEEKS[0])["start_hour"])
        return C.slice_hours(arr, start, C.NUM_HOURS_WEEK).tolist()

    for node in g.get("nodes") or []:
        profiles = node.get("profiles") or {}
        for key, val in list(profiles.items()):
            if isinstance(val, (list, np.ndarray)):
                profiles[key] = _one(val)
        for asset in (node.get("assets") or {}).values():
            prof = asset.get("profile")
            if isinstance(prof, (list, np.ndarray)):
                asset["profile"] = _one(prof)
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
    import ev_charging_project.config as config

    kw = deepcopy(config.SOLVER_KW)
    opts = kw.setdefault("solver", {}).setdefault("options", {})
    # Dual simplex (Method=1) stalled on the 672 h nested LP (~8.6M rows)
    # with dual infeasibility after 1 h. Barrier is the default for this LP.
    opts["Method"] = 2
    opts["Crossover"] = crossover
    opts["BarHomogeneous"] = 1
    opts["Presolve"] = 2
    opts["NodefileStart"] = 0.5
    opts.setdefault("NodefileDir", os.path.abspath("./gurobi_nodefiles"))
    opts["NumericFocus"] = 1
    opts["ScaleFlag"] = 2
    # Explicit convergence tolerances: previously left at Gurobi defaults, so
    # a badly-scaled model (capacities span ~1e5-1e10 W; costs ~1e-13-1e3
    # across shortfall/wastage/operating) could report "optimal" without
    # actually certifying a tight solution. Loosen from Gurobi's 1e-8/1e-6
    # defaults slightly given the coefficient spread, but keep them explicit
    # so a failure to meet them is visible rather than silently accepted.
    # ASTR_BARCONVTOL loosens this for a two-stage sizing pass. On the 672 h
    # model the barrier stalls at a relative gap of ~3e-4 and reports
    # "Sub-optimal termination": primal residual and complementarity both
    # plateau for the last ~15 iterations rather than falling, so more time
    # does not help. At 1e-7 that stall is a failure and Gurobi hands the
    # point to crossover, which then cannot finish (2.27M iterations, 4 h, no
    # convergence). At a tolerance the model can actually meet, barrier
    # terminates optimal in ~6 min and the build it reports is good to a few
    # parts in ten thousand -- ample for deciding capacity, which is all
    # stage 1 is asked for. Stage 2 keeps the tight default for dispatch.
    opts["BarConvTol"] = float(os.environ.get("ASTR_BARCONVTOL", 1e-7))
    # Loosened from 1e-6 after S1 and S3 both hit the 2 h limit inside
    # crossover on the June week. Most of that time went into grinding from an
    # already near-optimal point -- S1's objective moved 0.1% over its final
    # 40 minutes while dual infeasibility oscillated rather than fell. Dual
    # precision is not something this model consumes (duals are disabled to
    # keep memory down), so trading a decimal place of it for termination is
    # the right side of the trade.
    opts["OptimalityTol"] = 1e-5
    opts["FeasibilityTol"] = 1e-6
    # S3 scales CA transmission and BA interfaces by 10x, which widens an
    # already wide coefficient range; its crossover reported status "Numeric"
    # rather than "Sub-Optimal", i.e. numerical trouble and not merely a
    # shortage of time. Raise the numerical effort for that scenario.
    if scenario == "S3":
        opts["NumericFocus"] = 2
    if log_path is not None:
        opts["LogFile"] = str(log_path)
    if horizon == "8760":
        opts["TimeLimit"] = 24 * 3600
    else:
        opts["TimeLimit"] = float(os.environ.get("ASTR_TIME_LIMIT_S", 4 * 3600))
    return kw


def _generation_totals(solution_graph) -> pd.DataFrame:
    rows = []
    for node_name, node_data in solution_graph._node.items():
        for asset_name, asset_data in node_data.get("assets", {}).items():
            prod = asset_data.get("production", asset_data.get("net", None))
            fuel = asset_data.get("fuel", None)
            if prod is None or fuel is None:
                continue
            arr = np.asarray(prod, dtype=float).flatten()
            energy_j = float(arr.sum() * 3600.0)
            rows.append(
                {
                    "node": node_name,
                    "asset": asset_name,
                    "fuel": str(fuel).lower(),
                    "energy_J": energy_j,
                    "mean_W": float(arr.mean()) if arr.size else 0.0,
                    "hourly_W": arr,
                }
            )
    return pd.DataFrame(rows)


def _emissions_kg(gen_df: pd.DataFrame) -> float:
    ef = {
        "coal": 3.36e-7,
        "natural gas": 2.0e-7,
        "oil": 2.7e-7,
        "biomass": 9.3e-8,
        "waste": 1.0e-7,
    }
    if gen_df.empty:
        return 0.0
    co2 = 0.0
    for _, r in gen_df.iterrows():
        co2 += r["energy_J"] * ef.get(r["fuel"], 0.0)
    return float(co2)


def _line_records(solution_graph) -> pd.DataFrame:
    rows = []
    for src, adj in solution_graph._adj.items():
        for tgt, edge in adj.items():
            for handle, line in (edge.get("lines") or {}).items():
                flow = np.asarray(line.get("transmission", []), dtype=float).reshape(-1)
                cap = float(line.get("installed_capacity") or 0.0)
                if flow.size == 0:
                    continue
                binding = (cap > 0) & (flow >= 0.99 * cap)
                rows.append(
                    {
                        "source": src,
                        "target": tgt,
                        "line": handle,
                        "capacity_W": cap,
                        "mean_flow_W": float(flow.mean()),
                        "peak_flow_W": float(flow.max()),
                        "binding_hours": int(binding.sum()),
                        "n_hours": int(flow.size),
                    }
                )
    return pd.DataFrame(rows)


def _shortfall_wastage_totals(solution_graph) -> dict:
    """Sum node-level shortfall/wastage energy (J) across the whole graph.

    Region.solution() puts 'shortfall'/'wastage' directly on each node dict
    (not under 'assets') as a per-hour list in Joules (region.py's energy
    balance and objective sum these with no time_step multiplication, i.e.
    they're already per-step energy, not power).
    """
    shortfall_j = 0.0
    wastage_j = 0.0
    top_shortfall = []
    top_wastage = []
    for nid, node in solution_graph._node.items():
        sf = np.asarray(node.get("shortfall") or [], dtype=float)
        ws = np.asarray(node.get("wastage") or [], dtype=float)
        if sf.size:
            s = float(sf.sum())
            shortfall_j += s
            if s > 0:
                top_shortfall.append((nid, s))
        if ws.size:
            w = float(ws.sum())
            wastage_j += w
            if w > 0:
                top_wastage.append((nid, w))
    top_shortfall.sort(key=lambda x: x[1], reverse=True)
    top_wastage.sort(key=lambda x: x[1], reverse=True)
    return {
        "shortfall_J": shortfall_j,
        "wastage_J": wastage_j,
        "shortfall_GWh": shortfall_j / 3.6e12,
        "wastage_GWh": wastage_j / 3.6e12,
        "top_shortfall_nodes": top_shortfall[:10],
        "top_wastage_nodes": top_wastage[:10],
        "n_wastage_nodes": len(top_wastage),
    }


def _bess_records(solution_graph) -> pd.DataFrame:
    rows = []
    for nid, node in solution_graph._node.items():
        for aname, asset in (node.get("assets") or {}).items():
            if not str(aname).startswith("bess"):
                continue
            prod = np.asarray(asset.get("production", [0.0]), dtype=float).reshape(-1)
            capex = asset.get("capex", [0.0])
            built = float(np.asarray(capex, dtype=float).reshape(-1)[0]) if capex is not None else 0.0
            rows.append(
                {
                    "node": nid,
                    "planned_W": float(asset.get("capex_capacity") or 0.0),
                    "built_W": built,
                    "discharge_Wh": float(np.maximum(prod, 0.0).sum()),
                }
            )
    return pd.DataFrame(rows)


def _solve_nlg(nlg, policies, network_kw, solver_kw, label: str):
    import good
    import pyomo.environ as pyomo

    graph = good.graph.graph_from_nlg(nlg)
    print(f"  Building network [{label}] nodes={graph.number_of_nodes()} edges={graph.number_of_edges()}")
    t0 = time.time()
    network = good.optimization.network.Network(**network_kw).from_graph(graph, policies)
    network.build()
    print(f"    built in {time.time()-t0:.1f}s; steps={network.steps}")
    print(f"  Solving [{label}] ...")
    t0 = time.time()
    network.solve(**solver_kw)
    print(f"    solved in {time.time()-t0:.1f}s")
    obj = None
    try:
        obj = float(pyomo.value(network.model.objective))
    except Exception:
        obj = None
    return network.solution, obj


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
    """TOTAL built capacity for every extensible asset, keyed (region, handle).

    Deliberately broader than ``ev_charging_project.utils.extract_capex_expansion``,
    which only matches handles beginning ``optional_``. The substation batteries
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
    """
    base_ic = {
        (n["id"], h): float(a.get("installed_capacity") or 0.0)
        for n in (nlg.get("nodes") or [])
        for h, a in (n.get("assets") or {}).items()
        if a.get("extensible")
    }
    out: dict = {}
    for region, node in solution._node.items():
        for handle, asset in (node.get("assets") or {}).items():
            if (region, handle) not in base_ic:
                continue
            v = asset.get("capex", [0])
            if isinstance(v, (list, tuple)):
                w = float(v[0]) if v else 0.0
            else:
                w = float(v)
            out[(region, handle)] = max(0.0, base_ic[(region, handle)] + w)
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
    """
    g = deepcopy(nlg)
    n_built = n_frozen = 0
    built_w = 0.0
    for node in g.get("nodes") or []:
        for handle, asset in (node.get("assets") or {}).items():
            if not asset.get("extensible"):
                continue
            built = decisions.get((node["id"], handle))
            asset["capex_capacity"] = 0.0
            asset["extensible"] = False
            if built:
                asset["installed_capacity"] = float(built)
                built_w += float(built)
                n_built += 1
            else:
                n_frozen += 1
    print(f"  CAPEX fixed from stage 1: {n_built} assets held at {built_w / 1e6:,.0f} MW, "
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
    """Lock later scenarios to at least S0 renewable/storage expansion (nlg dict)."""
    g = deepcopy(nlg)
    by_id = {n["id"]: n for n in g.get("nodes") or []}
    for (region, handle), expansion_w in expansions.items():
        node = by_id.get(region)
        if not node:
            continue
        asset = (node.get("assets") or {}).get(handle)
        if not asset:
            continue
        orig_ic = float(asset.get("installed_capacity") or 0.0)
        orig_cap = float(asset.get("capex_capacity") or 0.0)
        asset["installed_capacity"] = orig_ic + float(expansion_w)
        asset["capex_capacity"] = max(0.0, orig_cap - float(expansion_w))
        if asset["capex_capacity"] == 0:
            asset["extensible"] = False
    return g


def _compact_run_rows(horizon: str, tag: str, scen: str, gen: pd.DataFrame, lines: pd.DataFrame, co2: float, obj):
    fuel = (
        gen.groupby("fuel", as_index=False)["energy_J"].sum()
        if not gen.empty
        else pd.DataFrame(columns=["fuel", "energy_J"])
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
        row[f"energy_J_{r['fuel']}"] = r["energy_J"]
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
) -> pd.DataFrame:
    import ev_charging_project.config as config
    from ev_charging_project.utils import prepare_graph, extract_capex_expansion, solution_to_dict
    import good
    from good.reload import deep_reload

    tag = week["name"] if week is not None else horizon
    if only_ba:
        tag = f"{tag}_{'+'.join(sorted(only_ba))}"
    out_root = C.ensure_dir(C.ASTR_RESULTS_DIR / tag)
    n_hours = _num_hours(horizon) if horizon != "weekly" else C.NUM_HOURS_WEEK

    deep_reload(good)
    wec_path = C.resolve_wec_json()
    with open(wec_path, encoding="utf-8") as fh:
        base_nlg = json.load(fh)
    base_nlg = _transform_nlg_profiles(base_nlg, "weekly" if week else horizon, week)
    base_nlg = _disable_solar_wind_capex(base_nlg)

    network_blob = _nest._load_network()
    hub_ids, ev_kW, tot_kW = _hub_load_arrays("weekly" if week else horizon, week)

    with open(C.POLICIES_JSON, encoding="utf-8") as fh:
        policies = json.load(fh)

    network_kw = dict(config.NETWORK_KW)
    network_kw["steps"] = (0, n_hours)
    # shortfall_cost / wastage_cost are deliberately NOT overridden here any
    # more. They are benchmark-calibrated in ev_charging_project/config.py
    # (VOLL $10,000/MWh and curtailment $1/MWh, both expressed in $/J), so
    # config.py is the single source of truth; see
    # docs/cost_calibration_methodology.tex.

    # Apply prepare_graph flags on a throwaway NX graph then... we apply in nlg after nest.
    # prepare_graph expects NetworkX; apply after from_nlg inside _solve, so replicate
    # the important flags onto nlg assets here.
    nx_tmp = good.graph.graph_from_nlg(base_nlg)
    nx_tmp = prepare_graph(nx_tmp, config)
    # write back nuclear / storage duration into base_nlg
    for nid, nd in nx_tmp._node.items():
        match = next((n for n in base_nlg["nodes"] if n["id"] == nid), None)
        if match is None:
            continue
        match["assets"] = nd.get("assets", match.get("assets"))

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
            bess_top_n=bess_top_n,
            bess_hub_override=scen_bess_override,
            only_ba=only_ba,
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
        scen_dir = C.ensure_dir(out_root / scen_out_name)

        if crossover == "auto":
            # Crossover=1 gives trustworthy per-asset values but reproducibly
            # hangs for 12+ hours in Pyomo's solution-loading step (walking
            # ~10M variables one at a time) once EV load makes the solution
            # much denser -- confirmed twice, in independent processes, on
            # S1. S0 (no EV) loads fine under Crossover=1 in ~20 min. Use
            # crossover only where it's actually affordable.
            scen_crossover = 0 if sc.get("ev") else 1
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
            solution, obj = _solve_nlg(nlg, policies, network_kw, solver_kw, f"{tag}-{scen}")
            if stage1_capex_out is not None:
                decisions = _extract_capex_decisions(solution, nlg)
                _save_stage1_capex(stage1_capex_out, scen, decisions)
                built_w = sum(v for v in decisions.values() if v)
                print(f"  stage-1 build: {sum(1 for v in decisions.values() if v)}"
                      f"/{len(decisions)} extensible assets, {built_w / 1e6:,.0f} MW"
                      f" total capacity -> {stage1_capex_out}")
            if scen == "S0" and stage1_capex is None and not no_capex:
                baseline_expansions = extract_capex_expansion(solution, good.graph.graph_from_nlg(nlg))
                _save_baseline_expansions(baseline_capex_path, baseline_expansions)
            gen = _generation_totals(solution)
            gen.drop(columns=["hourly_W"], errors="ignore").to_csv(
                scen_dir / "generation_by_asset.csv", index=False
            )
            lines = _line_records(solution)
            lines.to_csv(scen_dir / "line_flows_summary.csv", index=False)
            bess = _bess_records(solution)
            if not bess.empty:
                bess.to_csv(scen_dir / "bess_summary.csv", index=False)
            sfw = _shortfall_wastage_totals(solution)
            (scen_dir / "shortfall_wastage.json").write_text(json.dumps(sfw, indent=2), encoding="utf-8")
            print(
                f"  shortfall={sfw['shortfall_GWh']:.3f} GWh "
                f"({sfw['shortfall_J']*float(network_kw.get('shortfall_cost') or 0):.3e} $)  "
                f"wastage={sfw['wastage_GWh']:.3f} GWh"
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
        "--no-capex",
        action="store_true",
        help="Dispatch the as-built system: close every CAPEX decision so no new "
        "capacity can be built. Keeps the 4.22 GW of installed pumped hydro and "
        "0.60 GW of battery; drops the 799 optional assets. S2's substation "
        "batteries are prescribed at their per-site cap instead of being built, "
        "otherwise S2 would collapse into S1.",
    )
    args = parser.parse_args()

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
        )
    print(f"\nResults under {C.ASTR_RESULTS_DIR}")


if __name__ == "__main__":
    main()
