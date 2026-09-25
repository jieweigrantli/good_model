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
# Costs in this model are [$/J]. 1 MWh = 3.6e9 J, so $/MWh = ($/J) * 3.6e9.
# The generator operating costs carried in the WEC graph are correctly scaled
# in those terms (nuclear 3.09e-9 $/J = $11.13/MWh; most expensive unit
# 1.07e-8 $/J = $38.52/MWh), but the shortfall/wastage penalties were not:
# the previous 1e-3 / 1e-6 defaults correspond to $3.6M/MWh and $3,600/MWh.
# Together they made up >99.9% of the objective on the 672 h nested LP, left
# generation cost (which is what sets dispatch, and therefore CO2) at ~0.04%
# of the objective, and stretched the objective coefficient range across ~11
# orders of magnitude. See docs/cost_calibration_methodology.tex for the
# benchmarks and derivations behind the values below.
_J_PER_MWH = 3.6e9

NETWORK_KW = {
    'verbose': True,
    'steps': STEPS,
    'amortization_period': 31536000 * 20,  # 20 years in seconds
    'time_step': 3600,                     # 1 hour in seconds
    'shortfall_capacity': np.inf,
    # Value of lost load. $10,000/MWh is GenX's default VOLL and sits in the
    # lower-middle of the $9,000-$45,000/MWh literature range for developed
    # economies. Still ~260x the priciest generator, so load is served
    # whenever it is physically possible to serve it.
    'shortfall_cost': 10_000 / _J_PER_MWH,   # 2.78e-6 $/J
    'wastage_capacity': np.inf,
    # Curtailment of surplus is conventionally valued at $0/MWh. A strictly
    # positive value is still needed here as a tie-breaker (a zero cost leaves
    # flat directions the barrier method smears across), so use $1/MWh -- the
    # low end of the curtailment range, far below any generator's cost, so it
    # breaks ties without steering dispatch. $25/MWh (wind PTC opportunity
    # cost) is the natural alternative for a sensitivity run.
    'wastage_cost': 1 / _J_PER_MWH,          # 2.78e-10 $/J
    # Duals are only consumed by visualizations.py (clearing_price); the ASTR
    # meso pipeline uses primal quantities only. Importing them costs a
    # name -> value entry per constraint (multi-GB at this model size) and
    # makes Region.solution() rebuild a dict over every dual, per node.
    'extract_duals': False,
}

_NODEFILE_DIR = os.path.abspath('./gurobi_nodefiles')

# solver_io:
#   'lp'     (Pyomo default) — Gurobi LP reader, can MemoryError on large models
#   'mps'    — file-based; Gurobi reads the model back from disk, so gurobipy
#              defers creating Var/Constr objects until attributes are queried.
#              GUROBI_RUN then builds {VarName: X} (and, when duals are
#              imported, {ConstrName: Pi}) dicts and serializes them to a text
#              .sol file that Pyomo re-parses. That retrieval step is what
#              MemoryErrors on the ~12M var / ~14M constraint nested LP --
#              hence extract_duals=False above, which removes the constraint
#              half of it entirely.
#   'direct' — builds the model in-process via gurobipy; avoids the name-keyed
#              dicts and text round-trip on retrieval (with save_results=False),
#              BUT measured WORSE here: translating the Pyomo model into
#              gurobipy object-by-object, while holding ComponentMaps of 11.9M
#              vars and 14.2M constraints plus both model copies, used 24+ GB
#              and 74+ min *before Gurobi even started solving* on the 672 h
#              model. Only viable at smaller model sizes.
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
