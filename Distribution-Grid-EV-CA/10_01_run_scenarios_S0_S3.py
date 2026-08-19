"""
10_01_run_scenarios_S0_S3.py

S0–S3 nested GOOD solves.

Horizons
  four_week  (default)  concatenate 4 seasonal weeks (672 h) — CAPEX co-optimized
                        against all four weeks in one LP. Feasibility test.
  8760                  single-shot full-year LP. Do not run on 32 GB until
                        four_week succeeds; coded as the primary production path.
  weekly                four separate 168 h solves (legacy screening).

Gurobi: dual simplex (Method=1), NodefileStart, MPS I/O.

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


def _solver_kw(horizon: str, scenario: str) -> dict:
    import ev_charging_project.config as config

    kw = deepcopy(config.SOLVER_KW)
    opts = kw.setdefault("solver", {}).setdefault("options", {})
    opts["Method"] = 1
    opts["Presolve"] = 2
    opts["NodefileStart"] = 0.5
    opts.setdefault("NodefileDir", os.path.abspath("./gurobi_nodefiles"))
    opts["NumericFocus"] = 3
    opts["ScaleFlag"] = 2
    if horizon == "8760":
        opts["TimeLimit"] = 24 * 3600
    elif scenario == "S2":
        opts["TimeLimit"] = 7200
    else:
        opts["TimeLimit"] = 3600
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

    network_blob = _nest._load_network()
    hub_ids, ev_kW, tot_kW = _hub_load_arrays("weekly" if week else horizon, week)

    with open(C.POLICIES_JSON, encoding="utf-8") as fh:
        policies = json.load(fh)

    network_kw = dict(config.NETWORK_KW)
    network_kw["steps"] = (0, n_hours)
    network_kw["shortfall_cost"] = 1e3

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
    baseline_expansions = None

    for scen, sc in scales.items():
        print(f"\n=== {tag.upper()} / {scen}  horizon={horizon} hours={n_hours} ===")
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
        )
        if baseline_expansions and scen != "S0":
            nlg = _apply_capex_floor_nlg(nlg, baseline_expansions)

        scen_dir = C.ensure_dir(out_root / scen)
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
                    "scenario": scen,
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
            solver_kw = _solver_kw(horizon, scen)
            solution, obj = _solve_nlg(nlg, policies, network_kw, solver_kw, f"{tag}-{scen}")
            if scen == "S0":
                baseline_expansions = extract_capex_expansion(solution, good.graph.graph_from_nlg(nlg))
            gen = _generation_totals(solution)
            gen.drop(columns=["hourly_W"], errors="ignore").to_csv(
                scen_dir / "generation_by_asset.csv", index=False
            )
            lines = _line_records(solution)
            lines.to_csv(scen_dir / "line_flows_summary.csv", index=False)
            bess = _bess_records(solution)
            if not bess.empty:
                bess.to_csv(scen_dir / "bess_summary.csv", index=False)
            co2 = _emissions_kg(gen)
            obj_path.write_text(f"objective={obj}\nco2_kg={co2}\nn_hours={n_hours}\n", encoding="utf-8")
            if save_json:
                with open(scen_dir / "solution.json", "w", encoding="utf-8") as fh:
                    json.dump(solution_to_dict(solution), fh)
            compact_rows.append(_compact_run_rows(horizon, tag, scen, gen, lines, co2, obj))
            summary_rows.append(
                {
                    "tag": tag,
                    "horizon": horizon,
                    "scenario": scen,
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
                    "scenario": scen,
                    "status": f"error: {exc}",
                    "n_nodes": n_nodes,
                    "n_edges": n_edges,
                    "n_hours": n_hours,
                }
            )
            print(f"  ERROR: {exc}")
        gc.collect()

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_root / "scenario_summary.csv", index=False)
    if compact_rows:
        C.ensure_dir(C.RESULTS_DIR)
        compact = pd.DataFrame(compact_rows)
        out_pq = C.RUNS_8760_PARQUET if horizon == "8760" else C.RUNS_FOUR_WEEK_PARQUET
        if horizon == "weekly":
            out_pq = C.RESULTS_DIR / "S0_S3_weekly_runs.parquet"
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
        )
    print(f"\nResults under {C.ASTR_RESULTS_DIR}")


if __name__ == "__main__":
    main()
