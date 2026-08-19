"""
10_01_run_scenarios_S0_S3.py

Seasonal weekly S0–S3 solves on a two-tier nested graph:
  S0 — baseline WECC BA week (no meso EV)
  S1 — BA week + meso EV, tight delivery, no new BESS
  S2 — S1 + endogenous BESS on meso hubs
  S3 — meso EV with relaxed meso/interface capacities

Run from repository root or this folder (needs Gurobi + good).
"""

from __future__ import annotations

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

# Reuse builders from 09_02
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "nest_meso", PKG / "09_02_nest_meso_in_good.py"
)
_nest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nest)


def _add_meso_to_sliced_graph(
    graph,
    hubs: pd.DataFrame,
    edges: pd.DataFrame,
    interfaces: pd.DataFrame,
    meso_hourly_kW: np.ndarray,
    hub_ids: list[str],
    include_bess: bool,
    scale_meso: float,
    scale_iface: float,
    bess_top_n: int = 0,
):
    """Mutate a NetworkX DiGraph (GOOD) by adding meso nodes/edges.

    bess_top_n: if >0, only top-N peak-EV nodes get Store assets; if <=0, all
    nodes with EV peak > 0 get Store (full substation-level S2).
    """
    hub_index = {h: i for i, h in enumerate(hub_ids)}
    peaks = {
        hid: float(np.asarray(meso_hourly_kW[hub_index[hid]]).max())
        for hid in hub_ids
        if hid in hub_index
    }
    if bess_top_n and bess_top_n > 0:
        bess_hubs = set(
            sorted(peaks, key=peaks.get, reverse=True)[:bess_top_n]
        )
    else:
        bess_hubs = {hid for hid, p in peaks.items() if p > 0}

    for _, row in hubs.iterrows():
        hid = row["hub_id"]
        if hid in graph._node:
            continue
        i = hub_index[hid]
        profile = meso_hourly_kW[i]
        assets = {f"ev_load_{hid}": _nest.make_ev_load_asset(hid, profile)}
        if include_bess and hid in bess_hubs:
            peak_W = float(np.asarray(profile).max()) * 1000.0
            assets[f"bess_{hid}"] = _nest.make_store_asset(hid, max(peak_W * 0.5, 1e6))
        graph.add_node(hid, **{"_class": "Region", "assets": assets, "profiles": {}})

    def _add_link(src, tgt, cap, handle):
        line = _nest.make_transmission_line(src, tgt, cap, handle)
        if graph.has_edge(src, tgt):
            graph[src][tgt].setdefault("lines", {})[handle] = line
        else:
            graph.add_edge(src, tgt, **{"_class": "Link", "lines": {handle: line}})

    for _, e in edges.iterrows():
        cap = float(e["installed_capacity_W"]) * scale_meso
        s, t = e["source"], e["target"]
        _add_link(s, t, cap, f"meso_{s}_{t}_fwd")
        _add_link(t, s, cap, f"meso_{t}_{s}_rev")

    for _, e in interfaces.iterrows():
        hid, ba = e["hub_id"], e["parent_ba"]
        cap = float(e["interface_capacity_W"]) * scale_iface
        _add_link(ba, hid, cap, f"iface_{ba}_{hid}_to_hub")
        _add_link(hid, ba, cap, f"iface_{hid}_{ba}_to_ba")

    return graph


def _solve_graph(graph, policies, network_kw, solver_kw, label: str):
    import good
    import pyomo.environ as pyomo

    print(f"  Building network [{label}] ...")
    t0 = time.time()
    network = good.optimization.network.Network(**network_kw).from_graph(graph, policies)
    network.build()
    print(f"    built in {time.time()-t0:.1f}s; steps={network.steps}")
    print(f"  Solving [{label}] ...")
    t0 = time.time()
    network.solve(**solver_kw)
    print(f"    solved in {time.time()-t0:.1f}s")
    solution = network.solution
    obj = None
    try:
        obj = float(pyomo.value(network.model.objective))
    except Exception:
        obj = None
    return solution, obj


