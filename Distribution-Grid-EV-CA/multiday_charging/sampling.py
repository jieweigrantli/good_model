"""
sampling.py -- SOC-aware multi-day charging sampler.

Core idea
---------
Instead of forcing a charging event for every qualifying trip, each vehicle
accumulates energy demand as it drives and only charges when either:

  (a) its **preferred interval** has elapsed (``interval_pref_days`` drawn from
      the charge-frequency probability matrix), or
  (b) its **battery would be depleted** (cumulative energy since the last
      charge reaches the usable pack energy) -- this is where battery capacity
      enters: a larger pack can skip more days.

When a charge is triggered, a location (home/work/public) is chosen among the
feasible set, the recharge energy (= depletion since last charge, capped at the
usable pack) is binned, and an empirical session of the matching
``(charge_type, sub-type, bin)`` is sampled to provide ``start_hour``,
``end_hour`` and ``power``.

The simulation is vectorised across vehicles and loops over days.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import MultiDayConfig
from choice import LOCATIONS, choose_locations, location_weight_vector, sample_home_housing, sample_public_levels

_LOC_TO_CODE = {"home": 0, "work": 1, "public": 2}
_CODE_TO_LOC = {v: k for k, v in _LOC_TO_CODE.items()}


def _bin_of(energy: np.ndarray, cfg: MultiDayConfig) -> np.ndarray:
    inner = np.array(cfg.bin_edges[1:-1], dtype=float)  # [5,10,15,20,30,50,80]
    return np.digitize(energy, inner, right=True) + 1    # -> 1..8


class _PoolIndex:
    """Fast candidate lookup into the session pool with graceful fallbacks."""

    def __init__(self, pool: pd.DataFrame):
        self.start = pool["start_hour"].to_numpy()
        self.end = pool["end_hour"].to_numpy()
        self.power = pool["power"].to_numpy()
        self.energy = pool["energy"].to_numpy()
        self.level = pool["charger_level"].astype(str).to_numpy()
        # coerce missing housing to "" so equality/np.unique are well-defined
        self.housing = pool["housing"].fillna("").astype(str).to_numpy()
        ctype = pool["charge_type"].to_numpy()
        b = pool["bin"].to_numpy()

        self._exact: dict[tuple, np.ndarray] = {}
        self._by_type_bin: dict[tuple, np.ndarray] = {}
        self._by_type: dict[str, np.ndarray] = {}

        idx = np.arange(len(pool))
        for ct in np.unique(ctype):
            ct_mask = ctype == ct
            self._by_type[str(ct)] = idx[ct_mask]
            for bb in np.unique(b[ct_mask]):
                tb_mask = ct_mask & (b == bb)
                self._by_type_bin[(str(ct), int(bb))] = idx[tb_mask]

        # exact keys: home -> housing, public -> level, work -> ""
        for ct, sub_arr in (("home", self.housing), ("public", self.level)):
            ct_mask = ctype == ct
            for bb in np.unique(b[ct_mask]):
                for sub in np.unique(sub_arr[ct_mask]):
                    mask = ct_mask & (b == bb) & (sub_arr == sub)
                    if mask.any():
                        self._exact[(ct, str(sub), int(bb))] = idx[mask]

    def candidates(self, charge_type: str, sub: str, b: int) -> np.ndarray:
        key = (charge_type, str(sub), int(b))
        arr = self._exact.get(key)
        if arr is not None and arr.size:
            return arr
        arr = self._by_type_bin.get((charge_type, int(b)))
        if arr is not None and arr.size:
            return arr
        return self._by_type.get(charge_type, np.arange(0))


def _sample_pool_sessions(
    pidx: _PoolIndex,
    location: np.ndarray,
    sub: np.ndarray,
    bins: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Draw start/end/power/energy from the pool; pure NumPy (no pandas)."""
    n = location.shape[0]
    start = np.empty(n, dtype=float)
    end = np.empty(n, dtype=float)
    power = np.empty(n, dtype=float)
    esess = np.empty(n, dtype=float)

    loc_code = np.zeros(n, dtype=np.int8)
    loc_code[location == "work"] = 1
    loc_code[location == "public"] = 2

    sub_s = np.empty(n, dtype="U32")
    sub_s[loc_code == 0] = np.asarray(sub[loc_code == 0], dtype="U32")
    sub_s[loc_code == 1] = ""
    sub_s[loc_code == 2] = np.asarray(sub[loc_code == 2], dtype="U32")

    grp = np.empty(
        n,
        dtype=[("lc", "i1"), ("bin", "i1"), ("sub", "U32")],
    )
    grp["lc"] = loc_code
    grp["bin"] = bins.astype(np.int8)
    grp["sub"] = sub_s

    _, inv = np.unique(grp, return_inverse=True)
    for gi in range(int(inv.max()) + 1):
        rows = np.where(inv == gi)[0]
        ct = _CODE_TO_LOC[int(grp["lc"][rows[0]])]
        subv = str(grp["sub"][rows[0]])
        b = int(grp["bin"][rows[0]])
        cand = pidx.candidates(ct, subv, b)
        if cand.size == 0:
            start[rows] = np.nan
            end[rows] = np.nan
            power[rows] = np.nan
            esess[rows] = np.nan
            continue
        pick = rng.choice(cand, size=rows.size, replace=True)
        start[rows] = pidx.start[pick]
        end[rows] = pidx.end[pick]
        power[rows] = pidx.power[pick]
        esess[rows] = pidx.energy[pick]

    return start, end, power, esess


