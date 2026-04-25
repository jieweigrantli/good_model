"""
04_02_split_charging_events_TAZ_to_block_yearly_new.py — Python port of
``04_02_split_charging events_TAZ to block_yearly new.R``.

Splits TAZ-level charging events into block-level events according to each
block's population/jobs share within its TAZ, handling integer rounding with
a seeded random residual assignment.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common import read_rds_like, save_rds_like

EVENT_SD = "data/mobility data/CSTDM processed/charge_event_new_SDPTM_sample42_draw12.pkl"
EVENT_LD = "data/mobility data/CSTDM processed/charge_event_new_LDPTM_sample42.pkl"
EVENT_ETM = "data/mobility data/CSTDM processed/charge_event_new_ETM_sample42.pkl"
SHARE_CSV = "data/mapping/census block to TAZ 2010/share_block vs TAZ.csv"

OUT_DIR = "data/mobility data/CSTDM processed"
YEARS = range(2022, 2046)


def _normalise(event: pd.DataFrame, mapping: dict, extras: dict | None = None) -> pd.DataFrame:
    out = event.rename(columns=mapping)
    cols = ["year", "TAZ", "dist", "type", "time"]
    if extras:
        for k, v in extras.items():
            out[k] = v
        cols += list(extras.keys())
    else:
        out["trip"] = mapping.pop("trip_default", None)
        cols += ["trip"]
    return out[cols]


def _distribute_int(
    count_tbl: pd.DataFrame,
    share_col: str,
    seed: int,
) -> pd.DataFrame:
    """Distribute an integer event count across blocks proportional to share.
    Residuals assigned to a random permutation of blocks with share > 0.
    """
    rng = np.random.default_rng(seed)
    out = count_tbl.copy()
    out["int"] = np.floor(out["n_event_TAZ"] * out[share_col]).astype(int)
    out["res"] = out.groupby(["TAZ", "year"])["int"].transform(
        lambda s: s.name[0] if False else (s.iloc[0] if False else None)
    )  # placeholder
    # n_event_TAZ is constant within (TAZ, year); residual = n_event_TAZ - sum(int)
    agg = out.groupby(["TAZ", "year"])[["n_event_TAZ", "int"]].agg({"n_event_TAZ": "first", "int": "sum"})
    agg["res"] = agg["n_event_TAZ"] - agg["int"]
    out = out.drop(columns=["res"]).merge(
        agg["res"].rename("res").reset_index(), on=["TAZ", "year"]
    )
    out["n_event_block"] = out["int"]

    # Random order among blocks with positive share, within each (TAZ, year)
    out["ID_TAZ_y_nonz"] = 0
    nz_mask = out[share_col] > 0

    def _assign_order(idx: pd.Index) -> np.ndarray:
        ord_ids = rng.permutation(len(idx)) + 1
        return ord_ids

    out_nz = out[nz_mask]
    orders = np.empty(len(out_nz), dtype=int)
    cursor = 0
    for _, g in out_nz.groupby(["TAZ", "year"], sort=False):
        ord_ids = _assign_order(g.index)
        orders[cursor : cursor + len(g)] = ord_ids
        cursor += len(g)
    out.loc[nz_mask, "ID_TAZ_y_nonz"] = orders

    bump = (out["ID_TAZ_y_nonz"] > 0) & (out["ID_TAZ_y_nonz"] <= out["res"])
    out.loc[bump, "n_event_block"] = out.loc[bump, "n_event_block"] + 1
    return out


def _split_events_to_blocks(
    events: pd.DataFrame,
    nblock: pd.DataFrame,
    group_cols: list[str],
    seed: int,
) -> pd.DataFrame:
    """Randomly order events within each (TAZ, year) and assign cumulative
    position to a block based on the number of events that block owns.
    """
    rng = np.random.default_rng(seed)
    events = events.copy()
    events["order"] = 0

    for _, g in events.groupby(["TAZ", "year"], sort=False):
        events.loc[g.index, "order"] = rng.permutation(len(g)) + 1

    nblock = nblock.sort_values(["TAZ", "year", "ID_TAZ_y_nonz"]).copy()
    nblock["start"] = nblock.groupby(["TAZ", "year"])["n_event_block"].shift(fill_value=0).groupby(
        nblock.groupby(["TAZ", "year"]).ngroup()
    ).cumsum()
    nblock["end"] = nblock["start"] + nblock["n_event_block"]

    # For each event find the block whose (start, end] bracket covers its order
    split_parts = []
    for (taz, yr), g_ev in events.groupby(["TAZ", "year"], sort=False):
        g_bl = nblock[(nblock["TAZ"] == taz) & (nblock["year"] == yr)]
        if g_bl.empty:
            continue
        # For each event: find the block where start < order <= end
        orders = g_ev["order"].to_numpy()
        starts = g_bl["start"].to_numpy()
        ends = g_bl["end"].to_numpy()
        bl_idx = np.searchsorted(ends, orders, side="left")  # smallest end >= order
        valid = bl_idx < len(g_bl)
        if not valid.any():
            continue
        g_ev_valid = g_ev.iloc[valid].copy()
        geoid = g_bl["GEOID10"].to_numpy()[bl_idx[valid]]
        g_ev_valid["GEOID10"] = geoid
        split_parts.append(g_ev_valid)

    if not split_parts:
        return pd.DataFrame()
    out = pd.concat(split_parts, ignore_index=True)
    return out


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    event_sd = read_rds_like(EVENT_SD).rename(
        columns={"chargeDist": "dist", "ChargeType": "type", "J": "TAZ", "Time": "time"}
    )
    event_sd = event_sd[["year", "TAZ", "dist", "type", "time"]].copy()
    event_sd["trip"] = "sd"

    event_ld = read_rds_like(EVENT_LD).rename(
        columns={"TAZway": "TAZ", "chargeDist": "dist", "ChargeEventType": "type", "Time": "time"}
    )
    event_ld["trip"] = np.where(event_ld["other"] == "corridor", "ld_corridor", "ld_destination")
    event_ld = event_ld[["year", "TAZ", "dist", "type", "time", "trip"]]

    event_etm = read_rds_like(EVENT_ETM).rename(
        columns={"TAZway": "TAZ", "ChargeType": "type", "chargeDist": "dist", "Time": "time"}
    )
    event_etm = event_etm[["year", "TAZ", "dist", "type", "time"]].copy()
    event_etm["trip"] = "etm"

    event = pd.concat([event_sd, event_ld, event_etm], ignore_index=True).sort_values(
        ["year", "type", "TAZ"]
    )

    event_home = event[event["type"] == "H"].copy()
    event_wp = event[event["type"].isin(["W", "P"])].copy()

    n_event_home = event_home.groupby(["TAZ", "year"], as_index=False).size().rename(
        columns={"size": "n_event"}
    )
    n_event_wp = event_wp.groupby(["TAZ", "year"], as_index=False).size().rename(
        columns={"size": "n_event"}
    )

    share_block = pd.read_csv(SHARE_CSV).rename(columns={"TAZ_Zone": "TAZ"})
    share_years = pd.concat(
        [share_block.assign(year=y) for y in YEARS], ignore_index=True
    )

    n_home_block = pd.merge(share_years, n_event_home, on=["TAZ", "year"]).rename(
        columns={"n_event": "n_event_TAZ"}
    )
    n_home_block = n_home_block[n_home_block["population_TAZ"] != 0]

    n_wp_block = pd.merge(share_years, n_event_wp, on=["TAZ", "year"]).rename(
        columns={"n_event": "n_event_TAZ"}
    )
    n_wp_block = n_wp_block[n_wp_block["jobs_TAZ"] != 0]

    n_home_block = n_home_block[
        ["year", "TAZ", "GEOID10", "population_share", "n_event_TAZ"]
    ]
    n_wp_block = n_wp_block[["year", "TAZ", "GEOID10", "jobs_share", "n_event_TAZ"]]

    n_home_block = _distribute_int(n_home_block, "population_share", seed=66)
    n_wp_block = _distribute_int(n_wp_block, "jobs_share", seed=66)

    event_home_split = _split_events_to_blocks(event_home, n_home_block, ["TAZ", "year"], seed=99)
    event_wp_split = _split_events_to_blocks(event_wp, n_wp_block, ["TAZ", "year"], seed=99)

    save_rds_like(
        event_home_split,
        os.path.join(OUT_DIR, "charge_event_new_block_home_sample42_draw12_split6699.pkl"),
    )
    save_rds_like(
        event_wp_split,
        os.path.join(OUT_DIR, "charge_event_new_block_workpublic_sample42_draw12_split6699.pkl"),
    )


if __name__ == "__main__":
    main()
