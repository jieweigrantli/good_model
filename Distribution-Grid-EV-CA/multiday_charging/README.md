# Multi-day, SOC-aware EV charging sampler

A self-contained subproject that rebuilds the charging-session sampling so that
vehicles **do not necessarily charge every day**. Charging is driven by a
combination of a per-vehicle **charging-frequency preference** (charge every
*k* days) and **battery capacity** (a larger pack can skip more days before it
must recharge).

It requires the empirical session pool at
``data/charging data/charging_session_all_clean.pkl`` (build via
``build_multiday_inputs.py``). SDPTM trip calibration is optional.

## What it models

| # | Component | Module |
|---|-----------|--------|
| 1 | Fleet with different battery capacities + charging-frequency preference | `fleet.py` |
| 2 | Short-distance daily trip profile with destination labels (H/W/P) | `trips.py` |
| 3 | Empirical session pool (home, work, public L2, public DC) | `session_pool.py` |
| 4 | Charging-location energy shares (Home/Work/Public) | `choice.py` + `config.py` |
| 5 | Charging-frequency probability matrix (every 1..7 days) | `config.py` -> `fleet.py` |
| 6 | SOC-aware multi-day sampling logic | `sampling.py` |
| - | Hourly load aggregation + percentiles | `load_profile.py` |
| - | Driver notebook (single config cell, runs all, plots) | `run_multiday_charging.ipynb` |

## Core logic (`sampling.py`)

For each vehicle, energy demand accumulates as it drives. A charge is triggered
when **either**:

- the preferred interval has elapsed (`days_since >= interval_pref_days`), **or**
- the battery would be depleted (`cumulative_energy >= usable_kwh`), where
  `usable_kwh = battery_kwh * (1 - reserve_soc)`.

On a charging day a feasible location (home/work/public) is chosen to match the
configured **fleet recharge energy shares** (default: greedy daily kWh budgeting).
The recharge energy (depletion since last charge, capped at the usable pack) is
binned, and an empirical session of the matching `(charge_type, sub-type, bin)`
is drawn to supply `start_hour`, `end_hour`, and `power`.

## Run it

Open `run_multiday_charging.ipynb`, edit the single **Configuration** cell, then
*Run All*. It simulates 365 days and writes to `multiday_charging/outputs/`:

- `daily_load_365_lines.png` — all 365 daily load curves + mean
- `hourly_load_percentile_band.png` — 5–95th percentile band by hour
- `mean_load_by_type.png` — mean daily load split by Home/Work/Public
- `daily_hourly_load_kW.npy`, `hourly_percentile_band.csv`

## Key settings (in `config.py` / the notebook config cell)

- `battery_mix` — capacity distribution `{kWh: share}`
- `reserve_soc` — minimum state of charge before a forced charge
- `charge_interval_probs` — frequency matrix `{days: prob}`
- `location_weights` — fleet recharge **energy** fractions (kWh): home / work / public (default 0.68 / 0.04 / 0.28, matching Reference RAW_SHARES)
- `location_selection` — `"energy_budget"` (default) allocates each day's kWh greedily toward `location_weights`; `"session_prob"` uses legacy per-session lottery weights
- `public_level_weights` — absolute fleet energy fractions for public DC and L2 (default 0.20 / 0.08)
- `home_access_share`, `work_access_share`, `work_trip_prob`, `public_trip_prob` — location feasibility
- `mean_daily_vmt`, `vmt_*_cv`, `no_travel_prob` — daily-driving model
- `efficiency_mi_per_kwh` — 3.0 reproduces the parent pipeline's `dist/3` rule

## Using real inputs

- Real SDPTM trips at `data/mobility_data/CSTDM_processed/EV trips_new_SDPTM_sample42.pkl`
  calibrate per-vehicle VMT and work access (toggle `CALIBRATE_FROM_REAL_TRIPS`).
- Empirical pool at `data/charging data/charging_session_all_clean.pkl`
  (or `.rds` via `pyreadr`) is required.

## Block / TAZ-level curves

Attach a `zone` column to `sessions` (e.g. via the block→TAZ crosswalk), then:

```python
import load_profile as L
by_zone = L.grouped_daily_hourly(sessions, "zone", cfg.n_days)  # {zone: [n_days, 24]}
```

## Dependencies

`numpy`, `pandas`, `matplotlib` (all already used by the parent project).
The notebook also uses the parent `common.py` for `.pkl`/`.rds`-style loading.
