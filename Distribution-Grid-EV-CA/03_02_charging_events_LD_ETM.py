"""
03_02_charging_events_LD_ETM.py — Python port of
``03_02_charging events_LD_ETM.R``.

Builds long-distance (LDPTM) and ETM charging events using the TAZ-distance
matrix.  Implements the paper's "option 4" (charge at the TAZs that overlap
with the most trips, enabling charging every 100 miles) as the saved output.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common import fread_all_in_dir, read_rds_like, save_rds_like

PARSED_LDPTM_DIR = "data/mobility_data/TAZ_distance/parsed_100000_LDPTM"
PARSED_ETM_DIR = "data/mobility_data/TAZ_distance/parsed_100000_ETM"

EV_TRIPS_LD = "data/mobility_data/CSTDM_processed/EV trips_new_LDPTM_sample42.pkl"
EV_TRIPS_ETM = "data/mobility_data/CSTDM_processed/EV trips_new_EXT_sample42.pkl"
OUT_DIR = "data/mobility_data/CSTDM_processed"


def _prep_distance_table(directory: str) -> pd.DataFrame:
    dist = fread_all_in_dir(directory)
    if dist.empty:
        return dist
    dist = dist.rename(columns={"from": "I", "to": "J", "TAZ12,": "TAZway"})
    return dist


def _prep_trips(path: str) -> pd.DataFrame:
    ev = read_rds_like(path)
    ev = ev.sort_values(["year", "SerialNo", "Person", "Tour", "Trip"]).reset_index(drop=True)
    ev["TripID"] = np.arange(1, len(ev) + 1)
    return ev[["TripID", "year", "I", "J", "Dist", "Time", "ChargeType"]].copy()


def _events_option4(dist: pd.DataFrame, trips: pd.DataFrame, ldv: bool) -> pd.DataFrame:
    """Charge at TAZs with the most overlap — at least ceil(Dist/100) stops."""
    dist = dist.copy()
    dist["TAZfreq"] = dist.groupby("TAZway")["TAZway"].transform("size")
    taz_freq = dist[["TAZway", "TAZfreq"]].drop_duplicates()

    routes = pd.merge(
        dist[["TAZway", "I", "J"]],
        trips,
        on=["I", "J"],
        how="left" if not ldv else "inner",
    )
    routes = pd.merge(routes, taz_freq, on="TAZway")

    routes["nCharge"] = np.ceil(routes["Dist"] / 100).astype(int)
    routes["nTAZtrip"] = routes.groupby("TripID")["TAZway"].transform("size")
    routes["qtCUT"] = 1 - routes["nCharge"] / routes["nTAZtrip"]

    def _qcut(g: pd.DataFrame) -> pd.Series:
        # single-scalar quantile for the trip
        q = g["qtCUT"].iloc[0]
        return pd.Series(np.quantile(g["TAZfreq"].to_numpy(dtype=float), max(0.0, min(1.0, q))), index=g.index)

    routes["freqCUT"] = routes.groupby("TripID", group_keys=False).apply(_qcut)

    mask = routes["TAZfreq"] > routes["freqCUT"] if ldv else routes["TAZfreq"] >= routes["freqCUT"]
    events = routes.loc[mask].copy()
    events["nTAZcharge"] = events.groupby("TripID")["TAZway"].transform("size")
    events["nDelta"] = events["nTAZcharge"] - events["nCharge"]
    events["chargeDist"] = events["Dist"] / events["nTAZcharge"]
    return events


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    # --- LDPTM ---------------------------------------------------------------
    dist_ld = _prep_distance_table(PARSED_LDPTM_DIR)
    ev_ld = _prep_trips(EV_TRIPS_LD)
    events_ld = _events_option4(dist_ld, ev_ld, ldv=True)
    events_ld["ChargeEventType"] = "P"
    events_ld.loc[events_ld["TAZway"] == events_ld["J"], "ChargeEventType"] = events_ld.loc[
        events_ld["TAZway"] == events_ld["J"], "ChargeType"
    ]
    events_ld["other"] = "corridor"
    events_ld.loc[events_ld["TAZway"] == events_ld["J"], "other"] = "destination"
    events_ld = events_ld[
        ["TripID", "year", "TAZway", "I", "J", "Time", "chargeDist", "ChargeEventType", "other"]
    ]
    save_rds_like(
        events_ld, os.path.join(OUT_DIR, "charge_event_new_LDPTM_sample42.pkl")
    )

    # --- ETM -----------------------------------------------------------------
    dist_etm = _prep_distance_table(PARSED_ETM_DIR)
    ev_etm = _prep_trips(EV_TRIPS_ETM)
    events_etm = _events_option4(dist_etm, ev_etm, ldv=False)
    events_etm = events_etm[
        ["TripID", "year", "TAZway", "I", "J", "Time", "chargeDist", "ChargeType"]
    ]
    save_rds_like(
        events_etm, os.path.join(OUT_DIR, "charge_event_new_ETM_sample42.pkl")
    )


if __name__ == "__main__":
    main()
