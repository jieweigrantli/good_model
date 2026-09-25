"""
10_07_peak_overload_analysis.py

Substation overload by capacity-threshold comparison, following Li & Jenn
(2024, PNAS) rather than the dispatch LP.

Why this exists alongside the LP
--------------------------------
Their method needs no optimisation: add projected EV charging to the
published baseload on each facility, and test whether the total exceeds the
published capacity, hour by hour. That sidesteps every solver problem we have
had -- no crossover, no time limits, no conditioning -- and it is directly
comparable to a published result.

It answers a narrower question than the LP. It says which substations are
over their rating and by how much; it cannot say what generation responds,
what it emits, or whether transmission could have delivered the energy. The
LP answers those and this does not, so the two are complements.

Method, matching theirs where our data allows
---------------------------------------------
* Baseload is the published **high** value per month-hour, not a midpoint.
  Li & Jenn use `load_high` because this is a peak-adequacy test. Measured
  against NREL county demand for PG&E territory, `high` recovers a 15.59 GW
  coincident peak against NREL's 20.41 GW and PG&E's real ~20-21 GW, while a
  midpoint gives 13.02 GW and understates by a third.
* Capacity is the published transformer bank rating per substation.
* Their unit is the feeder and ours is the substation, because our EV
  allocation resolves to substations.

Writes:
  data/results/peak_overload_by_substation.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio

import common as C

OUT_CSV = C.RESULTS_DIR / "peak_overload_by_substation.csv"


def measured_baseload_high() -> pd.DataFrame:
    """Published substation baseload, month-hour, in W. PG&E and SCE."""
    frames = []

    path = C.grip_layer("SubstationLoadProfile")
    if path.is_file():
        sp = pyogrio.read_dataframe(
            str(path), columns=["subid", "monthhour", "high"], read_geometry=False
        )
        sp["high"] = pd.to_numeric(sp["high"], errors="coerce")
        sp = sp.dropna(subset=["high"])
        sp["substation_id"] = sp["subid"].astype(str).str.strip().str.zfill(5)
        sp["month"] = sp["monthhour"].str[:2].astype(int)
        sp["hour"] = sp["monthhour"].str[3:].astype(int)
        sp["base_W"] = sp["high"].clip(lower=0) * 1e3
        frames.append(sp[["substation_id", "month", "hour", "base_W"]])

    sce_lp = C.DATA_DIR / "ica" / "sce" / "substation_load_profile.parquet"
    sce_ss = C.DATA_DIR / "ica" / "sce" / "substations.parquet"
    if sce_lp.is_file() and sce_ss.is_file():
        lp = pd.read_parquet(sce_lp)
        ss = pd.read_parquet(sce_ss)
        kv = (
            ss.assign(sec_kv=pd.to_numeric(
                ss["SUBSTATION_VOLTAGE"].astype(str).str.extract(r"/\s*([0-9.]+)")[0],
                errors="coerce"))
            .groupby("SUB_NAME")["sec_kv"].max()
        )
        lut = C.sce_substation_nodes()
        lp["MAX_LOAD"] = pd.to_numeric(lp["MAX_LOAD"], errors="coerce")
        lp = lp.dropna(subset=["MAX_LOAD"])
        lp["substation_id"] = lp["SUBSTATION"].map(
            lambda s: lut.get(str(s).strip().upper())
        )
        lp["kv"] = lp["SUBSTATION"].map(kv)
        lp = lp.dropna(subset=["substation_id", "kv"])
        # SCE publishes amperes; convert with the substation's secondary kV.
        lp["base_W"] = lp["MAX_LOAD"].clip(lower=0) * lp["kv"] * 1e3 * np.sqrt(3.0)
        lp["month"] = lp["MONTH"].astype(int) % 12 + 1
        lp["hour"] = lp["HOUR"].astype(int) % 24
        frames.append(lp[["substation_id", "month", "hour", "base_W"]])

    if not frames:
        return pd.DataFrame(columns=["substation_id", "month", "hour", "base_W"])
    return pd.concat(frames, ignore_index=True)


def ev_by_month_hour() -> pd.DataFrame:
    """EV load per substation per month-hour [W], from the 8760 allocation."""
    ids = np.load(C.MESO_DIR / "substation_ids.npy", allow_pickle=True).astype(str)
    ev_kw = np.load(C.MESO_DIR / "substation_hourly_ev_kW_8760.npy")
    hours = pd.date_range("2019-01-01", periods=ev_kw.shape[1], freq="h")
    mo, hh = hours.month.to_numpy(), hours.hour.to_numpy()

    rows = []
    for m in range(1, 13):
        for h in range(24):
            sel = (mo == m) & (hh == h)
            if not sel.any():
                continue
            rows.append(pd.DataFrame({
                "substation_id": ids,
                "month": m,
                "hour": h,
                # peak within the month-hour, matching the baseload convention
                "ev_W": ev_kw[:, sel].max(axis=1) * 1e3,
            }))
    return pd.concat(rows, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ev-scale", type=float, default=1.0,
                    help="Multiply EV load, to stand in for a future adoption year.")
    args = ap.parse_args()

    print("Loading published baseload (high)...")
    base = measured_baseload_high()
    print(f"  {base['substation_id'].nunique():,} substations, {len(base):,} month-hours")

    print("Loading published capacity...")
    cap = C.published_substation_ratings()
    cap["substation_id"] = cap["substation_id"].astype(str)
    print(f"  {len(cap):,} substations with a published rating")

    print("Loading EV allocation...")
    ev = ev_by_month_hour()
    ev["substation_id"] = ev["substation_id"].astype(str)
    if args.ev_scale != 1.0:
        ev["ev_W"] *= args.ev_scale
        print(f"  EV scaled x{args.ev_scale}")

    df = base.merge(ev, on=["substation_id", "month", "hour"], how="left")
    df["ev_W"] = df["ev_W"].fillna(0.0)
    df = df.merge(cap, on="substation_id", how="inner")

    df["total_W"] = df["base_W"] + df["ev_W"]
    df["headroom_W"] = df["rating_W"] - df["base_W"]
    df["overload_W"] = (df["total_W"] - df["rating_W"]).clip(lower=0)

    n_sub = df["substation_id"].nunique()
    per = df.groupby("substation_id").agg(
        peak_base_W=("base_W", "max"),
        peak_total_W=("total_W", "max"),
        rating_W=("rating_W", "first"),
        max_overload_W=("overload_W", "max"),
        overload_hours=("overload_W", lambda s: int((s > 0).sum())),
    )
    per["loading_base"] = per["peak_base_W"] / per["rating_W"]
    per["loading_total"] = per["peak_total_W"] / per["rating_W"]

    C.ensure_dir(OUT_CSV.parent)
    per.to_csv(OUT_CSV)

    ov_base = int((per["peak_base_W"] > per["rating_W"]).sum())
    ov_tot = int((per["peak_total_W"] > per["rating_W"]).sum())
    print(f"\n  substations analysed: {n_sub:,}")
    print(f"  overloaded by baseload alone : {ov_base:,} ({100 * ov_base / n_sub:.1f}%)")
    print(f"  overloaded with EV added     : {ov_tot:,} ({100 * ov_tot / n_sub:.1f}%)")
    print(f"  EV-induced additional        : {ov_tot - ov_base:,}")
    print(f"\n  upgrade capacity implied (sum of max overload): "
          f"{per['max_overload_W'].sum() / 1e9:.2f} GW")
    print(f"    of which EV-attributable: "
          f"{(per['max_overload_W'].sum() - (per['peak_base_W'] - per['rating_W']).clip(lower=0).sum()) / 1e9:.2f} GW")
    print(f"\n  median loading, baseload only: {per['loading_base'].median():.2f}")
    print(f"  median loading, with EV      : {per['loading_total'].median():.2f}")
    print(f"\n  wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
