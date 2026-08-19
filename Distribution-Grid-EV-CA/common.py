"""
common.py — Shared helpers used across the Distribution-Grid-EV-CA Python ports.

Notes on the R→Python translation:
- R data.table → pandas.DataFrame
- fread / fwrite → pd.read_csv / df.to_csv
- readRDS / saveRDS → pd.read_pickle / df.to_pickle (extension .pkl)
  (if you need to read native .rds files produced by R, install ``pyreadr``
   and replace _read_rds with pyreadr.read_r(...).)
- library(sf) → geopandas; st_read → gpd.read_file; st_transform → gdf.to_crs
- ggplot2 → matplotlib (and optionally seaborn)
- setnames(df, "a", "b") → df.rename(columns={"a": "b"}, inplace=True)
- dcast(df, A ~ B, value.var="v") → df.pivot_table(index=A, columns=B, values="v")
- melt (data.table) → pd.melt
"""

from __future__ import annotations

import glob
import os
import pickle
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project roots / ASTR–GRIP paths
# ---------------------------------------------------------------------------

# Directory containing this module (Distribution-Grid-EV-CA/)
PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR / "data"
REPO_ROOT = PKG_DIR.parent

# Nested unzip layout from PG&E GRIP Shape download
GRIP_SHP_DIR = DATA_DIR / "GRIP_SHP" / "GRIP_SHP" / "GRIP_SHP" / "GRIP_SHP"

GRIP_ED_SUBSTATIONS = GRIP_SHP_DIR / "EDSubstations.shp"
GRIP_TRANSMISSION_LINES = GRIP_SHP_DIR / "TransmissionLines.shp"
GRIP_FEEDER_DETAIL = GRIP_SHP_DIR / "FeederDetail.shp"
GRIP_SUBSTATION_LOAD_PROFILE = GRIP_SHP_DIR / "SubstationLoadProfile.shp"
GRIP_FEEDER_LOAD_PROFILE = GRIP_SHP_DIR / "FeederLoadProfile.shp"

TAZ_CENTROID_GPKG = DATA_DIR / "shps" / "TAZ_centroid_sf.gpkg"
MULTIDAY_OUTPUT_DIR = PKG_DIR / "multiday_charging" / "outputs"
TAZ_TOTAL_DEMAND_CSV = MULTIDAY_OUTPUT_DIR / "taz_total_demand_kwh.csv"
FLEET_DAILY_HOURLY_NPY = MULTIDAY_OUTPUT_DIR / "daily_hourly_load_kW.npy"

MAPPING_DIR = DATA_DIR / "mapping"
MESO_DIR = DATA_DIR / "meso"
FIGURES_ASTR_DIR = PKG_DIR / "figures" / "ASTR_diagnostics"
ASTR_RESULTS_DIR = PKG_DIR / "astr_meso_results"

# HIFLD Electric Substations cache (downloaded by 08_01 if missing)
HIFLD_SUBSTATIONS_GPKG = DATA_DIR / "hifld" / "electric_substations_ca.gpkg"


def is_meso_delivery_node(node_id: str) -> bool:
    """True for nested CA delivery nodes (SUB_* substations or aggregated MESO_*)."""
    s = str(node_id)
    return s.startswith("SUB_") or s.startswith("MESO_")


CALIFORNIA_REGIONS = [
    "WEC_BANC",
    "WEC_CALN",
    "WEC_LADW",
    "WEC_SDGE",
    "WECC_IID",
    "WECC_SCE",
]

# Seasonal week windows (hour-of-year start), matching ev_charging_project/config.py
SEASONAL_WEEKS = [
    {"name": "march", "start_hour": 1416, "month": "March"},
    {"name": "june", "start_hour": 3624, "month": "June"},
    {"name": "september", "start_hour": 5832, "month": "September"},
    {"name": "december", "start_hour": 8016, "month": "December"},
]
NUM_HOURS_WEEK = 7 * 24


def grip_layer(name: str) -> Path:
    """Return path to a GRIP shapefile layer by basename (with or without .shp)."""
    if not name.lower().endswith(".shp"):
        name = f"{name}.shp"
    return GRIP_SHP_DIR / name


def ensure_dir(path: Path | str) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------

def fread_all_in_dir(directory: str, pattern: str = "*.csv", **read_csv_kwargs) -> pd.DataFrame:
    """Read every matching file in a directory and rbind them (like R's
    ``rbindlist(lapply(list.files(), fread))``).
    """
    files = sorted(glob.glob(os.path.join(directory, pattern)))
    if not files:
        return pd.DataFrame()
    parts = [pd.read_csv(f, **read_csv_kwargs) for f in files]
    return pd.concat(parts, ignore_index=True)


def read_rds_like(path: str) -> pd.DataFrame:
    """Read a pickle file that mirrors R's ``readRDS``.

    In the Python port, we save/read intermediate tables as ``.pkl``.
    If ``path`` ends in .rds, this function will look for the matching .pkl
    next to it (same basename).
    """
    if path.endswith(".rds"):
        path = path[:-4] + ".pkl"
    if path.endswith(".pkl") or path.endswith(".pickle"):
        with open(path, "rb") as fh:
            obj = pickle.load(fh)
        return obj
    return pd.read_pickle(path)


def save_rds_like(df: pd.DataFrame, path: str) -> str:
    """Save a DataFrame mirroring R's ``saveRDS`` but as pickle.

    Returns the actual path written (always .pkl).
    """
    if path.endswith(".rds"):
        path = path[:-4] + ".pkl"
    elif not (path.endswith(".pkl") or path.endswith(".pickle")):
        path = path + ".pkl"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_pickle(path)
    return path


# ---------------------------------------------------------------------------
# data.table-ish helpers
# ---------------------------------------------------------------------------

def unique_rows(df: pd.DataFrame, subset: Iterable[str] | None = None) -> pd.DataFrame:
    """R's ``unique(dt)``."""
    return df.drop_duplicates(subset=list(subset) if subset else None).reset_index(drop=True)


def group_sample(
    df: pd.DataFrame,
    by: list[str],
    n_col: str,
    id_col: str,
    seed: int | None = None,
) -> pd.DataFrame:
    """Within each group ``by``, sample ``min(n_col)`` distinct values of ``id_col``.
    Returns one row per sampled id with the grouping columns plus the id."""
    rng = np.random.default_rng(seed)
    out_frames: list[pd.DataFrame] = []
    for keys, g in df.groupby(by, sort=False):
        if g.empty:
            continue
        n_take = int(g[n_col].min())
        n_take = max(0, min(n_take, len(g)))
        if n_take == 0:
            continue
        idx = rng.choice(len(g), size=n_take, replace=False)
        picked = g.iloc[idx][[id_col]].copy()
        if not isinstance(keys, tuple):
            keys = (keys,)
        for k, v in zip(by, keys):
            picked[k] = v
        out_frames.append(picked)
    return (
        pd.concat(out_frames, ignore_index=True)
        if out_frames
        else pd.DataFrame(columns=list(by) + [id_col])
    )


def weighted_sample(values, weights, n, seed=None, replace=True):
    """R's ``sample(values, n, prob=weights, replace=TRUE)``."""
    rng = np.random.default_rng(seed)
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()
    return rng.choice(np.asarray(values), size=n, replace=replace, p=w)
