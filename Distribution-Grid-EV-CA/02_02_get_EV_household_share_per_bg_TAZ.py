"""
02_02_get_EV_household_share_per_bg_TAZ.py — Python port of
``02_02_get_EV household share_per_bg_TAZ.R``.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

HH_ALL_TRACT = "data/mobility_data/EV_Toolbox/households_all_by_tract.csv"
HH_EV_TRACT = "data/mobility_data/EV_Toolbox/evhouseholds_by_tract_acc2_2011.csv"
POP_BG_TRACT = "data/mapping/census bg to tract 2010/popluation share bg to tract 2010.csv"
MAP_BG_TAZ = "data/mapping/census bg to tract 2010/map bg to TAZ 2010.csv"

OUT_DIR = "data/mobility_data/EV_Toolbox"
YEARS = range(2020, 2045)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    hh_all_tract = pd.read_csv(HH_ALL_TRACT)
    hh_ev_tract = pd.read_csv(HH_EV_TRACT)
    pop_bg_tract = pd.read_csv(POP_BG_TRACT)
    map_bg_taz = pd.read_csv(MAP_BG_TAZ)[["GEOID", "TAZ_Zone"]].rename(
        columns={"GEOID": "GEOIDbg", "TAZ_Zone": "TAZ"}
    )

    share_bg = pop_bg_tract[["GEOIDtract", "GEOIDbg", "share"]].copy()

    # yearly distribution of tracts → bgs
    share_bg_years = pd.concat(
        [share_bg.assign(year=y) for y in YEARS], ignore_index=True
    )

    # EV households tract → bg
    hh_ev_bg = pd.merge(
        share_bg_years,
        hh_ev_tract,
        left_on=["GEOIDtract", "year"],
        right_on=["tract", "year"],
    ).rename(columns={"households": "EVhh_tract"})
    hh_ev_bg["EVhh_bg"] = hh_ev_bg["EVhh_tract"] * hh_ev_bg["share"]
    hh_ev_bg.to_csv(os.path.join(OUT_DIR, "evhh_tract_bg_ACC2.csv"), index=False)

    # All households tract → bg (single year - collapsed)
    hh_all_total = hh_all_tract.groupby("tract", as_index=False)["households"].sum()
    hh_all_bg = (
        pd.merge(share_bg, hh_all_total, left_on="GEOIDtract", right_on="tract")
        [["GEOIDtract", "GEOIDbg", "share", "households"]]
        .rename(columns={"households": "hh_tract"})
    )
    hh_all_bg["hh_bg"] = hh_all_bg["hh_tract"] * hh_all_bg["share"]
    hh_all_bg.to_csv(os.path.join(OUT_DIR, "allhh_tract_bg.csv"), index=False)

    # EV household share per TAZ per year
    hh_bg = pd.merge(
        hh_ev_bg[["year", "GEOIDbg", "EVhh_bg"]],
        hh_all_bg[["GEOIDbg", "hh_bg"]],
        on="GEOIDbg",
    )
    hh_bg_taz = pd.merge(hh_bg, map_bg_taz, on="GEOIDbg")
    hh_taz = (
        hh_bg_taz.groupby(["year", "TAZ"], as_index=False)
        .agg(EVhh_TAZ=("EVhh_bg", "sum"), hh_TAZ=("hh_bg", "sum"))
    )
    hh_taz["EVhh_share"] = np.where(
        hh_taz["hh_TAZ"] == 0, 0.0, hh_taz["EVhh_TAZ"] / hh_taz["hh_TAZ"]
    )
    hh_taz["EVhh_share"] = hh_taz["EVhh_share"].clip(upper=1.0)
    hh_taz.to_csv(os.path.join(OUT_DIR, "evhh_share_TAZ.csv"), index=False)


if __name__ == "__main__":
    main()
