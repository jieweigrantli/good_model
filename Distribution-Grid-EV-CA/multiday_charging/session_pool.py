"""
session_pool.py -- empirical charging-session pool (home / work / public).

The pool is the menu of *real* sessions that the sampler draws from. Each row
describes one observed charging session:

    charge_type   : home | work | public
    charger_level : L2 | DC            (L2 for home/work; L2 or DC for public)
    housing       : single family | multi family | NA  (home only)
    energy        : kWh delivered
    power         : kW
    start_hour    : 0..23
    end_hour      : 0..23 (may wrap past midnight)
    bin           : 1..8 energy bin (matches the parent pipeline)

Requires ``charging_session_all_clean.pkl`` (or ``.rds``) on disk — see
``MultiDayConfig.session_pool_pkl``. Build it with ``build_multiday_inputs.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import MultiDayConfig
from common import read_rds_like

_REQUIRED_COLUMNS = {"charge_type", "energy", "start_hour"}


def assign_bins(energy: np.ndarray | pd.Series, cfg: MultiDayConfig) -> pd.Series:
    return pd.cut(
        energy,
        bins=list(cfg.bin_edges),
        labels=list(range(1, len(cfg.bin_edges))),
        right=True,
        include_lowest=True,
    ).astype("Int64")


def _duration_to_end_hour(start_hour: np.ndarray, duration_h: np.ndarray) -> np.ndarray:
    end = (start_hour + np.ceil(duration_h)).astype(int) % 24
    return end


def _infer_power(pool: pd.DataFrame) -> pd.Series:
    """Derive kW from energy and clock-hour span when power is missing."""
    start = pool["start_hour"].astype(int).to_numpy()
    end = pool["end_hour"].astype(int).to_numpy()
    span = end - start
    span = np.where(span <= 0, span + 24, span)
    span = np.maximum(span, 1)
    return pd.Series(pool["energy"].to_numpy() / span, index=pool.index)


def build_session_pool(cfg: MultiDayConfig, rng: np.random.Generator) -> pd.DataFrame:
    """Load the cleaned empirical session pool."""
    del rng  # kept for a stable call signature across modules
    path = cfg.session_pool_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Empirical session pool not found: {path}\n"
            "Run build_multiday_inputs.py (or place charging_session_all_clean.pkl)."
        )

    try:
        pool = read_rds_like(str(path))
    except Exception as exc:
        raise RuntimeError(f"Failed to read session pool at {path}") from exc

    if pool is None or not _REQUIRED_COLUMNS.issubset(pool.columns):
        missing = _REQUIRED_COLUMNS - set(pool.columns if pool is not None else [])
        raise ValueError(
            f"Session pool at {path} is missing required columns: {sorted(missing)}"
        )

    pool = pool.copy()
    pool.attrs["source"] = "empirical"
    if "charger_level" not in pool.columns:
        pool["charger_level"] = "L2"
    if "housing" not in pool.columns:
        pool["housing"] = pd.NA
    if "power" not in pool.columns:
        if "end_hour" not in pool.columns:
            raise ValueError(
                "Session pool must include 'power' or both 'end_hour' and 'start_hour'."
            )
        pool["power"] = _infer_power(pool)
    if "end_hour" not in pool.columns:
        dur = np.clip(pool["energy"].to_numpy() / pool["power"].to_numpy(), 0.1, 14.0)
        pool["end_hour"] = _duration_to_end_hour(
            pool["start_hour"].to_numpy().astype(int), dur
        )

    pool["bin"] = assign_bins(pool["energy"], cfg)
    pool = pool.reset_index(drop=True)
    pool["eventID"] = np.arange(1, len(pool) + 1)
    return pool


def pool_coverage(pool: pd.DataFrame) -> pd.DataFrame:
    """Count sessions per (charge_type, charger_level, bin) for sanity checks."""
    return (
        pool.groupby(["charge_type", "charger_level", "bin"], dropna=False)
        .size()
        .rename("n")
        .reset_index()
    )
