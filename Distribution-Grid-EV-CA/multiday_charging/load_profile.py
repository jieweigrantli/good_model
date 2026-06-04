"""
load_profile.py -- expand sampled sessions into hourly load and aggregate.

A session drawing ``power`` kW that starts at ``start_hour`` and ends at
``end_hour`` (possibly wrapping past midnight) contributes ``power`` to every
hour it is active, following the same hour-occupancy convention as the parent
pipeline's ``06_01`` (overnight sessions wrap into the early hours of the same
day index).

Key outputs:
- ``daily_hourly_matrix``      : [n_days, 24] total fleet kW per day/hour
- ``hourly_load_by_type``      : dict charge_type -> [n_days, 24]
- ``percentile_band``          : per-hour percentile band across the days
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def daily_hourly_matrix(
    sessions: pd.DataFrame, n_days: int, power_col: str = "power"
) -> np.ndarray:
    """Return [n_days, 24] matrix of total kW by (day, hour)."""
    load = np.zeros((n_days, 24), dtype=float)
    if sessions.empty:
        return load

    days = sessions["day"].to_numpy()
    start = sessions["start_hour"].to_numpy()
    end = sessions["end_hour"].to_numpy()
    power = sessions[power_col].to_numpy(dtype=float)

    same_day_base = start <= end
    for h in range(24):
        same_day = same_day_base & (h >= start) & (h <= end)
        overnight = (~same_day_base) & ((h >= start) | (h <= end))
        active = same_day | overnight
        if active.any():
            np.add.at(load, (days[active], np.full(active.sum(), h)), power[active])
    return load


def hourly_load_by_type(
    sessions: pd.DataFrame, n_days: int, power_col: str = "power"
) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for ct, grp in sessions.groupby("charge_type"):
        out[str(ct)] = daily_hourly_matrix(grp, n_days, power_col)
    return out


def percentile_band(
    load: np.ndarray, lo: float = 5.0, hi: float = 95.0
) -> pd.DataFrame:
    """Per-hour percentile band across days."""
    return pd.DataFrame(
        {
            "hour": np.arange(24),
            "p_lo": np.percentile(load, lo, axis=0),
            "median": np.percentile(load, 50, axis=0),
            "mean": load.mean(axis=0),
            "p_hi": np.percentile(load, hi, axis=0),
        }
    )


def grouped_daily_hourly(
    sessions: pd.DataFrame,
    group_col: str,
    n_days: int,
    power_col: str = "power",
) -> dict[object, np.ndarray]:
    """Return {group_value: [n_days, 24]} for block/TAZ-level curves.

    Requires ``sessions`` to carry ``group_col`` (e.g. a block or TAZ id);
    not used by the default fleet-level workflow.
    """
    out: dict[object, np.ndarray] = {}
    for gv, grp in sessions.groupby(group_col):
        out[gv] = daily_hourly_matrix(grp, n_days, power_col)
    return out
