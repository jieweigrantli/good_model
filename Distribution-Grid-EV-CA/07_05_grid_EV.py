"""
07_05_grid_EV.py — Python port of ``07_05_grid_EV.R``.

Combines utility baseline load & capacity (PGE, SCE, SDGE) with the cumulative
EV charging load per feeder to produce an overload table.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common import read_rds_like, save_rds_like

LOAD_PGE = "data/grid data/clean/clean load PGE_full hour.csv"
LOAD_SCE = "data/grid data/clean/clean load SCE_fixed.csv"
LOAD_SDGE = "data/grid data/clean/clean load SDGE_2022.csv"

CAP_PGE = "data/grid data/clean/clean ICA PGE_all.csv"
CAP_SCE = "data/grid data/clean/clean GNA SCE.csv"
CAP_SDGE = "data/grid data/clean/clean ICA SDGE_all.csv"

EV_PKL = (
    "data/result/charging profile by feeder/"
    "profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.pkl"
)
OUT_PKL = (
    "data/result/overload/overload_sample42_draw12_split6699_sample36.pkl"
)


def _prep_pge() -> pd.DataFrame:
    load = pd.read_csv(LOAD_PGE)[["FeederID", "load_high", "month", "hour"]]
    cap = pd.read_csv(CAP_PGE).rename(
        columns={"NetworkId": "FeederID", "Month": "month", "Hour": "hour", "capacity_kW": "headroom"}
    )
    pge = pd.merge(load, cap, on=["FeederID", "month", "hour"]).rename(columns={"load_high": "baseload"})
    pge["capacity"] = pge["headroom"] + pge["baseload"]
    pge["FeederID"] = pge["FeederID"].astype(str)
    pge.loc[pge["FeederID"].str.len() == 8, "FeederID"] = "0" + pge.loc[pge["FeederID"].str.len() == 8, "FeederID"]
    pge["utility"] = "PGE"
    return pge


def _prep_sce() -> pd.DataFrame:
    load = pd.read_csv(LOAD_SCE)
    cap = pd.read_csv(CAP_SCE)[["FeederID", "capacity_kW"]]
    sce = pd.merge(load, cap, on="FeederID").rename(
        columns={"load_high": "baseload", "capacity_kW": "capacity"}
    )
    sce["headroom"] = sce["capacity"] - sce["baseload"]
    sce["utility"] = "SCE"
    return sce


def _prep_sdge() -> pd.DataFrame:
    load = pd.read_csv(LOAD_SDGE)
    cap = pd.read_csv(CAP_SDGE)[["FeederID", "month", "hour", "capacity"]]
    cap["FeederID"] = cap["FeederID"].astype(str)
    sdge = pd.merge(load, cap, on=["FeederID", "month", "hour"]).rename(
        columns={"capacity": "headroom", "load": "baseload"}
    )
    sdge["capacity"] = sdge["headroom"] + sdge["baseload"]
    sdge["utility"] = "SDGE"
    return sdge


def main() -> None:
    os.makedirs(os.path.dirname(OUT_PKL), exist_ok=True)
    ev = read_rds_like(EV_PKL)

    pge = _prep_pge()
    sce = _prep_sce()
    sdge = _prep_sdge()
    grid = pd.concat([pge, sce, sdge], ignore_index=True)

    ev_sum = (
        ev.groupby(["FeederID", "year", "hour"], as_index=False)["load"]
        .sum()
        .rename(columns={"load": "EVload"})
    )
    # replicate per month 1..12
    ev_sum = ev_sum.loc[ev_sum.index.repeat(12)].reset_index(drop=True)
    n_rows = len(ev_sum) // 12
    ev_sum["month"] = np.tile(np.arange(1, 13), n_rows)

    ev_grid = pd.merge(ev_sum, grid, on=["FeederID", "month", "hour"])
    ev_grid["overload"] = ev_grid["EVload"] - ev_grid["headroom"]
    save_rds_like(ev_grid, OUT_PKL)

    print("valid feeders:", ev_grid["FeederID"].nunique())


if __name__ == "__main__":
    main()
