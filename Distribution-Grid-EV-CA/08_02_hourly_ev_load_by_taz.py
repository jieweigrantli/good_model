"""
08_02_hourly_ev_load_by_taz.py

Scale annual TAZ EV demand (kWh) by the fleet hourly shape to produce 8760-hour
load arrays for every TAZ, plus seasonal-week slices used by the 4-week test.

Writes:
  data/meso/taz_hourly_ev_8760.parquet
  data/meso/taz_ids.npy, taz_weights.npy, mean_diurnal_shape_kW.npy
  data/meso/seasonal/<week>/taz_hourly_kW.npy
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


def _write_wide_parquet(path: Path, ids, values: np.ndarray, id_col: str) -> None:
    """ids (n,) and values (n, T) → parquet with columns id_col, h0000, …"""
    n_h = values.shape[1]
    cols = {id_col: np.asarray(ids)}
    for h in range(n_h):
        cols[f"h{h:04d}"] = values[:, h]
    df = pd.DataFrame(cols)
    C.ensure_dir(path.parent)
    df.to_parquet(path, index=False)


def main() -> None:
    C.require_file(C.FLEET_DAILY_HOURLY_NPY, hint="Run the multiday charging module first.")
    C.require_file(C.TAZ_TOTAL_DEMAND_CSV, hint="Need multiday_charging/outputs/taz_total_demand_kwh.csv")

    print("Loading fleet daily-hourly load and TAZ annual demand...")
    fleet = np.load(C.FLEET_DAILY_HOURLY_NPY)  # [365, 24] kW
    taz_df = pd.read_csv(C.TAZ_TOTAL_DEMAND_CSV)
    taz_df["TAZ"] = taz_df["TAZ"].astype(int)
    taz_df = taz_df.sort_values("TAZ").reset_index(drop=True)

    weights = taz_df["total_kwh"].to_numpy(dtype=float)
    taz_ids = taz_df["TAZ"].to_numpy()
    print(
        f"  fleet shape={fleet.shape}; n_taz={len(taz_ids)}; "
        f"annual energy={weights.sum()/1e9:.2f} TWh-eq (kWh sum)"
    )

    mean_diurnal = fleet.mean(axis=0)
    out_dir = C.ensure_dir(C.MESO_DIR)
    np.save(out_dir / "taz_ids.npy", taz_ids)
    np.save(out_dir / "taz_weights.npy", weights)
    np.save(out_dir / "mean_diurnal_shape_kW.npy", mean_diurnal)

    # Full-year allocation: [n_taz, 8760] float32 via outer product of shares × fleet
    fleet_flat = fleet.reshape(-1)
    fleet_8760 = C.pad_or_wrap_hours(fleet_flat, C.HOURS_YEAR).astype(np.float32)
    total_w = float(weights.sum())
    shares = (weights / total_w).astype(np.float32) if total_w > 0 else np.zeros_like(weights, dtype=np.float32)
    taz_8760 = np.outer(shares, fleet_8760).astype(np.float32)
    _write_wide_parquet(C.TAZ_HOURLY_EV_8760, taz_ids, taz_8760, "TAZ")
    np.save(out_dir / "taz_hourly_ev_8760.npy", taz_8760)
    print(
        f"  wrote {C.TAZ_HOURLY_EV_8760} shape={taz_8760.shape} "
        f"peak_total={taz_8760.sum(axis=0).max()/1e6:.2f} GW"
    )

    season_dir = C.ensure_dir(out_dir / "seasonal")
    for week in C.SEASONAL_WEEKS:
        name = week["name"]
        start_h = int(week["start_hour"])
        d0 = _day_of_year_from_hour(start_h)
        days = [(d0 + i) % fleet.shape[0] for i in range(7)]
        week_fleet = fleet[days, :]
        zonal = scale_fleet_shape_by_zone_weights(week_fleet, weights)
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
        print(
            f"  {name}: wrote taz_hourly_kW.npy {flat.shape}, "
            f"peak_total={flat.sum(axis=0).max()/1e6:.2f} GW"
        )

    print(f"Done. Outputs under {out_dir}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
