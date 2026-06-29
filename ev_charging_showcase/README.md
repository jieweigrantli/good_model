# EV Charging Interactive Showcase

Multi-scenario June-week runs for comparing grid impacts at different EV fleet sizes.
Reuses the [`ev_charging_project`](../ev_charging_project/) solver, post-processing, and graphics pipeline.

## Scenarios

All scenarios use the **same June week** (`start_hour=3624`, 168 hours, includes Sat–Sun).

The base `synthetic_load_8760.csv` represents a **2.5M-vehicle** fleet, so each
scenario's load scale is `fleet_millions / 2.5`.

| Scenario | Year | Fleet | Load scale |
|----------|------|-------|------------|
| `baseline_no_ev` | 2020 | 0 | ×0 |
| `ev_0p5m_2020` | 2020 | 0.5M | ×0.2 |
| `ev_2m_2025` | 2025 | 2M | ×0.8 |
| `ev_15m_2035` | 2035 | 15M | ×6 |
| `ev_25m_2045` | 2045 | 25M | ×10 |

Run accounting (**5 total solves**):

1. **One baseline solve** (no EV, battery/solar/wind CAPEX on). This *is* the
   `baseline_no_ev` scenario — it is never re-solved per scenario.
2. **Four EV solves** (`0.5M`, `2M`, `15M`, `25M`), each on the baseline grid.

EV scenarios **do not add any CAPEX on top of the baseline**: the baseline's
solar/wind/battery buildout is baked in as fixed capacity and all optional
assets are frozen (`capex_capacity=0`, `extensible=False`). Extra EV load is
served by the existing baseline grid (or shortfall), never by new capacity.

Each scenario is post-processed into the same plots/CSVs as `ev_charging_project`.

## Run

From repository root (requires Gurobi + `Examples/WEC_modified.json` + `synthetic_load_8760.csv`):

```bash
python ev_charging_showcase/run_showcase.py
```

Options:

```bash
python ev_charging_showcase/run_showcase.py --scenario ev_2m_2025
python ev_charging_showcase/run_showcase.py --postprocess-only
python ev_charging_showcase/run_showcase.py --skip-aggregate
python ev_charging_showcase/run_showcase.py --force-baseline   # re-solve baseline
```

The baseline is solved once and reused; pass `--force-baseline` to re-solve it.

## Outputs

```
ev_charging_showcase_results/
├── showcase_manifest.json          # top-level index for interactive apps
├── _shared_baseline/               # one baseline solve reused by all scenarios
├── aggregate/
│   ├── data/summary_metrics.csv
│   └── plots/                      # cross-scenario comparison figures
└── <scenario>/
    ├── scenario_meta.json          # objectives, paths, fleet metadata
    ├── iteration_summary.txt
    ├── baseline/
    │   ├── baseline_solution_*.json
    │   └── baseline_objective_*.txt
    ├── ev/
    │   ├── ev_solution_*.json
    │   └── ev_objective_*.txt
    ├── data/                       # generation, emissions, CAPEX CSVs
    └── plots/                      # same PNG set as ev_charging_results/june
```

## JSON files for interactive use

- **`showcase_manifest.json`** — lists all scenarios, objectives, and artifact paths
- **`scenario_meta.json`** — per-scenario metadata + paths to solution JSON and plots
- **`baseline_solution_*.json` / `ev_solution_*.json`** — full solved network state (same format as `ev_charging_project`)

Load a solution in Python:

```python
from ev_charging_project.utils import load_solution_json
g, meta = load_solution_json("ev_charging_showcase_results/ev_2m_2025/ev/ev_solution_....json")
```
