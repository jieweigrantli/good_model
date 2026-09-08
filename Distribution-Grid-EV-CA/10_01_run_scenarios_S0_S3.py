"""
10_01_run_scenarios_S0_S3.py

S0–S3 nested GOOD solves.

Horizons
  four_week  (default)  concatenate 4 seasonal weeks (672 h) — CAPEX co-optimized
                        against all four weeks in one LP. Feasibility test.
  8760                  single-shot full-year LP. Do not run on 32 GB until
                        four_week succeeds; coded as the primary production path.
  weekly                four separate 168 h solves (legacy screening).

Gurobi: barrier (Method=2, Crossover=0), NodefileStart, MPS I/O.

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
    opts["BarConvTol"] = 1e-7
    opts["OptimalityTol"] = 1e-6
    opts["FeasibilityTol"] = 1e-6
    if log_path is not None:
        opts["LogFile"] = str(log_path)
    if horizon == "8760":
        opts["TimeLimit"] = 24 * 3600
    elif scenario == "S2":
        opts["TimeLimit"] = 7200
    else:
        opts["TimeLimit"] = 7200
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
) -> pd.DataFrame:
    import ev_charging_project.config as config
    from ev_charging_project.utils import prepare_graph, extract_capex_expansion, solution_to_dict
    import good
    from good.reload import deep_reload

    tag = week["name"] if week is not None else horizon
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
    # Real operating costs run ~2e-13-1e-8 $/J. The prior shortfall_cost=1e3
    # against config.py's wastage_cost=1e-6 default was a 1e9 ratio on top of
    # an already wide capacity-coefficient range, which is a scaling hazard
    # independent of topology. Keep shortfall strongly penalized relative to
    # the priciest real generator but within a narrower band of the rest of
    # the cost coefficients.
    #
    # wastage_cost was previously dropped to 1e-9 (near-free) purely to
    # narrow the shortfall/wastage coefficient ratio; that gave the solver
    # almost no reason to avoid dumping surplus energy even where a real
    # delivery path existed (measured: 2,500+ GWh of wastage over 4 weeks on
    # the corrected topology). Set it to a real, if lighter, disincentive:
    # 100x the priciest real generator's operating cost (~1e-8 $/J) but 100x
    # cheaper than shortfall, so unmet demand still costs strictly more than
    # curtailing surplus (the standard modeling convention), while wastage
    # is no longer effectively free.
    network_kw["shortfall_cost"] = 1e-2
    network_kw["wastage_cost"] = 1e-4

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
    if "S0" not in scales and baseline_capex_path.is_file():
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
        )
        if baseline_expansions and scen != "S0":
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
            if scen == "S0":
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
        out_pq = C.RUNS_8760_PARQUET if horizon == "8760" else C.RUNS_FOUR_WEEK_PARQUET
        if horizon == "weekly":
            out_pq = C.RESULTS_DIR / "S0_S3_weekly_runs.parquet"
        if out_pq.is_file():
            prev = pd.read_parquet(out_pq)
            prev = prev[~prev["tag"].eq(tag) | ~prev["scenario"].isin(compact["scenario"])]
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
    args = parser.parse_args()

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
        )
    print(f"\nResults under {C.ASTR_RESULTS_DIR}")


if __name__ == "__main__":
    main()
