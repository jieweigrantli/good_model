"""
run_baseline.py — Build and solve the baseline (no-EV) scenario.

Can be called directly or imported by run_all.py.
Signature updated to accept per-iteration output_dir, start_hour, num_hours.
"""

import os
import sys
import gc
import json
import time
import datetime
import numpy as np
import pyomo.environ as pe

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import good
from good.reload import deep_reload
import ev_charging_project.config as config
from ev_charging_project.utils import (
    prepare_graph,
    slice_graph_profiles,
    extract_capex_expansion,
    solution_to_dict,
)


def _print_section(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


def run_baseline(graph, policies,
                 output_dir=None,
                 start_hour=None,
                 num_hours=None):
    """
    Build and solve the baseline (no-EV) scenario.

    Parameters
    ----------
    graph       : prepared (but un-sliced) graph object
    policies    : policy dict
    output_dir  : folder to save baseline_solution.json; defaults to config.OUTPUT_DIR
    start_hour  : profile start hour; defaults to config.START_HOUR
    num_hours   : window length; defaults to config.NUM_HOURS

    Returns
    -------
    baseline_solution : solved solution graph
    baseline_graph    : sliced graph used for this solve
    capex_expansions  : dict {(region, handle): expanded_W}
    """
    _print_section('RUNNING BASELINE SCENARIO (No EV)')

    if output_dir  is None: output_dir  = config.OUTPUT_DIR
    if start_hour  is None: start_hour  = getattr(config, 'START_HOUR', 0)
    if num_hours   is None: num_hours   = config.NUM_HOURS

    os.makedirs(output_dir,        exist_ok=True)
    os.makedirs(config._NODEFILE_DIR, exist_ok=True)

    # Build network_kw using the correct steps for this window
    steps = (0, num_hours)
    network_kw = dict(config.NETWORK_KW)
    network_kw['steps'] = steps

    # Slice profiles
    print('Slicing graph profiles...')
    t0 = time.time()
    baseline_graph = slice_graph_profiles(graph, start_hour=start_hour, num_hours=num_hours)
    print(f'  Profile slicing done in {time.time()-t0:.1f}s')

    # Build network
    print('Building network model...')
    t0 = time.time()
    baseline_network = (
        good.optimization.network.Network(**network_kw)
        .from_graph(baseline_graph, policies)
    )
    baseline_network.build()
    print(f'  Network built in {time.time()-t0:.1f}s')
    print(f'  steps: {baseline_network.steps}')

    sk = config.SOLVER_KW.get('solver', {})
    opts = sk.get('options', {})
    sio  = sk.get('solver_io', 'lp')
    print(f'  Gurobi solver_io={sio}, '
          f'NodefileStart={opts.get("NodefileStart","(not set)")}, '
          f'NodefileDir={opts.get("NodefileDir","(not set)")}')

    # Solve
    print('Solving...')
    t0 = time.time()
    baseline_network.solve(**config.SOLVER_KW)
    elapsed = time.time() - t0
    print(f'  Solve completed in {elapsed:.1f}s')

    baseline_solution = baseline_network.solution
    obj_val = float(pe.value(baseline_network.model.objective))
    print(f'  Baseline objective value: {obj_val:.4e}')

    # Extract CAPEX expansions before we delete the model
    capex_expansions = extract_capex_expansion(baseline_solution, baseline_graph)
    n_expanded = len(capex_expansions)
    total_mw   = sum(capex_expansions.values()) / 1e6
    print(f'  CAPEX expansion: {n_expanded} assets, {total_mw:.1f} MW total')

    # Save solution JSON
    ts       = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    sol_path = os.path.join(output_dir, f'baseline_solution_{ts}.json')
    sol_dict = solution_to_dict(baseline_solution)
    with open(sol_path, 'w', encoding='utf-8') as f:
        json.dump(
            {'timestamp': ts, 'objective': obj_val, 'scenario': 'baseline',
             'start_hour': start_hour, 'num_hours': num_hours, **sol_dict},
            f, default=str,
        )
    print(f'  Saved: {sol_path}')

    # Save objective txt
    obj_path = os.path.join(output_dir, f'baseline_objective_{ts}.txt')
    with open(obj_path, 'w') as f:
        f.write(f'{obj_val}\n')

    # Free heavy Pyomo objects
    if hasattr(baseline_network, 'model'):
        del baseline_network.model
    if hasattr(baseline_network, 'result'):
        del baseline_network.result
    gc.collect()

    return baseline_solution, baseline_graph, capex_expansions


if __name__ == '__main__':
    deep_reload(good)
    graph    = good.graph.graph_from_json(config.GRAPH_FILE)
    graph    = prepare_graph(graph, config)
    policies = (
        good.utilities.read_json(config.POLICIES_FILE)
        if os.path.exists(config.POLICIES_FILE) else {}
    )
    run_baseline(graph, policies)
