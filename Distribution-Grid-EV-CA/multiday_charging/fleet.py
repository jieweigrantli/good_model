"""
fleet.py -- build a synthetic EV fleet with per-vehicle attributes.

Each vehicle gets:
- ``battery_kwh``        : pack size drawn from the configured battery mix
- ``usable_kwh``         : battery_kwh * (1 - reserve_soc)
- ``eff_mi_per_kwh``     : driving efficiency (constant by default)
- ``home_access``        : has a home charger (bool)
- ``work_access``        : can charge at work (bool)
- ``interval_pref_days`` : preferred charging interval (1..7) from the
                           charge-frequency probability matrix
- ``mean_daily_vmt``     : personal typical daily mileage
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import MultiDayConfig


def build_fleet(cfg: MultiDayConfig, rng: np.random.Generator) -> pd.DataFrame:
    n = cfg.n_vehicles

    # battery capacity
    bmix = cfg.normalised_battery_mix()
    caps = np.array(list(bmix.keys()), dtype=float)
    cap_p = np.array(list(bmix.values()), dtype=float)
    battery = rng.choice(caps, size=n, p=cap_p)

    # preferred charging interval (days)
    iprobs = cfg.normalised_interval_probs()
    intervals = np.array(list(iprobs.keys()), dtype=int)
    interval_p = np.array(list(iprobs.values()), dtype=float)
    interval_pref = rng.choice(intervals, size=n, p=interval_p)

    # access flags
    home_access = rng.random(n) < cfg.home_access_share
    work_access = rng.random(n) < cfg.work_access_share

    # personal typical daily VMT (lognormal around mean_daily_vmt)
    cv = cfg.vmt_between_vehicle_cv
    sigma = np.sqrt(np.log(1.0 + cv * cv))
    mu = np.log(cfg.mean_daily_vmt) - 0.5 * sigma * sigma
    mean_daily_vmt = rng.lognormal(mean=mu, sigma=sigma, size=n)

    fleet = pd.DataFrame(
        {
            "vehicle_id": np.arange(n, dtype=int),
            "battery_kwh": battery,
            "usable_kwh": battery * (1.0 - cfg.reserve_soc),
            "eff_mi_per_kwh": np.full(n, cfg.efficiency_mi_per_kwh, dtype=float),
            "home_access": home_access,
            "work_access": work_access,
            "interval_pref_days": interval_pref,
            "mean_daily_vmt": mean_daily_vmt,
        }
    )
    return fleet


def fleet_summary(fleet: pd.DataFrame) -> pd.DataFrame:
    """Compact summary table for sanity-checking the configured mix."""
    rows = []
    rows.append(("n_vehicles", len(fleet)))
    rows.append(("mean_battery_kwh", round(float(fleet["battery_kwh"].mean()), 2)))
    rows.append(("home_access_share", round(float(fleet["home_access"].mean()), 3)))
    rows.append(("work_access_share", round(float(fleet["work_access"].mean()), 3)))
    rows.append(
        ("mean_interval_pref_days", round(float(fleet["interval_pref_days"].mean()), 3))
    )
    rows.append(("mean_daily_vmt", round(float(fleet["mean_daily_vmt"].mean()), 2)))
    return pd.DataFrame(rows, columns=["metric", "value"])