def _accumulate_day_load(
    load: np.ndarray,
    day: int,
    start: np.ndarray,
    end: np.ndarray,
    power: np.ndarray,
    loc_code: np.ndarray | None = None,
    load_by_type: dict[str, np.ndarray] | None = None,
) -> None:
    """Add session power into ``load[day, :]`` (and optional per-type matrices)."""
    valid = np.isfinite(start) & np.isfinite(power)
    if not valid.any():
        return

    s = start[valid].astype(np.int32)
    e = end[valid].astype(np.int32)
    p = power[valid]
    lc = loc_code[valid] if loc_code is not None else None

    same_day = s <= e
    for h in range(24):
        active = (same_day & (s <= h) & (h <= e)) | (
            ~same_day & ((s <= h) | (h <= e))
        )
        if not active.any():
            continue
        np.add.at(load, (np.full(active.sum(), day), np.full(active.sum(), h)), p[active])
        if load_by_type is not None and lc is not None:
            for code, name in _CODE_TO_LOC.items():
                mask = active & (lc == code)
                if mask.any():
                    np.add.at(
                        load_by_type[name],
                        (np.full(mask.sum(), day), np.full(mask.sum(), h)),
                        p[mask],
                    )


def _init_taz_tracker(
    fleet: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Return per-TAZ accumulators and vehicle→TAZ index when ``home_taz`` exists."""
    if "home_taz" not in fleet.columns:
        return None

    taz_vals, vehicle_taz = np.unique(
        fleet["home_taz"].to_numpy(), return_inverse=True
    )
    n_taz = len(taz_vals)
    taz_demand = np.zeros((n_taz, 3), dtype=np.float64)  # home, work, public
    weights = np.bincount(vehicle_taz, minlength=n_taz).astype(np.float64)
    weights /= weights.sum()
    return taz_vals, vehicle_taz.astype(np.int32), taz_demand, weights


def _accumulate_taz_demand(
    taz_demand: np.ndarray,
    vehicle_taz: np.ndarray,
    taz_weights: np.ndarray,
    idx: np.ndarray,
    location: np.ndarray,
    demand: np.ndarray,
    rng: np.random.Generator,
) -> None:
    """Add session recharge energy (kWh) to TAZ buckets."""
    is_home = location == "home"
    is_work = location == "work"
    is_pub = location == "public"

    if is_home.any():
        np.add.at(taz_demand[:, 0], vehicle_taz[idx[is_home]], demand[is_home])
    if is_work.any():
        dest = rng.choice(len(taz_weights), size=int(is_work.sum()), p=taz_weights)
        np.add.at(taz_demand[:, 1], dest, demand[is_work])
    if is_pub.any():
        dest = rng.choice(len(taz_weights), size=int(is_pub.sum()), p=taz_weights)
        np.add.at(taz_demand[:, 2], dest, demand[is_pub])


def _taz_demand_frame(
    taz_vals: np.ndarray, taz_demand: np.ndarray
) -> pd.DataFrame:
    total = taz_demand.sum(axis=1)
    out = pd.DataFrame(
        {
            "TAZ": taz_vals,
            "home_kwh": taz_demand[:, 0],
            "work_kwh": taz_demand[:, 1],
            "public_kwh": taz_demand[:, 2],
            "total_kwh": total,
        }
    )
    return out.sort_values("total_kwh", ascending=False).reset_index(drop=True)


@dataclass
class SimulateResult:
    """Outputs from ``simulate``; ``sessions`` is None when ``store_sessions=False``."""

    sessions: pd.DataFrame | None
    load: np.ndarray
    load_by_type: dict[str, np.ndarray]
    summary: pd.DataFrame
    taz_demand_kwh: pd.DataFrame | None = None


def _estimate_session_count(cfg: MultiDayConfig, fleet: pd.DataFrame) -> int:
    mean_pref = float(fleet["interval_pref_days"].mean())
    return int(cfg.n_vehicles * cfg.n_days / max(mean_pref, 1.0))


def simulate(
    cfg: MultiDayConfig,
    fleet: pd.DataFrame,
    daily: dict[str, np.ndarray],
    pool: pd.DataFrame,
    rng: np.random.Generator,
    *,
    store_sessions: bool | None = None,
) -> pd.DataFrame | SimulateResult:
    """Run the multi-day simulation.

    With ``store_sessions=True`` (default for small fleets), returns a long
    session table.  With ``store_sessions=False``, returns ``SimulateResult``
    with hourly load matrices only — required at CA fleet scale.

    Session columns (when stored):
      vehicle_id, day, charge_type, charger_level, housing,
      start_hour, end_hour, power, energy_demand, energy_session
    """
    if store_sessions is None:
        store_sessions = cfg.store_sessions

    est = _estimate_session_count(cfg, fleet)
    if store_sessions and est > 30_000_000:
        raise MemoryError(
            f"Estimated ~{est:,} charging sessions for {cfg.n_vehicles:,} vehicles "
            f"over {cfg.n_days} days. Set cfg.store_sessions=False (or reduce "
            f"n_vehicles) to build hourly load without materialising sessions."
        )

    n = len(fleet)
    n_days = cfg.n_days

    usable = fleet["usable_kwh"].to_numpy(dtype=float)
    eff = fleet["eff_mi_per_kwh"].to_numpy(dtype=float)
    pref = fleet["interval_pref_days"].to_numpy(dtype=int)
    home_access = fleet["home_access"].to_numpy(dtype=bool)

    vmt = daily["vmt"]
    work_avail = daily["work_available"]
    public_avail = daily["public_available"]

    base_w = location_weight_vector(cfg)
    pidx = _PoolIndex(pool)

    cum_e = np.zeros(n, dtype=float)
    days_since = np.zeros(n, dtype=int)

    load = np.zeros((n_days, 24), dtype=float)
    load_by_type = {ct: np.zeros((n_days, 24), dtype=float) for ct in LOCATIONS}

    out_vehicle: list[np.ndarray] = []
    out_day: list[np.ndarray] = []
    out_ctype: list[np.ndarray] = []
    out_level: list[np.ndarray] = []
    out_housing: list[np.ndarray] = []
    out_start: list[np.ndarray] = []
    out_end: list[np.ndarray] = []
    out_power: list[np.ndarray] = []
    out_demand: list[np.ndarray] = []
    out_esess: list[np.ndarray] = []

    n_sessions = 0
    ctype_counts = {ct: 0 for ct in LOCATIONS}
    sum_esess = 0.0

    taz_tracker = _init_taz_tracker(fleet)
    if taz_tracker is not None:
        taz_vals, vehicle_taz, taz_demand, taz_weights = taz_tracker
    else:
        taz_vals = taz_demand = taz_weights = vehicle_taz = None

    for d in range(n_days):
        e_d = vmt[:, d] / eff
        cum_e += e_d
        days_since += 1

        trigger = ((cum_e >= usable) | (days_since >= pref)) & (cum_e > 0.0)
        idx = np.where(trigger)[0]
        if idx.size == 0:
            continue

        feas = np.zeros((idx.size, 3), dtype=bool)
        feas[:, 0] = home_access[idx]
        feas[:, 1] = work_avail[idx, d]
        feas[:, 2] = public_avail[idx, d]
        location = choose_locations(feas, base_w, rng)

        demand = np.minimum(cum_e[idx], usable[idx])
        bins = _bin_of(demand, cfg)

        sub = np.empty(idx.size, dtype=object)
        level_out = np.empty(idx.size, dtype="U8")
        housing_out = np.empty(idx.size, dtype="U32")

        is_home = location == "home"
        is_work = location == "work"
        is_pub = location == "public"

        if is_home.any():
            hh = sample_home_housing(int(is_home.sum()), cfg, rng)
            sub[is_home] = hh
            housing_out[is_home] = np.asarray(hh, dtype="U32")
            level_out[is_home] = "L2"
        if is_work.any():
            sub[is_work] = ""
            housing_out[is_work] = ""
            level_out[is_work] = "L2"
        if is_pub.any():
            lv = sample_public_levels(int(is_pub.sum()), cfg, rng)
            sub[is_pub] = lv
            housing_out[is_pub] = ""
            level_out[is_pub] = np.asarray(lv, dtype="U8")

        start, end, power, esess = _sample_pool_sessions(pidx, location, sub, bins, rng)

        loc_code = np.zeros(idx.size, dtype=np.int8)
        loc_code[is_work] = 1
        loc_code[is_pub] = 2
        _accumulate_day_load(load, d, start, end, power, loc_code, load_by_type)

        if taz_demand is not None:
            _accumulate_taz_demand(
                taz_demand, vehicle_taz, taz_weights, idx, location, demand, rng
            )

        n_sessions += idx.size
        for ct in LOCATIONS:
            ctype_counts[ct] += int((location == ct).sum())
        sum_esess += float(np.nansum(esess))

        if store_sessions:
            out_vehicle.append(idx.copy())
            out_day.append(np.full(idx.size, d, dtype=int))
            out_ctype.append(location.copy())
            out_level.append(level_out.copy())
            out_housing.append(housing_out.copy())
            out_start.append(start)
            out_end.append(end)
            out_power.append(power)
            out_demand.append(demand)
            out_esess.append(esess)

        cum_e[idx] = 0.0
        days_since[idx] = 0

    summary = _build_summary(cfg, n_sessions, ctype_counts, sum_esess)
    taz_df = (
        _taz_demand_frame(taz_vals, taz_demand)
        if taz_demand is not None
        else None
    )

    if not store_sessions:
        return SimulateResult(
            sessions=None,
            load=load,
            load_by_type=load_by_type,
            summary=summary,
            taz_demand_kwh=taz_df,
        )

    if not out_vehicle:
        sessions = pd.DataFrame(
            columns=[
                "vehicle_id", "day", "charge_type", "charger_level", "housing",
                "start_hour", "end_hour", "power", "energy_demand", "energy_session",
            ]
        )
    else:
        sessions = pd.DataFrame(
            {
                "vehicle_id": np.concatenate(out_vehicle),
                "day": np.concatenate(out_day),
                "charge_type": np.concatenate(out_ctype),
                "charger_level": np.concatenate(out_level),
                "housing": np.concatenate(out_housing),
                "start_hour": np.concatenate(out_start),
                "end_hour": np.concatenate(out_end),
                "power": np.concatenate(out_power),
                "energy_demand": np.concatenate(out_demand),
                "energy_session": np.concatenate(out_esess),
            }
        )
        sessions = sessions.dropna(subset=["start_hour", "power"]).reset_index(drop=True)
        sessions["start_hour"] = sessions["start_hour"].astype(int)
        sessions["end_hour"] = sessions["end_hour"].astype(int)
        empty_housing = sessions["housing"] == ""
        sessions.loc[empty_housing, "housing"] = pd.NA

    return SimulateResult(
        sessions=sessions,
        load=load,
        load_by_type=load_by_type,
        summary=summary,
        taz_demand_kwh=taz_df,
    )


def _build_summary(
    cfg: MultiDayConfig,
    n_sessions: int,
    ctype_counts: dict[str, int],
    sum_esess: float,
) -> pd.DataFrame:
    rows = [("total_sessions", n_sessions)]
    rows.append(
        (
            "sessions_per_vehicle_day",
            round(n_sessions / (cfg.n_vehicles * cfg.n_days), 4),
        )
    )
    for ct in LOCATIONS:
        share = ctype_counts[ct] / n_sessions if n_sessions else 0.0
        rows.append((f"share_{ct}", round(share, 4)))
    mean_es = sum_esess / n_sessions if n_sessions else 0.0
    rows.append(("mean_session_energy_kwh", round(mean_es, 2)))
    return pd.DataFrame(rows, columns=["metric", "value"])


def sampling_summary(sessions: pd.DataFrame, cfg: MultiDayConfig) -> pd.DataFrame:
    """High-level diagnostics for a sampling run."""
    rows = []
    rows.append(("total_sessions", len(sessions)))
    rows.append(
        (
            "sessions_per_vehicle_day",
            round(len(sessions) / (cfg.n_vehicles * cfg.n_days), 4),
        )
    )
    for ct in ("home", "work", "public"):
        rows.append(
            (f"share_{ct}", round(float((sessions["charge_type"] == ct).mean()), 4))
        )
    rows.append(
        ("mean_session_energy_kwh", round(float(sessions["energy_session"].mean()), 2))
    )
    return pd.DataFrame(rows, columns=["metric", "value"])
