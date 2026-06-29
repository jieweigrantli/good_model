"""
config.py — Showcase scenario definitions.

Reuses physics, solver, and graph paths from ev_charging_project.config.
Each scenario is one June week (168 h, start_hour=3624) with a different
EV fleet scale applied to synthetic_load_8760.csv.

Base profile (scale=1.0) represents 2.5M EVs. Per-scenario ev_scale is
therefore fleet_millions / 2.5.
"""

from ev_charging_project import config as base

# Re-export shared settings used by run_baseline / run_ev / postprocess
GRAPH_FILE = base.GRAPH_FILE
POLICIES_FILE = base.POLICIES_FILE
EV_CHARGING_LOAD_FILE = base.EV_CHARGING_LOAD_FILE
EV_CHARGING_LOAD_UNIT = base.EV_CHARGING_LOAD_UNIT
CALIFORNIA_REGIONS = base.CALIFORNIA_REGIONS
NUM_HOURS = base.NUM_HOURS
STEPS = base.STEPS
NETWORK_KW = base.NETWORK_KW
SOLVER_KW = base.SOLVER_KW
_NODEFILE_DIR = base._NODEFILE_DIR
ENABLE_CAPEX_EXPANSION = base.ENABLE_CAPEX_EXPANSION
ENABLE_TRANSMISSION_CAPEX_EXPANSION = base.ENABLE_TRANSMISSION_CAPEX_EXPANSION
TRANSMISSION_CAPEX_LIMIT_MULTIPLIER = base.TRANSMISSION_CAPEX_LIMIT_MULTIPLIER
APPLY_QUICK_CAPEX_FIX = base.APPLY_QUICK_CAPEX_FIX
WIND_SOLAR_MULT = base.WIND_SOLAR_MULT
BATTERY_MULT = base.BATTERY_MULT
TRANSMISSION_EFFICIENCY = base.TRANSMISSION_EFFICIENCY
BATTERY_DURATION_HOURS = base.BATTERY_DURATION_HOURS
PUMP_HYDRO_DURATION_HOURS = base.PUMP_HYDRO_DURATION_HOURS

OUTPUT_DIR = "ev_charging_showcase_results"

# June 1–7 (includes Sat–Sun); same window as ev_charging_results/june
JUNE_START_HOUR = 3624
WINDOW_LABEL = "June week (Sat–Sun included)"

# scale multiplies synthetic_load_8760.csv (kW) after slicing to the June window.
# Base scale 1.0 = 2.5M EVs (the synthetic load file's reference fleet size).
SCENARIOS = [
    {
        "name": "baseline_no_ev",
        "label": "Baseline (no EV)",
        "year": 2020,
        "fleet_millions": 0.0,
        "ev_scale": 0.0,
        "description": "Baseline without EV charging; battery/solar/wind CAPEX on.",
    },
    {
        "name": "ev_0p5m_2020",
        "label": "0.5M EV",
        "year": 2020,
        "fleet_millions": 0.5,
        "ev_scale": 0.2,
        "description": "Baseline + 0.5M EV (load ×0.2 of 2.5M base), 2020.",
    },
    {
        "name": "ev_2m_2025",
        "label": "2M EV",
        "year": 2025,
        "fleet_millions": 2.0,
        "ev_scale": 0.8,
        "description": "Baseline + 2M EV (load ×0.8 of 2.5M base), 2025.",
    },
    {
        "name": "ev_15m_2035",
        "label": "15M EV",
        "year": 2035,
        "fleet_millions": 15.0,
        "ev_scale": 6.0,
        "description": "Baseline + 15M EV (load ×6 of 2.5M base), 2035.",
    },
    {
        "name": "ev_25m_2045",
        "label": "25M EV",
        "year": 2045,
        "fleet_millions": 25.0,
        "ev_scale": 10.0,
        "description": "Baseline + 25M EV (load ×10 of 2.5M base), 2045.",
    },
]