def _generation_totals(solution_graph) -> pd.DataFrame:
    """Sum producer energy (Wh) by fuel from a GOOD solution graph."""
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


def _objective_value(network) -> float | None:
    for attr in ("objective", "objective_value", "obj"):
        if hasattr(network, attr):
            try:
                return float(getattr(network, attr))
            except Exception:
                pass
    # try pyomo model
    model = getattr(network, "model", None)
    if model is not None and hasattr(model, "objective"):
        try:
            return float(pyomo.value(model.objective))
        except Exception:
            pass
    return None


def run_season(season: dict, scales: dict, dry_run: bool = False, skip_existing: bool = False) -> None:
    import good
    from good.reload import deep_reload
    import ev_charging_project.config as config
    from ev_charging_project.utils import prepare_graph, slice_graph_profiles, solution_to_dict

    name = season["name"]
    start_hour = int(season["start_hour"])
    out_root = C.ensure_dir(C.ASTR_RESULTS_DIR / name)

    hubs = pd.read_csv(C.MESO_DIR / "meso_nodes.csv")
    edges = pd.read_csv(C.MESO_DIR / "meso_edges.csv")
    interfaces = pd.read_csv(C.MESO_DIR / "meso_ba_interfaces.csv")
    hub_ids = [str(h) for h in np.load(C.MESO_DIR / "seasonal" / name / "meso_hub_ids.npy")]
    meso_load = np.load(C.MESO_DIR / "seasonal" / name / "meso_hourly_kW.npy")

    deep_reload(good)
    base = good.graph.graph_from_json(str(REPO / "Examples" / "WEC_modified.json"))
    base = prepare_graph(base, config)
    with open(REPO / "Examples" / "policies.json", encoding="utf-8") as fh:
        policies = json.load(fh)

    network_kw = dict(config.NETWORK_KW)
    network_kw["steps"] = (0, C.NUM_HOURS_WEEK)
    # Raise shortfall penalty so meso EV load is met by generation/transfers
    # rather than cheap unmet-load slack (config default is ~1e-3).
    network_kw["shortfall_cost"] = 1e3
    solver_kw_base = deepcopy(config.SOLVER_KW)
    solver_kw_base.setdefault("solver", {}).setdefault("options", {})
    # Default cap for S0/S1/S3; S2 overrides with a longer limit below.
    solver_kw_base["solver"]["options"].setdefault("TimeLimit", 1200)

    summary_rows = []

    for scen, sc in scales.items():
        print(f"\n=== {name.upper()} / {scen} ===")
        solver_kw = deepcopy(solver_kw_base)
        # Full substation-level S2: BESS candidates on every node with EV peak > 0.
        # Pass --bess-top-n N to restore selective siting if needed.
        bess_top_n = int(os.environ.get("ASTR_BESS_TOP_N", "0"))
        if scen == "S2":
            solver_kw["solver"]["options"]["TimeLimit"] = 7200
            solver_kw["solver"]["options"]["Method"] = 1
        g = slice_graph_profiles(base, start_hour=start_hour, num_hours=C.NUM_HOURS_WEEK)
        if sc.get("ev"):
            g = _add_meso_to_sliced_graph(
                g,
                hubs,
                edges,
                interfaces,
                meso_load,
                hub_ids,
                include_bess=bool(sc.get("bess")),
                scale_meso=float(sc["meso"]),
                scale_iface=float(sc["interface"]),
                bess_top_n=bess_top_n,
            )
        scen_dir = C.ensure_dir(out_root / scen)
        if dry_run:
            print(f"  dry-run: nodes={g.number_of_nodes()} edges={g.number_of_edges()}")
            summary_rows.append(
                {
                    "season": name,
                    "scenario": scen,
                    "status": "dry_run",
                    "n_nodes": g.number_of_nodes(),
                    "n_edges": g.number_of_edges(),
                }
            )
            continue

        # Resume: reuse prior successful solve if present
        obj_path = scen_dir / "objective.txt"
        sol_path = scen_dir / "solution.json"
        if skip_existing and sol_path.is_file() and obj_path.is_file():
            obj = None
            co2 = None
            for line in obj_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("objective="):
                    try:
                        obj = float(line.split("=", 1)[1])
                    except ValueError:
                        obj = None
                if line.startswith("co2_kg="):
                    try:
                        co2 = float(line.split("=", 1)[1])
                    except ValueError:
                        co2 = None
            summary_rows.append(
                {
                    "season": name,
                    "scenario": scen,
                    "status": "ok",
                    "objective": obj,
                    "co2_kg": co2,
                    "n_nodes": g.number_of_nodes(),
                    "n_edges": g.number_of_edges(),
                }
            )
            print(f"  skip-existing: co2_kg={co2} objective={obj}")
            continue

        try:
            solution, obj = _solve_graph(g, policies, network_kw, solver_kw, f"{name}-{scen}")
            sol_dict = solution_to_dict(solution)
            with open(scen_dir / "solution.json", "w", encoding="utf-8") as fh:
                json.dump(sol_dict, fh)
            gen = _generation_totals(solution)
            gen.to_csv(scen_dir / "generation_by_asset.csv", index=False)
            co2 = _emissions_kg(gen)
            with open(scen_dir / "objective.txt", "w", encoding="utf-8") as fh:
                fh.write(f"objective={obj}\nco2_kg={co2}\n")
            summary_rows.append(
                {
                    "season": name,
                    "scenario": scen,
                    "status": "ok",
                    "objective": obj,
                    "co2_kg": co2,
                    "n_nodes": g.number_of_nodes(),
                    "n_edges": g.number_of_edges(),
                }
            )
            print(f"  co2_kg={co2:.3e} objective={obj}")
        except Exception as exc:
            err = traceback.format_exc()
            (scen_dir / "error.txt").write_text(err, encoding="utf-8")
            summary_rows.append(
                {
                    "season": name,
                    "scenario": scen,
                    "status": f"error: {exc}",
                    "n_nodes": g.number_of_nodes(),
                    "n_edges": g.number_of_edges(),
                }
            )
            print(f"  ERROR: {exc}")
        gc.collect()

    pd.DataFrame(summary_rows).to_csv(out_root / "scenario_summary.csv", index=False)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build graphs only; do not call Gurobi",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Reuse solution.json/objective.txt when already present",
    )
    parser.add_argument(
        "--seasons",
        nargs="*",
        default=None,
        help="Subset of seasons (default: all four)",
    )
    args = parser.parse_args()

    scales_path = C.MESO_DIR / "scenario_capacity_scales.json"
    with open(scales_path, encoding="utf-8") as fh:
        scales = json.load(fh)

    seasons = C.SEASONAL_WEEKS
    if args.seasons:
        seasons = [s for s in seasons if s["name"] in args.seasons]

    # Check gurobi availability early
    if not args.dry_run:
        try:
            import gurobipy  # noqa: F401
        except Exception as exc:
            print(f"Gurobi not available ({exc}); re-run with --dry-run or install Gurobi.")
            print("Writing blocker note and exiting.")
            C.ensure_dir(C.ASTR_RESULTS_DIR)
            (C.ASTR_RESULTS_DIR / "GUROBI_BLOCKER.txt").write_text(
                f"Gurobi import failed: {exc}\n"
                "BA-level results in repo ev_charging_results/ can still be used "
                "for 10_03/10_04 diagnostics.\n",
                encoding="utf-8",
            )
            # still dry-build one season for structural check
            args.dry_run = True

    for season in seasons:
        run_season(
            season, scales, dry_run=args.dry_run, skip_existing=args.skip_existing
        )

    print(f"\nResults under {C.ASTR_RESULTS_DIR}")


if __name__ == "__main__":
    main()
