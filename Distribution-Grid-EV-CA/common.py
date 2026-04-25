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
from typing import Iterable

import numpy as np
import pandas as pd


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
