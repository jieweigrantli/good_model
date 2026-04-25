"""
01_01_get_LDV_households_per_TAZ.py — Python port of ``01_01_get_LDV_households_per_TAZ.R``.

Collects unique (SerialNo, HomeZone) pairs of LDV households from:
- CSTDM SDPTM (Short-Distance Passenger Travel Model)
- CSTDM LDPTM (Long-Distance Passenger Travel Model)  + AccEgr
- CSTDM ETM (External Travel Model)

Outputs per-source "hh home TAZ" CSVs.
"""

from __future__ import annotations

import os

import pandas as pd

from common import fread_all_in_dir

# ---------------------------------------------------------------------------
# Paths (mirror the R script)
# ---------------------------------------------------------------------------
WORK_ROOT = r"E:\GitHub\good_model\Distribution-Grid-EV-CA"

SDPTM_DIR = r"E:\Data\CSTDM_OD/SDPTM"
LDPTM_TRIPS = r"E:\Data\CSTDM_OD/LDPTM/LDPTM_Trips.csv"
LDPTM_ACCEGR = r"E:\Data\CSTDM_OD/LDPTM/LDPTM_AccEgr.csv"
ETM_TRIPS = r"E:\Data\CSTDM_OD/ETM/trips_Ext.csv"

OUT_DIR = r"E:\GitHub\good_model\Distribution-Grid-EV-CA\data\mobility_data\CSTDM_processed"
LDV_MODES = {"SOV", "HOV2", "HOV3"}


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    # --- Short Distance Private Trips ---------------------------------------
    sd = fread_all_in_dir(SDPTM_DIR)
    sd_hh = sd.loc[sd["Mode"].isin(LDV_MODES), ["SerialNo", "HomeZone"]]
    sd_hh = sd_hh.drop_duplicates()
    sd_hh.to_csv(os.path.join(OUT_DIR, "SDPTM_hh_home_TAZ.csv"), index=False)

    # --- Long Distance Private Trips ----------------------------------------
    ld = pd.read_csv(LDPTM_TRIPS)
    ld_hh = ld.loc[ld["Mode"].isin(LDV_MODES), ["SerialNo", "HomeZone"]].drop_duplicates()
    ld_hh.to_csv(os.path.join(OUT_DIR, "LDPTM_hh_home_TAZ.csv"), index=False)

    ld_ae = pd.read_csv(LDPTM_ACCEGR)
    print("NA AccMode:", ld_ae["AccMode"].isna().sum())
    print("NA EgrMode:", ld_ae["EgrMode"].isna().sum())

    mask_ae = ((ld_ae["DPurp"] == "Access") & (ld_ae["AccMode"] == "Drive_Park")) | (
        (ld_ae["OPurp"] == "Egress") & (ld_ae["EgrMode"] == "Rent_Car")
    )
    ld_ae_hh = ld_ae.loc[mask_ae, ["SerialNo", "HomeZone"]].drop_duplicates()
    ld_ae_hh.to_csv(os.path.join(OUT_DIR, "LDPTM_AccEgr_hh_home_TAZ.csv"), index=False)

    # --- ETM (only LDV trips that enter CA) ---------------------------------
    etm = pd.read_csv(ETM_TRIPS)
    mask_etm = (
        etm["ActorType"].isin(["CarLong", "CarLocal"])
        & etm["Mode"].isin(LDV_MODES)
        & (etm["DPurp"] == "I")
    )
    etm_hh = etm.loc[mask_etm, ["SerialNo", "HomeZone"]].drop_duplicates()
    etm_hh.to_csv(os.path.join(OUT_DIR, "ETM_hh_home_TAZ.csv"), index=False)


if __name__ == "__main__":
    main()
