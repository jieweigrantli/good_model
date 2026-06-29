"""
run_showcase.py — Multi-scenario EV charging showcase pipeline.

Usage (from repository root):
    python ev_charging_showcase/run_showcase.py
    python ev_charging_showcase/run_showcase.py --scenario ev_2m_2025
    python ev_charging_showcase/run_showcase.py --postprocess-only

For each scenario (June week, start_hour=3624, 168 hours):
  1. Shared baseline solve (no EV, CAPEX on) — once per run
  2. EV solve with scaled synthetic_load_8760.csv
  3. Post-process: same plots/CSVs/JSONs as ev_charging_project

Output: ev_charging_showcase_results/<scenario>/
"""

from __future__ import annotations

import argparse
import gc
import glob
import json
import os
import sys
import time
import datetime
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import good
from good.reload import deep_reload

import ev_charging_showcase.config as config
from ev_charging_showcase.utils import (
    copy_baseline_outputs,
    load_scaled_ev_profile,
    lock_capex_to_baseline,
    mirror_baseline_as_ev,
    scenario_iteration_cfg,
    write_scenario_meta,
    write_showcase_manifest,
)
from ev_charging_project.utils import prepare_graph
from ev_charging_project.run_baseline import run_baseline
from ev_charging_project.run_ev import run_ev
from ev_charging_project.postprocess import run_postprocess
from ev_charging_showcase.aggregate_showcase import run_aggregate_showcase


def _print_header(title: str, char: str = "#") -> None:
    bar = char * 72
    print(f"\n{bar}\n  {title}\n{bar}")


def _read_obj(folder: str, prefix: str) -> float:
    pattern = os.path.join(folder, f"{prefix}_objective_*.txt")
    files = sorted(glob.glob(pattern))
    if not files:
        return 0.0
    with open(files[-1], encoding="utf-8") as f:
        return float(f.read().strip())


def _latest_json(folder: str, prefix: str) -> str | None:
    files = sorted(glob.glob(os.path.join(folder, f"{prefix}_solution_*.json")))
    return files[-1] if files else None


