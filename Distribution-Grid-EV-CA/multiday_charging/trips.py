"""
trips.py -- short-distance daily driving profiles with destination labels.

Two responsibilities:

1. (Optional) read the parent pipeline's SDPTM EV-trip table to derive a
   realistic per-vehicle *typical-day* profile: daily VMT and which charging
   destinations (Home / Work / Public) the vehicle actually visits. This can
   be used to calibrate the fleet.

2. Generate day-varying simulation inputs over ``n_days``:
   - ``vmt``            : [n_vehicles, n_days] miles driven each day
   - ``work_available`` : [n_vehicles, n_days] bool, can charge at work that day
   - ``public_available``: [n_vehicles, n_days] bool, has a public-eligible stop

The SDPTM trip table already carries destination labels through ``ChargeType``
(H/W/P), so when present it informs each vehicle's work/public propensity.
"""

from __future__ import annotations

import glob
import gc
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from config import MultiDayConfig
from common import read_rds_like, save_rds_like

# DPurp -> ChargeType mapping (same as build_multiday_inputs / 02_04).
_AGG_SD = pd.DataFrame(
    {
        "DPurp": ["O", "W", "P", "S", "H", "T", "C", "L", "R", "K", "Z"],
        "ChargeType": ["H", "W", "W", "P", "P", "P", "P", "P", "P", "P", "P"],
    }
)
_SDPTM_MODES = np.array(["SOV", "HOV2", "HOV3"], dtype=object)
_HH_IDS_CACHE_SUFFIX = "_hh_ids.npy"


def _hh_ids_cache_path(evhh_path: Path) -> Path:
    return evhh_path.with_name(evhh_path.stem + _HH_IDS_CACHE_SUFFIX)


def _load_ev_hh_ids(evhh_path: Path) -> np.ndarray:
    """Sorted unique EV household IDs (int32), with optional .npy cache."""
    cache = _hh_ids_cache_path(evhh_path)
    if cache.is_file():
        return np.load(cache)

    parts: list[np.ndarray] = []
    for chunk in pd.read_csv(
        evhh_path,
        usecols=["hhID"],
        chunksize=500_000,
        dtype={"hhID": "int32"},
    ):
        parts.append(chunk["hhID"].to_numpy(copy=False))

    if not parts:
        raise RuntimeError(f"No hhID rows in {evhh_path}")

    hh_ids = np.unique(np.concatenate(parts))
    np.save(cache, hh_ids)
    return hh_ids


def _profiles_from_trip_table(ev: pd.DataFrame) -> pd.DataFrame:
    """Aggregate trip rows to per-vehicle calibration profiles."""
    needed = {"SerialNo", "Dist", "ChargeType"}
    if not needed.issubset(ev.columns):
        raise ValueError(f"trip table missing columns; need {needed}")

    grp = ev.groupby("SerialNo", sort=False)
    prof = pd.DataFrame(
        {
            "daily_vmt": grp["Dist"].sum(),
            "has_work": grp["ChargeType"].apply(lambda s: bool((s == "W").any())),
            "has_public": grp["ChargeType"].apply(lambda s: bool((s == "P").any())),
        }
    ).reset_index(drop=True)
    return prof[prof["daily_vmt"] > 0].reset_index(drop=True)


def build_sd_trip_profiles(cfg: MultiDayConfig, *, save: bool = True) -> pd.DataFrame:
    """Build per-vehicle profiles by streaming SDPTM CSVs (low memory).

    Reads the same source files as ``build_multiday_inputs.build_sd_ev_trips``
    but never materialises the full ~24M-row trip table.
    """
    base = cfg.base_dir
    evhh_path = base / "data/mobility_data/CSTDM_processed/EVhh_new_SDPTM_sample42.csv"
    sdptm_dir = base / "data/mobility_data/CSTDM/SDPTM"
    if not evhh_path.is_file():
        raise FileNotFoundError(
            f"Missing {evhh_path}. Run 02_03_sample_EV_hh.py first."
        )

    print(f"Loading EV household IDs from {evhh_path.name} ...")
    hh_ids = _load_ev_hh_ids(evhh_path)
    print(f"  {len(hh_ids):,} unique hhIDs")

    files = sorted(glob.glob(str(sdptm_dir / "trips_*.csv")))
    if not files:
        raise FileNotFoundError(f"No SDPTM trip files under {sdptm_dir}")

    vmt_sum: dict[int, float] = defaultdict(float)
    has_work: dict[int, bool] = defaultdict(bool)
    has_public: dict[int, bool] = defaultdict(bool)

    cols = ["SerialNo", "DPurp", "Mode", "Dist"]
    for path in files:
        print(f"  scanning {Path(path).name} ...")
        for chunk in pd.read_csv(
            path,
            usecols=cols,
            chunksize=500_000,
            low_memory=False,
            dtype={"SerialNo": "int32", "Dist": "float32"},
        ):
            serial = chunk["SerialNo"].to_numpy(dtype=np.int32, copy=False)
            mode = chunk["Mode"].to_numpy(dtype=object, copy=False)
            mask = np.isin(serial, hh_ids) & np.isin(mode, _SDPTM_MODES)
            if not mask.any():
                continue
            sub = chunk.loc[mask, ["SerialNo", "DPurp", "Dist"]].copy()
            sub = sub.merge(_AGG_SD, on="DPurp", how="inner")
            agg = sub.groupby("SerialNo", sort=False).agg(
                vmt=("Dist", "sum"),
                hw=("ChargeType", lambda s: (s == "W").any()),
                hp=("ChargeType", lambda s: (s == "P").any()),
            )
            for sid, vmt, hw, hp in zip(
                agg.index.to_numpy(dtype=np.int64),
                agg["vmt"].to_numpy(),
                agg["hw"].to_numpy(dtype=bool),
                agg["hp"].to_numpy(dtype=bool),
            ):
                key = int(sid)
                vmt_sum[key] += float(vmt)
                has_work[key] = has_work[key] or bool(hw)
                has_public[key] = has_public[key] or bool(hp)
            del sub, agg, chunk
        gc.collect()

    if not vmt_sum:
        raise RuntimeError("No SDPTM trips matched sampled EV households.")

    prof = pd.DataFrame(
        {
            "daily_vmt": [vmt_sum[k] for k in vmt_sum],
            "has_work": [has_work[k] for k in vmt_sum],
            "has_public": [has_public[k] for k in vmt_sum],
        }
    )
    prof = prof[prof["daily_vmt"] > 0].reset_index(drop=True)

    if save:
        out = cfg.sd_trips_profiles_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        save_rds_like(prof, str(out))
        print(f"Wrote {len(prof):,} vehicle profiles -> {out}")

    return prof


