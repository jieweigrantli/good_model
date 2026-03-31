"""
config.py — Central configuration for the EV charging marginal emissions project.
Edit the values in this file and run run_all.py from the repository root:

    cd E:/GitHub/good_model
    python ev_charging_project/run_all.py
"""

import os
import numpy as np

# ---------------------------------------------------------------------------
# File paths  (relative to repository root, i.e. where you run the script)
# ---------------------------------------------------------------------------
WEC_FILE            = 'Examples/WEC.json'
WEC_MODIFIED_FILE   = 'Examples/WEC_modified.json'
GRAPH_FILE          = WEC_MODIFIED_FILE
POLICIES_FILE       = 'Examples/policies.json'
OUTPUT_DIR          = 'ev_charging_results'

# EV charging load CSV  (8760-row file; sliced per iteration)
EV_CHARGING_LOAD_FILE = 'synthetic_load_8760.csv'
EV_CHARGING_LOAD_UNIT = 'kW'   # 'kW' or 'W'

# ---------------------------------------------------------------------------
# Time window  —  each iteration is one 7-day window
# ---------------------------------------------------------------------------
NUM_HOURS = 7 * 24    # 168 hours per run
STEPS     = (0, NUM_HOURS)   # always 0-indexed after profile slicing

# Seasonal iterations: (label, start_hour_in_year)
# Month-start hours (non-leap year):
#   Jan  0   Feb  744   Mar 1416   Apr 2160   May 2880   Jun 3624
#   Jul 4344  Aug 5088  Sep 5832   Oct 6552   Nov 7296   Dec 8016
ITERATIONS = [
    {'name': 'march',     'start_hour': 1416,  'month': 'March'},
    {'name': 'june',      'start_hour': 3624,  'month': 'June'},
    {'name': 'september', 'start_hour': 5832,  'month': 'September'},
    {'name': 'december',  'start_hour': 8016,  'month': 'December'},
]

# ---------------------------------------------------------------------------
# California regions in the WEC model
# ---------------------------------------------------------------------------
CALIFORNIA_REGIONS = [
    'WEC_BANC', 'WEC_CALN', 'WEC_LADW', 'WEC_SDGE', 'WECC_IID', 'WECC_SCE',
]

# ---------------------------------------------------------------------------
# Physics / modelling
# ---------------------------------------------------------------------------
TRANSMISSION_EFFICIENCY      = 0.97
BATTERY_DURATION_HOURS       = 4
PUMP_HYDRO_DURATION_HOURS    = 8

ENABLE_CAPEX_EXPANSION              = True
ENABLE_TRANSMISSION_CAPEX_EXPANSION = False
TRANSMISSION_CAPEX_LIMIT_MULTIPLIER = 1.0

# Quick CAPEX scaling correction (applied in-memory only)
APPLY_QUICK_CAPEX_FIX = True
WIND_SOLAR_MULT       = 1000.0
BATTERY_MULT          = 2.1 / (1200 / (4 * 3.6e6))

# ---------------------------------------------------------------------------
# Network / solver
# ---------------------------------------------------------------------------
NETWORK_KW = {
    'verbose': True,
    'steps': STEPS,
    'amortization_period': 31536000 * 20,  # 20 years in seconds
    'time_step': 3600,                     # 1 hour in seconds
    'shortfall_capacity': np.inf,
    'shortfall_cost': 1e-3,
    'wastage_capacity': np.inf,
    'wastage_cost': 1e-6,
}

_NODEFILE_DIR = os.path.abspath('./gurobi_nodefiles')

# solver_io:
#   'lp'  (default) — Gurobi LP reader, can MemoryError on large models
#   'mps' — sparse MPS format, lower peak RAM for large sparse LPs
SOLVER_KW = {
    'solver': {
        '_name': 'gurobi',
        'solver_io': 'mps',
        'options': {
            # 1=dual simplex, 2=barrier, 5=deterministic concurrent simplex
            'Method':       1,
            'Presolve':     2,
            'NumericFocus': 3,
            'ScaleFlag':    2,
            'Aggregate':    0,
            'NodefileStart': 0.5,
            'NodefileDir':   _NODEFILE_DIR,
            # 'Threads': 4,
            # 'TimeLimit': 7200,
        },
    },
    'tee': True,
}