def main() -> None:
    parser = argparse.ArgumentParser(description="EV charging interactive showcase")
    parser.add_argument(
        "--scenario",
        help="Run a single scenario name (default: all)",
    )
    parser.add_argument(
        "--postprocess-only",
        action="store_true",
        help="Skip solves; post-process from existing JSON outputs",
    )
    parser.add_argument(
        "--skip-aggregate",
        action="store_true",
        help="Skip cross-scenario aggregate figures",
    )
    parser.add_argument(
        "--force-baseline",
        action="store_true",
        help="Re-solve the shared baseline even if one already exists",
    )
    args = parser.parse_args()

    wall_start = time.time()
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    os.makedirs(config._NODEFILE_DIR, exist_ok=True)

    scenarios = list(config.SCENARIOS)
    if args.scenario:
        scenarios = [s for s in scenarios if s["name"] == args.scenario]
        if not scenarios:
            raise SystemExit(f"Unknown scenario: {args.scenario!r}")

    start_hour = config.JUNE_START_HOUR
    num_hours = config.NUM_HOURS

    _print_header("EV CHARGING INTERACTIVE SHOWCASE")
    print(f"  Window: {config.WINDOW_LABEL}")
    print(f"  start_hour={start_hour}, num_hours={num_hours}")
    print(f"  Scenarios: {[s['name'] for s in scenarios]}")
    print(f"  Output: {os.path.abspath(config.OUTPUT_DIR)}")

    shared_baseline_dir = os.path.join(config.OUTPUT_DIR, "_shared_baseline")
    manifest_rows: list[dict] = []

    if not args.postprocess_only:
        _print_header("STEP 1 — Load & prepare graph")
        deep_reload(good)
        graph = good.graph.graph_from_json(config.GRAPH_FILE)
        graph = prepare_graph(graph, config)
        policies = (
            good.utilities.read_json(config.POLICIES_FILE)
            if os.path.exists(config.POLICIES_FILE)
            else {}
        )

        existing_baseline = _latest_json(shared_baseline_dir, "baseline")
        if existing_baseline and not args.force_baseline:
            _print_header("STEP 2 — Reuse existing shared baseline (no EV)")
            from ev_charging_project.utils import (
                load_solution_json,
                slice_graph_profiles,
                extract_capex_expansion,
            )

            print(f"  Loading: {existing_baseline}")
            baseline_solution, b_meta = load_solution_json(existing_baseline)
            baseline_graph = slice_graph_profiles(
                graph, start_hour=start_hour, num_hours=num_hours
            )
            capex_expansions = extract_capex_expansion(baseline_solution, baseline_graph)
            baseline_obj_shared = float(
                b_meta.get("objective") or _read_obj(shared_baseline_dir, "baseline")
            )
            print(
                f"  Reused baseline: obj={baseline_obj_shared:.4e}, "
                f"{len(capex_expansions)} CAPEX assets "
                f"(use --force-baseline to re-solve)"
            )
        else:
            _print_header("STEP 2 — Shared baseline solve (no EV)")
            baseline_solution, baseline_graph, capex_expansions = run_baseline(
                graph,
                policies,
                output_dir=shared_baseline_dir,
                start_hour=start_hour,
                num_hours=num_hours,
            )
            baseline_obj_shared = _read_obj(shared_baseline_dir, "baseline")
        print(f"  Shared baseline objective: {baseline_obj_shared:.4e}")

        import numpy as np

        n_ev_solves = sum(1 for s in scenarios if float(s["ev_scale"]) != 0.0)
        print(
            f"  Plan: 1 baseline run + {n_ev_solves} EV solve(s) "
            f"= {1 + n_ev_solves} total solves (no per-scenario baseline re-run)"
        )

        for idx, scenario in enumerate(scenarios):
            _print_header(
                f"SCENARIO {idx + 1}/{len(scenarios)} — {scenario['label']} ({scenario['year']})"
            )
            scenario_dir = os.path.join(config.OUTPUT_DIR, scenario["name"])
            baseline_dir = os.path.join(scenario_dir, "baseline")
            ev_dir = os.path.join(scenario_dir, "ev")
            for d in (scenario_dir, baseline_dir, ev_dir):
                os.makedirs(d, exist_ok=True)

            # Every scenario reuses the single baseline run (copied in for a
            # self-contained folder); the baseline is never re-solved.
            copy_baseline_outputs(shared_baseline_dir, baseline_dir)
            baseline_obj = baseline_obj_shared

            if float(scenario["ev_scale"]) == 0.0:
                # No-EV reference: reuse the baseline solution directly (no solve).
                print("  No-EV scenario: reusing baseline solution (no EV solve).")
                ev_charging_load = np.zeros(num_hours, dtype=float)
                ev_solution, ev_graph = baseline_solution, baseline_graph
                ev_obj = baseline_obj
                mirror_baseline_as_ev(baseline_dir, ev_dir)
            else:
                # EV solve on the baseline grid with CAPEX frozen at baseline.
                locked_graph = lock_capex_to_baseline(graph, capex_expansions)
                ev_charging_load = load_scaled_ev_profile(
                    scenario, start_hour, num_hours
                )
                t0 = time.time()
                ev_solution, ev_graph = run_ev(
                    locked_graph,
                    policies,
                    ev_charging_load,
                    output_dir=ev_dir,
                    start_hour=start_hour,
                    num_hours=num_hours,
                    baseline_expansions=None,
                )
                print(f"  EV solve done in {(time.time() - t0) / 60:.1f} min")
                ev_obj = _read_obj(ev_dir, "ev")
                del locked_graph

            iter_cfg = scenario_iteration_cfg(scenario, start_hour, num_hours)
            run_postprocess(
                baseline_solution,
                baseline_graph,
                ev_solution,
                ev_graph,
                baseline_obj,
                ev_obj,
                ev_charging_load,
                policies,
                iter_cfg,
                scenario_dir,
            )

            meta_path = write_scenario_meta(
                scenario_dir,
                scenario,
                baseline_obj,
                ev_obj,
                {
                    "baseline_solution": _latest_json(baseline_dir, "baseline"),
                    "ev_solution": _latest_json(ev_dir, "ev"),
                    "plots_dir": os.path.join(scenario_dir, "plots"),
                    "data_dir": os.path.join(scenario_dir, "data"),
                },
            )
            manifest_rows.append(
                {
                    "name": scenario["name"],
                    "label": scenario["label"],
                    "year": scenario["year"],
                    "fleet_millions": scenario["fleet_millions"],
                    "ev_scale": scenario["ev_scale"],
                    "baseline_objective": baseline_obj,
                    "ev_objective": ev_obj,
                    "scenario_meta": meta_path,
                }
            )

            if ev_solution is not baseline_solution:
                del ev_solution, ev_graph
            del ev_charging_load
            gc.collect()

        del baseline_solution, baseline_graph, capex_expansions, graph
        gc.collect()

    else:
        from ev_charging_project.utils import load_solution_json

        deep_reload(good)
        raw_graph = good.graph.graph_from_json(config.GRAPH_FILE)
        data_graph = prepare_graph(raw_graph, config)
        policies = (
            good.utilities.read_json(config.POLICIES_FILE)
            if os.path.exists(config.POLICIES_FILE)
            else {}
        )

        for scenario in scenarios:
            scenario_dir = os.path.join(config.OUTPUT_DIR, scenario["name"])
            baseline_dir = os.path.join(scenario_dir, "baseline")
            ev_dir = os.path.join(scenario_dir, "ev")
            b_path = _latest_json(baseline_dir, "baseline")
            e_path = _latest_json(ev_dir, "ev")
            if not b_path or not e_path:
                print(f"  Skipping {scenario['name']}: missing JSON")
                continue
            baseline_solution, b_meta = load_solution_json(b_path)
            ev_solution, e_meta = load_solution_json(e_path)
            from ev_charging_project.utils import slice_graph_profiles, add_ev_charging_load

            baseline_graph = slice_graph_profiles(
                data_graph, start_hour=start_hour, num_hours=num_hours
            )
            ev_load = load_scaled_ev_profile(scenario, start_hour, num_hours)
            ev_graph = add_ev_charging_load(
                data_graph,
                ev_load,
                start_hour=start_hour,
                num_hours=num_hours,
                california_regions=config.CALIFORNIA_REGIONS,
            )
            baseline_obj = _read_obj(baseline_dir, "baseline")
            ev_obj = _read_obj(ev_dir, "ev")
            iter_cfg = scenario_iteration_cfg(scenario, start_hour, num_hours)
            run_postprocess(
                baseline_solution,
                baseline_graph,
                ev_solution,
                ev_graph,
                baseline_obj,
                ev_obj,
                ev_load,
                policies,
                iter_cfg,
                scenario_dir,
            )
            manifest_rows.append({"name": scenario["name"], "label": scenario["label"]})

    manifest_path = write_showcase_manifest(
        config.OUTPUT_DIR,
        scenarios,
        shared_baseline_dir,
        manifest_rows,
    )
    print(f"\nShowcase manifest: {manifest_path}")

    if not args.skip_aggregate:
        _print_header("AGGREGATE POST-PROCESS (cross-scenario figures)")
        run_aggregate_showcase(config.OUTPUT_DIR)

    elapsed = (time.time() - wall_start) / 60
    _print_header(f"SHOWCASE COMPLETE — {elapsed:.1f} min wall time")


if __name__ == "__main__":
    main()
