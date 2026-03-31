"""
run_all.py — Seasonal iteration pipeline (4 × 7-day windows).

Usage (from repository root):
    python ev_charging_project/run_all.py

For each seasonal iteration (March, June, September, December):
  1. Slice the pre-loaded graph to a 7-day window.
  2. Run baseline (no-EV) optimisation.
  3. Extract CAPEX expansion from baseline solution.
  4. Run EV scenario with CAPEX floor = baseline expansion.
  5. Post-process: generate all plots, CSVs, and a summary TXT.

Output structure:
  ev_charging_results/
    march/
      baseline/    baseline_solution_*.json, baseline_objective_*.txt
      ev/          ev_solution_*.json, ev_objective_*.txt
      data/        generation CSVs, marginal emissions CSVs, CAPEX CSVs
      plots/       all PNGs (regions/, wecc, RPS, CAPEX, emissions…)
      iteration_summary.txt
    june/        (same)
    september/   (same)
    december/    (same)
"""

import os
import sys
import gc
import time
import datetime
import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import warnings
warnings.filterwarnings('ignore')

import good
from good.reload import deep_reload

import ev_charging_project.config as config
from ev_charging_project.utils    import (
    prepare_graph,
    load_ev_profile_for_hours,
)
from ev_charging_project.run_baseline import run_baseline
from ev_charging_project.run_ev       import run_ev
from ev_charging_project.postprocess          import run_postprocess
from ev_charging_project.aggregate_postprocess import run_aggregate_postprocess


def _print_header(title, char='#'):
    bar = char * 72
    print(f'\n{bar}\n  {title}\n{bar}')


def _print_section(title):
    bar = '-' * 72
    print(f'\n{bar}\n  {title}\n{bar}')


def main():
    wall_start = time.time()
    ts_run     = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    os.makedirs(config.OUTPUT_DIR,    exist_ok=True)
    os.makedirs(config._NODEFILE_DIR, exist_ok=True)

    # -----------------------------------------------------------------------
    # 1. Load and prepare graph ONCE (profile slicing happens per-iteration)
    # -----------------------------------------------------------------------
    _print_header('STEP 1 — Load & prepare graph')
    deep_reload(good)
    graph = good.graph.graph_from_json(config.GRAPH_FILE)
    graph = prepare_graph(graph, config)

    policies = (
        good.utilities.read_json(config.POLICIES_FILE)
        if os.path.exists(config.POLICIES_FILE) else {}
    )
    print(f'  Policies loaded: {len(policies)} entries')
    print(f'  Iterations to run: {len(config.ITERATIONS)}')
    for it in config.ITERATIONS:
        print(f'    {it["name"]:12s}  start_hour={it["start_hour"]}')

    # -----------------------------------------------------------------------
    # 2. Iterate over seasons
    # -----------------------------------------------------------------------
    iteration_results = {}

    for idx, iteration in enumerate(config.ITERATIONS):
        season       = iteration['name']
        start_hour   = iteration['start_hour']
        month_label  = iteration['month']
        num_hours    = config.NUM_HOURS

        _print_header(
            f'ITERATION {idx+1}/{len(config.ITERATIONS)}  —  {month_label.upper()}  '
            f'(hours {start_hour}–{start_hour+num_hours-1})')

        # Directories for this iteration
        iter_dir      = os.path.join(config.OUTPUT_DIR, season)
        baseline_dir  = os.path.join(iter_dir, 'baseline')
        ev_dir        = os.path.join(iter_dir, 'ev')
        for d in (iter_dir, baseline_dir, ev_dir):
            os.makedirs(d, exist_ok=True)

        # Slice EV profile to this window
        _print_section(f'{month_label} — Load EV profile slice')
        ev_charging_load = load_ev_profile_for_hours(config, start_hour, num_hours)

        # -------------------------------------------------------------------
        # 2a. Baseline
        # -------------------------------------------------------------------
        _print_section(f'{month_label} — Baseline solve')
        t0 = time.time()
        baseline_solution, baseline_graph, capex_expansions = run_baseline(
            graph, policies,
            output_dir=baseline_dir,
            start_hour=start_hour,
            num_hours=num_hours,
        )
        print(f'  Baseline done in {(time.time()-t0)/60:.1f} min')

        gc.collect()
        print('  Baseline Pyomo model freed from RAM.')

        # -------------------------------------------------------------------
        # 2b. EV solve (CAPEX floor from baseline)
        # -------------------------------------------------------------------
        _print_section(f'{month_label} — EV solve')
        t0 = time.time()
        ev_solution, ev_graph = run_ev(
            graph, policies, ev_charging_load,
            output_dir=ev_dir,
            start_hour=start_hour,
            num_hours=num_hours,
            baseline_expansions=capex_expansions,
        )
        print(f'  EV done in {(time.time()-t0)/60:.1f} min')

        gc.collect()
        print('  EV Pyomo model freed from RAM.')

        # -------------------------------------------------------------------
        # 2c. Post-process
        # -------------------------------------------------------------------
        _print_section(f'{month_label} — Post-processing')

        # Retrieve objective values saved in solution JSON
        import json as _json, glob as _glob

        def _read_obj(folder, prefix):
            pattern = os.path.join(folder, f'{prefix}_objective_*.txt')
            files   = sorted(_glob.glob(pattern))
            if not files:
                return 0.0
            with open(files[-1]) as f:
                return float(f.read().strip())

        baseline_obj = _read_obj(baseline_dir, 'baseline')
        ev_obj       = _read_obj(ev_dir,       'ev')

        run_postprocess(
            baseline_solution, baseline_graph,
            ev_solution,       ev_graph,
            baseline_obj,      ev_obj,
            ev_charging_load,
            policies,
            iteration,
            iter_dir,
        )

        iteration_results[season] = {
            'baseline_obj': baseline_obj,
            'ev_obj':       ev_obj,
            'n_capex_expanded': len(capex_expansions),
        }

        # Release solution graphs before next iteration
        del baseline_solution, baseline_graph
        del ev_solution, ev_graph
        del ev_charging_load, capex_expansions
        gc.collect()
        print(f'  Memory released after {month_label} iteration.')

    # -----------------------------------------------------------------------
    # 3. Cross-iteration summary
    # -----------------------------------------------------------------------
    _print_header('ALL ITERATIONS COMPLETE')
    summary_path = os.path.join(config.OUTPUT_DIR, 'all_iterations_summary.txt')
    wall_elapsed = time.time() - wall_start

    lines = [
        f'Run completed at : {datetime.datetime.now().isoformat()}',
        f'Total wall time  : {wall_elapsed/60:.1f} min',
        '',
        f'{"Season":<14}  {"Baseline Obj":>15}  {"EV Obj":>15}  '
        f'{"Diff (EV-BL)":>15}  {"CAPEX assets":>14}',
        '-' * 75,
    ]
    for season, res in iteration_results.items():
        b, e = res['baseline_obj'], res['ev_obj']
        lines.append(
            f'{season:<14}  {b:>15.4e}  {e:>15.4e}  '
            f'{(e-b):>15.4e}  {res["n_capex_expanded"]:>14d}'
        )
    lines.append('')
    lines.append(f'Results directory: {os.path.abspath(config.OUTPUT_DIR)}')

    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print('\n'.join(lines))
    print(f'\nCross-iteration summary: {summary_path}')

    _print_header('AGGREGATE POST-PROCESS (cross-season figures)')
    run_aggregate_postprocess(config.OUTPUT_DIR)


if __name__ == '__main__':
    main()