def load_sd_trip_profiles(cfg: MultiDayConfig) -> pd.DataFrame | None:
    """Per-vehicle typical-day profile from real SDPTM EV trips, or None.

    Returns columns: ``daily_vmt``, ``has_work``, ``has_public``.

    Uses a small pre-aggregated profiles file when available.  If only the
    full trip pickle exists, builds profiles from SDPTM CSVs instead of loading
    the entire ~24M-row table into memory.
    """
    prof_path = cfg.sd_trips_profiles_path()
    if prof_path.is_file():
        return read_rds_like(str(prof_path))

    trips_path = cfg.sd_trips_path()
    if not trips_path.is_file():
        return None

    base = cfg.base_dir
    evhh_path = base / "data/mobility_data/CSTDM_processed/EVhh_new_SDPTM_sample42.csv"
    sdptm_dir = base / "data/mobility_data/CSTDM/SDPTM"
    if evhh_path.is_file() and sdptm_dir.is_dir() and any(sdptm_dir.glob("trips_*.csv")):
        print("Building SDPTM vehicle profiles from CSV (streaming, low memory) ...")
        return build_sd_trip_profiles(cfg, save=True)

    print("Loading full SDPTM trip pickle to derive profiles (may use a lot of RAM) ...")
    try:
        ev = read_rds_like(str(trips_path))
    except MemoryError as exc:
        raise MemoryError(
            "Could not load the full SDPTM trip pickle. Run:\n"
            "  python build_multiday_inputs.py --profiles-only\n"
            "from Distribution-Grid-EV-CA/ to build a lightweight profiles file."
        ) from exc

    prof = _profiles_from_trip_table(ev)
    del ev
    if len(prof):
        prof_path.parent.mkdir(parents=True, exist_ok=True)
        save_rds_like(prof, str(prof_path))
    return prof if len(prof) else None


def calibrate_fleet_from_trips(
    cfg: MultiDayConfig,
    fleet: pd.DataFrame,
    profiles: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Override fleet ``mean_daily_vmt`` and work access from real profiles.

    Each fleet vehicle is matched to a random sampled real profile.
    ``home_access`` is left as configured; ``work_access`` becomes the AND of
    configured access and the profile actually visiting work.
    """
    idx = rng.integers(0, len(profiles), size=len(fleet))
    sampled = profiles.iloc[idx].reset_index(drop=True)
    fleet = fleet.copy()
    fleet["mean_daily_vmt"] = sampled["daily_vmt"].to_numpy()
    fleet["work_access"] = fleet["work_access"].to_numpy() & sampled["has_work"].to_numpy()
    fleet["_has_public_profile"] = sampled["has_public"].to_numpy()
    return fleet


def build_daily_inputs(
    cfg: MultiDayConfig, fleet: pd.DataFrame, rng: np.random.Generator
) -> dict[str, np.ndarray]:
    """Generate day-varying VMT and location-availability arrays."""
    n = len(fleet)
    d = cfg.n_days

    # ----- daily VMT: personal mean * lognormal day-to-day noise -----------
    cv = cfg.vmt_day_to_day_cv
    sigma = np.sqrt(np.log(1.0 + cv * cv))
    # noise has median 1.0 (multiplicative, mean-preserving in log space)
    noise = rng.lognormal(mean=-0.5 * sigma * sigma, sigma=sigma, size=(n, d))
    base = fleet["mean_daily_vmt"].to_numpy(dtype=float)[:, None]
    vmt = (base * noise).astype(np.float32)

    # zero out no-travel days
    no_travel = rng.random((n, d)) < cfg.no_travel_prob
    vmt[no_travel] = 0.0

    # ----- work availability ------------------------------------------------
    work_access = fleet["work_access"].to_numpy(dtype=bool)[:, None]
    work_available = (rng.random((n, d)) < cfg.work_trip_prob) & work_access

    # ----- public availability ---------------------------------------------
    public_available = rng.random((n, d)) < cfg.public_trip_prob

    return {
        "vmt": vmt,
        "work_available": work_available,
        "public_available": public_available,
    }
