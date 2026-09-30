"""
02_04_get_EV_trips.py — Python port of ``02_04_get_EV trips.R``.

Filters the raw CSTDM trip tables down to EV-household trips for SDPTM, LDPTM,
LDPTM AccEgr, and ETM, then joins aggregate charging-purpose categories.
Outputs pickle files (``.pkl``) in place of the R ``.rds`` files.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common import fread_all_in_dir, read_rds_like, save_rds_like

SDPTM_DIR = "data/mobility_data/CSTDM/SDPTM"
LDPTM_TRIPS = "data/mobility_data/CSTDM/LDPTM/LDPTM_Trips.csv"
LDPTM_ACCEGR = "data/mobility_data/CSTDM/LDPTM/LDPTM_AccEgr.csv"
ETM_TRIPS = "data/mobility_data/CSTDM/ETM/trips_Ext.csv"
PARSED_LDPTM_DIR = "data/mobility_data/TAZ_distance/parsed_100000_LDPTM"
PARSED_ETM_DIR = "data/mobility_data/TAZ_distance/parsed_100000_ETM"

OUT_DIR = "data/mobility_data/CSTDM_processed"


# charging-purpose aggregation tables (from the R script)
AGG_SD = pd.DataFrame(
    {
        "ChargeType": ["H", "W", "W", "P", "P", "P", "P", "P", "P", "P", "P"],
        "DPurp":      ["O", "W", "P", "S", "H", "T", "C", "L", "R", "K", "Z"],
    }
)
AGG_LD = pd.DataFrame(
    {"ChargeType": ["W", "H", "P", "W", "P"], "DPurp": ["Bus", "VFR", "Rec", "Com", "Oth"]}
)
AGG_LD_AE = pd.DataFrame(
    {
        "ChargeType": ["W", "H", "P", "W", "P", "P"],
        "DPurp": ["Bus", "VFR", "Rec", "Com", "Oth", "Access"],
    }
)


def _load_taz_dist_short(directory: str) -> pd.DataFrame:
    df = fread_all_in_dir(directory)
    if df.empty:
        return df
    return df[["from", "to", "distance"]].drop_duplicates()


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    # TAZ distance matrices (short columns only)
    taz_dist_ld = _load_taz_dist_short(PARSED_LDPTM_DIR)
    taz_dist_etm = _load_taz_dist_short(PARSED_ETM_DIR)

    # --- SDPTM ---------------------------------------------------------------
    all_sd = fread_all_in_dir(SDPTM_DIR)
    cols_short = ["SerialNo", "Person", "Tour", "Trip", "DPurp", "I", "J", "Mode", "Dist", "Time"]
    all_sd_short = all_sd[cols_short].copy()
    save_rds_like(all_sd_short, os.path.join(OUT_DIR, "SDPTM trips short.pkl"))

    evhh_sd = pd.read_csv(os.path.join(OUT_DIR, "EVhh_new_SDPTM_sample42.csv"))
    ev_sd = all_sd_short[
        all_sd_short["SerialNo"].isin(evhh_sd["hhID"])
        & all_sd_short["Mode"].isin(["SOV", "HOV2", "HOV3"])
    ]
    ev_sd = pd.merge(
        ev_sd,
        evhh_sd[["hhID", "year"]],
        left_on="SerialNo",
        right_on="hhID",
    ).drop(columns=["hhID"])
    ev_sd = pd.merge(ev_sd, AGG_SD, on="DPurp")
    save_rds_like(ev_sd, os.path.join(OUT_DIR, "EV trips_new_SDPTM_sample42.pkl"))

    # --- LDPTM ---------------------------------------------------------------
    evhh_ld = pd.read_csv(os.path.join(OUT_DIR, "EVhh_new_LDPTM_sample42.csv"))
    all_ld = pd.read_csv(LDPTM_TRIPS)
    ev_ld = all_ld[
        all_ld["SerialNo"].isin(evhh_ld["hhID"])
        & all_ld["Mode"].isin(["SOV", "HOV2", "HOV3"])
    ][cols_short].copy()
    ev_ld = pd.merge(
        ev_ld, evhh_ld[["hhID", "year"]], left_on="SerialNo", right_on="hhID"
    ).drop(columns=["hhID"])
    ev_ld = pd.merge(ev_ld, AGG_LD, on="DPurp")
    save_rds_like(ev_ld, os.path.join(OUT_DIR, "EV trips_new_LDPTM_sample42.pkl"))

    if not taz_dist_ld.empty:
        ev_ld_dist = pd.merge(
            ev_ld,
            taz_dist_ld,
            left_on=["I", "J"],
            right_on=["from", "to"],
            how="left",
        )
        print(ev_ld_dist["distance"].describe())

    # --- LDPTM AccEgr --------------------------------------------------------
    evhh_ld_ae = pd.read_csv(os.path.join(OUT_DIR, "EVhh_new_LDPTM_AccEgr_sample42.csv"))
    all_ld_ae = pd.read_csv(LDPTM_ACCEGR)
    mask_ae = all_ld_ae["SerialNo"].isin(evhh_ld_ae["hhID"]) & (
        ((all_ld_ae["DPurp"] == "Access") & (all_ld_ae["AccMode"] == "Drive_Park"))
        | ((all_ld_ae["OPurp"] == "Egress") & (all_ld_ae["EgrMode"] == "Rent_Car"))
    )
    ev_ld_ae = all_ld_ae.loc[
        mask_ae,
        ["SerialNo", "DPurp", "I", "J", "Mode", "AccMode", "EgrMode", "Time"],
    ].copy()
    ev_ld_ae = pd.merge(
        ev_ld_ae, evhh_ld_ae[["hhID", "year"]], left_on="SerialNo", right_on="hhID"
    ).drop(columns=["hhID"])
    ev_ld_ae = pd.merge(ev_ld_ae, AGG_LD_AE, on="DPurp")
    save_rds_like(ev_ld_ae, os.path.join(OUT_DIR, "EV trips_new_LDPTM_AccEgr_sample42.pkl"))

    # --- ETM -----------------------------------------------------------------
    evhh_etm = pd.read_csv(os.path.join(OUT_DIR, "EVhh_new_ETM_sample42.csv"))
    all_etm = pd.read_csv(ETM_TRIPS)
    mask_etm = (
        all_etm["SerialNo"].isin(evhh_etm["hhID"])
        & all_etm["ActorType"].isin(["CarLong", "CarLocal"])
        & all_etm["Mode"].isin(["SOV", "HOV2", "HOV3"])
        & (all_etm["DPurp"] == "I")
    )
    ev_etm = all_etm.loc[
        mask_etm,
        ["SerialNo", "Person", "Tour", "Trip", "DPurp", "I", "J", "Mode", "ActorType", "Time"],
    ].copy()
    ev_etm = pd.merge(
        ev_etm, evhh_etm[["hhID", "year"]], left_on="SerialNo", right_on="hhID"
    ).drop(columns=["hhID"])
    ev_etm["ChargeType"] = "P"
    ev_etm_dist = pd.merge(
        ev_etm, taz_dist_etm, left_on=["I", "J"], right_on=["from", "to"]
    )
    ev_etm_dist["Dist"] = ev_etm_dist["distance"] / 1609.344
    save_rds_like(ev_etm_dist, os.path.join(OUT_DIR, "EV trips_new_EXT_sample42.pkl"))


if __name__ == "__main__":
    main()
