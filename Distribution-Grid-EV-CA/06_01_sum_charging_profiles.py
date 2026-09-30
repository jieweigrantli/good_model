"""
06_01_sum_charging_profiles.py — Python port of ``06_01_sum_charging profiles.R``.

Produces per-feeder hourly load profiles for each year by expanding every
sampled charging session into a 24-hour vector and summing by feeder/type.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common import read_rds_like, save_rds_like

SESSION_FEEDER_PKL = (
    "data/result/charging session by feeder/"
    "session_feeder_new_bins_IOU_sample42_draw12_split6699_sample36.pkl"
)
OUT_CUM_PKL = (
    "data/result/charging profile by feeder/"
    "profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.pkl"
)

YEARS = range(2022, 2046)


def _year_profile(events: pd.DataFrame) -> pd.DataFrame:
    """Expand each event into 24 hourly rows and sum by (FeederID, charge_type, hour)."""
    n = len(events)
    if n == 0:
        return pd.DataFrame(columns=["FeederID", "charge_type", "hour", "load_feeder"])
    ev = events.loc[events.index.repeat(24)].reset_index(drop=True)
    ev["hour"] = np.tile(np.arange(24), n)
    ev["load"] = 0.0
    same_day = (ev["hour"] >= ev["start_hour"]) & (ev["hour"] <= ev["end_hour"])
    overnight = (ev["start_hour"] > ev["end_hour"]) & (
        (ev["hour"] >= ev["start_hour"]) | (ev["hour"] <= ev["end_hour"])
    )
    ev.loc[same_day, "load"] = ev.loc[same_day, "power"]
    ev.loc[overnight, "load"] = ev.loc[overnight, "power"]
    agg = ev.groupby(["FeederID", "charge_type", "hour"], as_index=False)["load"].sum()
    return agg.rename(columns={"load": "load_feeder"})


def main() -> None:
    os.makedirs(os.path.dirname(OUT_CUM_PKL), exist_ok=True)
    event_feeder = read_rds_like(SESSION_FEEDER_PKL)
    all_feeders = event_feeder["FeederID"].unique().tolist()

    # step 1: per-year new profiles (not cumulative)
    byyear_parts: list[pd.DataFrame] = []
    for y in YEARS:
        y_prof = _year_profile(event_feeder[event_feeder["year"] == y])
        y_prof["year"] = y
        byyear_parts.append(y_prof)
    by_year = pd.concat(byyear_parts, ignore_index=True)

    # step 2: cumulative up to year y, expanded to all feeders × {home, work, public} × 24h
    idx = pd.MultiIndex.from_product(
        [all_feeders, ["home", "work", "public"], range(24)],
        names=["FeederID", "charge_type", "hour"],
    )
    template = pd.DataFrame(index=idx).reset_index()

    cum_parts: list[pd.DataFrame] = []
    for y in YEARS:
        prev = by_year[by_year["year"] <= y]
        cum = (
            prev.groupby(["FeederID", "charge_type", "hour"], as_index=False)["load_feeder"]
            .sum()
            .rename(columns={"load_feeder": "load"})
        )
        merged = template.merge(cum, on=["FeederID", "charge_type", "hour"], how="left")
        merged["load"] = merged["load"].fillna(0.0)
        merged["year"] = y
        cum_parts.append(merged)

    cum_profile = pd.concat(cum_parts, ignore_index=True)
    save_rds_like(cum_profile, OUT_CUM_PKL)


if __name__ == "__main__":
    main()
