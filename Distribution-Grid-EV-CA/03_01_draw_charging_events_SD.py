"""
03_01_draw_charging_events_SD.py — Python port of
``03_01_draw_charging events_SD.R``.

For each tour in the SDPTM EV trip table:
1. Build possible charging-location choices (H/W/P) and merge their empirical
   survey shares.
2. Draw one charging-location combination per tour weighted by Prob.
3. Identify which trips have charging events and compute per-event distance.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common import read_rds_like, save_rds_like

IN_PATH = "data/mobility_data/CSTDM_processed/EV trips_new_SDPTM_sample42.pkl"
OUT_DIR = "data/mobility_data/CSTDM_processed"


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    ev = read_rds_like(IN_PATH)

    # TourID: a single integer per (SerialNo, Person, Tour)
    ev["TourID"] = ev.groupby(["SerialNo", "Person", "Tour"], sort=False).ngroup() + 1
    ev = ev[["TourID", "Trip", "J", "Dist", "Time", "year", "ChargeType"]].copy()

    # Empirical choice-share table (each row = one possible H/W/P combination)
    survey_share = pd.DataFrame(
        {
            "H":     [1, 0, 0, 1, 1, 0, 1],
            "W":     [0, 1, 0, 1, 0, 1, 1],
            "P":     [0, 0, 1, 0, 1, 1, 1],
            "Share": [0.53, 0.08, 0.03, 0.16, 0.13, 0.03, 0.04],
        }
    )

    # For each tour, which charging types are possible (any trip in tour)?
    tour_types = ev[["TourID", "ChargeType"]].drop_duplicates()
    pivoted = (
        tour_types.assign(val=1)
        .pivot_table(
            index="TourID",
            columns="ChargeType",
            values="val",
            aggfunc="max",
            fill_value=0,
        )
        .reset_index()
    )
    for c in ("H", "W", "P"):
        if c not in pivoted.columns:
            pivoted[c] = 0
    pivoted = pivoted.rename(columns={"H": "Htype", "W": "Wtype", "P": "Ptype"})

    # Repeat each tour 7 times (one row per possible charging-location choice).
    pivoted = pivoted.loc[pivoted.index.repeat(7)].reset_index(drop=True)
    n_tours = len(pivoted) // 7
    pivoted["Hloc"] = np.tile([1, 0, 0, 1, 1, 0, 1], n_tours)
    pivoted["Wloc"] = np.tile([0, 1, 0, 1, 0, 1, 1], n_tours)
    pivoted["Ploc"] = np.tile([0, 0, 1, 0, 1, 1, 1], n_tours)

    pivoted["H"] = pivoted["Htype"] * pivoted["Hloc"]
    pivoted["W"] = pivoted["Wtype"] * pivoted["Wloc"]
    pivoted["P"] = pivoted["Ptype"] * pivoted["Ploc"]
    pivoted = pivoted[["TourID", "H", "W", "P"]].drop_duplicates()

    tour_types = pd.merge(pivoted, survey_share, on=["H", "W", "P"])
    tour_types["one"] = tour_types.groupby("TourID")["Share"].transform("sum")
    tour_types["Prob"] = tour_types["Share"] / tour_types["one"]

    save_rds_like(
        tour_types,
        os.path.join(OUT_DIR, "charge_choice_tour_SDPTM_sample42.pkl"),
    )

    # --- Draw one location combination per tour (weighted) -----------------
    rng = np.random.default_rng(12)

    def _draw_one(g: pd.DataFrame) -> pd.DataFrame:
        probs = g["Prob"].to_numpy(dtype=float)
        probs = probs / probs.sum()
        idx = rng.choice(len(g), size=1, p=probs)
        return g.iloc[idx]

    tour_choice = (
        tour_types.groupby("TourID", sort=False, group_keys=False).apply(_draw_one)
    ).reset_index(drop=True)

    # --- Identify trips that have a charging event (melt + filter) ---------
    ct = pd.merge(
        ev, tour_choice[["H", "W", "P", "TourID"]], on="TourID"
    )
    ct_long = ct.melt(
        id_vars=["TourID", "Trip", "ChargeType"],
        value_vars=["H", "W", "P"],
        var_name="variable",
        value_name="value",
    )
    charge_trip = ct_long[(ct_long["value"] == 1) & (ct_long["ChargeType"] == ct_long["variable"])].copy()

    # Charging-event index within each tour
    charge_trip = charge_trip.sort_values(["TourID", "Trip"]).reset_index(drop=True)
    charge_trip["chargeEvent"] = charge_trip.groupby("TourID").cumcount() + 1
    save_rds_like(
        charge_trip,
        os.path.join(OUT_DIR, "charge_trip_SDPTM_sample42_draw12.pkl"),
    )

    # Roll trips to the next charging event (R: data.table roll=-Inf)
    # For every trip in ev we assign the nearest *upcoming* chargeEvent in its tour.
    ct_merge = charge_trip[["TourID", "Trip", "chargeEvent"]].copy()
    all_trips = ev[["TourID", "Trip", "ChargeType", "Dist"]].copy().sort_values(["TourID", "Trip"])

    # For each (TourID, Trip) find the chargeEvent with Trip >= current trip.
    def _backfill(group: pd.DataFrame) -> pd.DataFrame:
        ev_events = ct_merge[ct_merge["TourID"] == group.name][["Trip", "chargeEvent"]].sort_values("Trip")
        if ev_events.empty:
            group["chargeEvent"] = np.nan
            return group
        # merge_asof requires sorted; direction='forward' picks next >= trip
        return pd.merge_asof(
            group.sort_values("Trip"),
            ev_events,
            on="Trip",
            direction="forward",
        )

    # Faster: use merge_asof per TourID via groupby/apply would scale poorly;
    # instead do it globally with `by='TourID'`.
    all_trips = all_trips.sort_values(["TourID", "Trip"])
    charge_events_sorted = ct_merge.sort_values(["TourID", "Trip"])
    ct_all = pd.merge_asof(
        all_trips,
        charge_events_sorted,
        by="TourID",
        on="Trip",
        direction="forward",
    )
    ct_all = ct_all.sort_values(["TourID", "Trip"]).reset_index(drop=True)
    save_rds_like(
        ct_all,
        os.path.join(OUT_DIR, "charge_trip_all_SDPTM_sample42_draw12.pkl"),
    )

    # Sum the trip distances per charging event
    charge_event = (
        ct_all.dropna(subset=["chargeEvent"])
        .groupby(["TourID", "chargeEvent"], as_index=False)["Dist"]
        .sum()
        .rename(columns={"Dist": "chargeDist"})
    )
    charge_event = pd.merge(
        charge_event,
        charge_trip[["TourID", "chargeEvent", "Trip", "ChargeType"]],
        on=["TourID", "chargeEvent"],
    )
    charge_event = pd.merge(
        charge_event,
        ev[["TourID", "Trip", "J", "Time", "year"]],
        on=["TourID", "Trip"],
    )
    save_rds_like(
        charge_event,
        os.path.join(OUT_DIR, "charge_event_new_SDPTM_sample42_draw12.pkl"),
    )


if __name__ == "__main__":
    main()
