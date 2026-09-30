"""
05_02_sample_charging_events_distance_bins_IOU_blocks.py — Python port of
``05_02_sample_charging events_distance bins_IOU blocks.R``.

Samples empirical charging sessions (from ``charging_session_all_clean``) that
match each block-level event's distance bin / housing / level.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common import read_rds_like, save_rds_like

EVENT_HOME = (
    "data/mobility_data/CSTDM_processed/charge_event_new_block_home_sample42_draw12_split6699.pkl"
)
EVENT_WP = (
    "data/mobility_data/CSTDM_processed/charge_event_new_block_workpublic_sample42_draw12_split6699.pkl"
)
BLOCK_TO_FEEDER = "data/mapping/block to feeder 2010/shape mapped/block_2010_to_feeder_map_line_combo.csv"
IOU_BLOCKS = "data/mapping/block to feeder 2010/shape block IOUs++/shape block IOUs++.csv"
EVENT_POOL = "data/charging data/charging_session_all_clean.pkl"

OUT_PATH = (
    "data/result/charging session by feeder/"
    "session_feeder_new_bins_IOU_sample42_draw12_split6699_sample36.pkl"
)

DEMAND_BIN_EDGES = np.array([0, 5, 10, 15, 20, 30, 50, 80, np.inf])


def assign_bins(vec: pd.Series) -> pd.Series:
    """Returns bin IDs 1..8 following the R script's cutpoints."""
    return pd.cut(
        vec,
        bins=DEMAND_BIN_EDGES,
        labels=[1, 2, 3, 4, 5, 6, 7, 8],
        right=True,
        include_lowest=True,
    ).astype("Int64")


def main() -> None:
    home = read_rds_like(EVENT_HOME)
    wp = read_rds_like(EVENT_WP)
    block_to_feeder = pd.read_csv(BLOCK_TO_FEEDER)
    event_pool = read_rds_like(EVENT_POOL)
    iou_blocks = pd.read_csv(IOU_BLOCKS)

    event_block = pd.concat([home, wp], ignore_index=True)
    event_block = event_block[event_block["GEOID10"].isin(iou_blocks["GEOID10"])]
    event_block = pd.merge(event_block, block_to_feeder, on="GEOID10")

    event_block["demand"] = event_block["dist"] / 3.0
    event_block["bin"] = assign_bins(event_block["demand"])
    event_pool["bin"] = assign_bins(event_pool["energy"])

    # --- weighted sampling for public level / housing -----------------------
    level_weight = pd.DataFrame({"level": ["DC", "L2"], "ncharger": [8919, 29229]})
    rng = np.random.default_rng(36)

    event_block["level"] = "none"
    public_mask = event_block["type"] == "P"
    p_level = level_weight["ncharger"].to_numpy(dtype=float) / level_weight["ncharger"].sum()
    event_block.loc[public_mask, "level"] = rng.choice(
        level_weight["level"].to_numpy(),
        size=int(public_mask.sum()),
        replace=True,
        p=p_level,
    )
    event_block.loc[event_block["trip"] == "ld_corridor", "level"] = "DC"

    housing_weight = pd.DataFrame({"house": ["single family", "multi family"], "unit": [56.4, 38.9]})
    rng2 = np.random.default_rng(36)
    event_block["house"] = "none"
    home_mask = event_block["type"] == "H"
    p_h = housing_weight["unit"].to_numpy(dtype=float) / housing_weight["unit"].sum()
    event_block.loc[home_mask, "house"] = rng2.choice(
        housing_weight["house"].to_numpy(),
        size=int(home_mask.sum()),
        replace=True,
        p=p_h,
    )

    # --- sample event IDs from the empirical pool ----------------------------
    event_pool = event_pool.reset_index(drop=True).copy()
    event_pool["eventID"] = np.arange(1, len(event_pool) + 1)
    event_block["n_event"] = event_block.groupby(
        ["type", "level", "house", "bin"], dropna=False
    )["type"].transform("size")
    event_block["eventID"] = np.nan

    def _sample_ids(pool_mask: pd.Series, block_mask: pd.Series, seed: int = 36) -> None:
        sub_pool = event_pool.loc[pool_mask, "eventID"].to_numpy()
        n = int(block_mask.sum())
        if n == 0 or sub_pool.size == 0:
            return
        rng_local = np.random.default_rng(seed)
        ids = rng_local.choice(sub_pool, size=n, replace=True)
        event_block.loc[block_mask, "eventID"] = ids

    # home
    for h in ("single family", "multi family"):
        for b in range(1, 9):
            pool_mask = (
                (event_pool["charge_type"] == "home")
                & (event_pool["housing"] == h)
                & (event_pool["bin"] == b)
            )
            block_mask = (
                (event_block["type"] == "H") & (event_block["house"] == h) & (event_block["bin"] == b)
            )
            _sample_ids(pool_mask, block_mask)

    # public
    for lvl in ("DC", "L2"):
        for b in range(1, 9):
            pool_mask = (
                (event_pool["charge_type"] == "public")
                & (event_pool["charger_level"] == lvl)
                & (event_pool["bin"] == b)
            )
            block_mask = (
                (event_block["type"] == "P") & (event_block["level"] == lvl) & (event_block["bin"] == b)
            )
            _sample_ids(pool_mask, block_mask)

    # work
    for b in range(1, 9):
        pool_mask = (event_pool["charge_type"] == "work") & (event_pool["bin"] == b)
        block_mask = (event_block["type"] == "W") & (event_block["bin"] == b)
        _sample_ids(pool_mask, block_mask)

    # --- join empirical rows into each sampled event -------------------------
    event_feeder = pd.merge(event_block, event_pool, on="eventID", how="left")
    cols = [
        "FeederID", "year", "start_hour", "end_hour", "charge_type", "charger_level",
        "power", "energy", "housing",
    ]
    event_feeder = event_feeder[cols]
    save_rds_like(event_feeder, OUT_PATH)


if __name__ == "__main__":
    main()
