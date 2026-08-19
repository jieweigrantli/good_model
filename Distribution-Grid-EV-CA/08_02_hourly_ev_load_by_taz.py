"""
08_02_hourly_ev_load_by_taz.py

Build L^{EV}_{i,t} without storing 2.5M sessions: allocate annual TAZ kWh
shares against the fleet daily-hourly load shape.

Writes under data/meso/:
  - taz_ids.npy
  - taz_weights.npy
  - seasonal/<week>/taz_hourly_kW.npy   shape [n_taz, 168]
  - mean_diurnal_shape_kW.npy           shape [24]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import common as C

sys.path.insert(0, str(Path(__file__).resolve().parent / "multiday_charging"))
from load_profile import scale_fleet_shape_by_zone_weights  # noqa: E402


def _day_of_year_from_hour(start_hour: int) -> int:
    return int(start_hour) // 24


def main() -> None:
    print("Loading fleet daily-hourly load and TAZ annual demand...")
    fleet = np.load(C.FLEET_DAILY_HOURLY_NPY)  # [365, 24] kW
    taz_df = pd.read_csv(C.TAZ_TOTAL_DEMAND_CSV)
    taz_df["TAZ"] = taz_df["TAZ"].astype(int)
    taz_df = taz_df.sort_values("TAZ").reset_index(drop=True)

    weights = taz_df["total_kwh"].to_numpy(dtype=float)
    taz_ids = taz_df["TAZ"].to_numpy()

    print(f"  fleet shape={fleet.shape}; n_taz={len(taz_ids)}; "
          f"annual energy={weights.sum()/1e9:.2f} TWh-eq (kWh sum)")

    # Full-year zonal allocation (kept for diagnostics; large but manageable)
    # [n_taz, 365, 24] would be ~1.8 GB float64 — write seasonal weeks only.
    mean_diurnal = fleet.mean(axis=0)

    out_dir = C.ensure_dir(C.MESO_DIR)
    np.save(out_dir / "taz_ids.npy", taz_ids)
    np.save(out_dir / "taz_weights.npy", weights)
    np.save(out_dir / "mean_diurnal_shape_kW.npy", mean_diurnal)

    season_dir = C.ensure_dir(out_dir / "seasonal")
    for week in C.SEASONAL_WEEKS:
        name = week["name"]
        start_h = int(week["start_hour"])
        d0 = _day_of_year_from_hour(start_h)
        # 7 consecutive calendar days from d0 (wrap if needed)
        days = [(d0 + i) % fleet.shape[0] for i in range(7)]
        week_fleet = fleet[days, :]  # [7, 24]
        zonal = scale_fleet_shape_by_zone_weights(week_fleet, weights)
        # reshape to [n_taz, 168]
        n_taz = zonal.shape[0]
        flat = zonal.reshape(n_taz, 7 * 24)

        wdir = C.ensure_dir(season_dir / name)
        np.save(wdir / "taz_hourly_kW.npy", flat.astype(np.float32))
        meta = {
            "season": name,
            "start_hour": start_h,
            "days": days,
            "unit": "kW",
            "shape": list(flat.shape),
        }
        with open(wdir / "meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        print(f"  {name}: wrote taz_hourly_kW.npy {flat.shape}, "
              f"peak_total={flat.sum(axis=0).max()/1e6:.2f} GW")

    print(f"Done. Outputs under {out_dir}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
