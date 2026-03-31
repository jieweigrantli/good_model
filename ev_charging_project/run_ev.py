"""
run_ev.py — Build and solve the EV-charging scenario.

Accepts per-iteration output_dir, start_hour, num_hours, and baseline_expansions
(dict from run_baseline) so the EV scenario cannot under-invest compared to baseline.
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
    add_ev_charging_load,
    load_ev_profile,
    apply_capex_floor,
    solution_to_dict,
)


def _print_section(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


def run_ev(graph, policies, ev_charging_load,
           output_dir=None,
           start_hour=None,
           num_hours=None,
           baseline_expansions=None):
    """
    Build and solve the EV scenario.

    Parameters
    ----------
    graph               : prepared (un-sliced) graph
    policies            : policy dict
    ev_charging_load    : 1-D W-unit numpy array (full 8760-h or sliced)
    output_dir          : folder to save ev_solution.json
    start_hour          : profile start hour
    num_hours           : window length
    baseline_expansions : dict {(region, handle): expanded_W} from run_baseline;
                          applied as minimum CAPEX floor so the EV solve cannot
                          install less capacity than the baseline chose.

    Returns
    -------
    ev_solution : solved solution graph
    ev_graph    : sliced + EV-injected graph
    """
    _print_section('RUNNING EV SCENARIO')

    if output_dir  is None: output_dir  = config.OUTPUT_DIR
    if start_hour  is None: start_hour  = getattr(config, 'START_HOUR', 0)
    if num_hours   is None: num_hours   = config.NUM_HOURS

    os.makedirs(output_dir,           exist_ok=True)
    os.makedirs(config._NODEFILE_DIR, exist_ok=True)

    steps = (0, num_hours)

    # Apply CAPEX floor from baseline before slicing / EV injection
    if baseline_expansions:
        print(f'  Applying CAPEX floor from baseline '
              f'({len(baseline_expansions)} assets)...')
        graph = apply_capex_floor(graph, baseline_expansions)

    # Slice profiles + inject EV load
    print('Building EV graph...')
    t0 = time.time()
    ev_graph = add_ev_charging_load(
        graph,
        ev_charging_load,
        start_hour=start_hour,
        num_hours=num_hours,
        california_regions=config.CALIFORNIA_REGIONS,
    )
    print(f'  EV graph ready in {time.time()-t0:.1f}s')

    # Build network
    network_kw = dict(config.NETWORK_KW)
    network_kw['steps'] = steps

    print('Building network model...')
    t0 = time.time()
    ev_network = (
        good.optimization.network.Network(**network_kw)
        .from_graph(ev_graph, policies)
    )
    ev_network.build()
    print(f'  Network built in {time.time()-t0:.1f}s')
    print(f'  steps: {ev_network.steps}')

    sk = config.SOLVER_KW.get('solver', {})
    opts = sk.get('options', {})
    sio  = sk.get('solver_io', 'lp')
    print(f'  Gurobi solver_io={sio}, '
          f'NodefileStart={opts.get("NodefileStart","(not set)")}, '
          f'NodefileDir={opts.get("NodefileDir","(not set)")}')

    # Solve
    print('Solving...')
    t0 = time.time()
    ev_network.solve(**config.SOLVER_KW)
    elapsed = time.time() - t0
    print(f'  Solve completed in {elapsed:.1f}s')

    ev_solution = ev_network.solution
    obj_val = float(pe.value(ev_network.model.objective))
    print(f'  EV objective value: {obj_val:.4e}')

    # Save solution JSON
    ts       = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    sol_path = os.path.join(output_dir, f'ev_solution_{ts}.json')
    sol_dict = solution_to_dict(ev_solution)
    with open(sol_path, 'w', encoding='utf-8') as f:
        json.dump(
            {'timestamp': ts, 'objective': obj_val, 'scenario': 'ev',
             'start_hour': start_hour, 'num_hours': num_hours, **sol_dict},
            f, default=str,
        )
    print(f'  Saved: {sol_path}')

    obj_path = os.path.join(output_dir, f'ev_objective_{ts}.txt')
    with open(obj_path, 'w') as f:
        f.write(f'{obj_val}\n')

    # Release heavy objects
    if hasattr(ev_network, 'model'):
        del ev_network.model
    if hasattr(ev_network, 'result'):
        del ev_network.result
    gc.collect()

    return ev_solution, ev_graph


if __name__ == '__main__':
    deep_reload(good)
    graph    = good.graph.graph_from_json(config.GRAPH_FILE)
    graph    = prepare_graph(graph, config)
    policies = (
        good.utilities.read_json(config.POLICIES_FILE)
        if os.path.exists(config.POLICIES_FILE) else {}
    )
    ev_load = load_ev_profile(config)
    run_ev(graph, policies, ev_load)
