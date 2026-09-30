"""
config.py -- single configuration object for the multi-day charging subproject.

Everything that controls the simulation (fleet mix, battery sizes, charging
frequency probabilities, location probabilities, empirical-pool assumptions,
and the daily-driving model) lives in ``MultiDayConfig`` so it can be edited in
one notebook cell and passed to every module.

Units
-----
- distance: miles
- energy:   kWh
- power:    kW
- time:     hour-of-day (0..23)

Consistency with the parent pipeline
-------------------------------------
The legacy pipeline uses ``demand_kWh = distance_miles / 3`` (i.e. ~3 mi/kWh)
and the energy bin edges [0,5,10,15,20,30,50,80, inf].  Both are reproduced
here so sampled sessions stay comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# Energy demand bin edges (kWh), identical to 05_02 in the parent pipeline.
DEFAULT_BIN_EDGES: tuple[float, ...] = (0, 5, 10, 15, 20, 30, 50, 80, float("inf"))

LOCATION_SELECTION_MODES = ("energy_budget", "session_prob")


@dataclass
class MultiDayConfig:
    # ---- reproducibility / size -------------------------------------------
    seed: int = 42
    n_vehicles: int = 2000
    n_days: int = 365
    # If False, ``simulate`` only builds hourly load matrices (no session table).
    # Required for full-state fleet sizes (e.g. millions of vehicles × 365 days).
    store_sessions: bool = True

    # ---- vehicle energy model ---------------------------------------------
    # Battery capacity mix: {capacity_kWh: share}. Shares are normalised.
    battery_mix: dict[float, float] = field(
        default_factory=lambda: {
            40.0: 0.15,   # small BEV / older PHEV-leaning
            60.0: 0.35,   # mainstream BEV
            75.0: 0.25,   # long-range sedan
            100.0: 0.20,  # large SUV / truck
            130.0: 0.05,  # very large pack
        }
    )
    # Driving efficiency (miles per kWh). 3.0 matches the legacy dist/3 rule.
    efficiency_mi_per_kwh: float = 3.0
    # Usable depth of discharge between charges: vehicle will not deplete below
    # ``reserve_soc`` of its pack, i.e. usable_energy = battery * (1 - reserve).
    reserve_soc: float = 0.10

    # ---- charging-frequency preference ------------------------------------
    # Probability a vehicle *prefers* to charge every k days (k = 1..7).
    # A vehicle still charges earlier if its battery would be depleted.
    charge_interval_probs: dict[int, float] = field(
        default_factory=lambda: {
            1: 0.45,  # charges (almost) every day
            2: 0.25,
            3: 0.15,
            4: 0.07,
            5: 0.04,
            6: 0.02,
            7: 0.02,
        }
    )

    # ---- charging-location model ------------------------------------------
    # Fleet-wide recharge energy fractions (kWh), matching Reference_Code
    # RAW_SHARES.  With ``location_selection='energy_budget'`` (default), each
    # day's triggered recharge is allocated greedily to hit these shares.
    # With ``location_selection='session_prob'``, the same values are used as
    # per-session choice weights among feasible locations (legacy behaviour).
    location_weights: dict[str, float] = field(
        default_factory=lambda: {"home": 0.68, "work": 0.04, "public": 0.28}
    )
    # How to interpret ``location_weights`` when assigning charge locations.
    location_selection: Literal["energy_budget", "session_prob"] = "energy_budget"
    # Fraction of the fleet with a home charger.
    home_access_share: float = 0.80
    # Fraction of the fleet that *can* charge at work (has access at some site).
    work_access_share: float = 0.30
    # Probability a work-enabled vehicle actually visits work on a given day.
    work_trip_prob: float = 0.62
    # Probability any vehicle has a public-charge-eligible trip on a given day.
    public_trip_prob: float = 0.85

    # ---- public charger level split (when location == public) -------------
    # Absolute fleet recharge energy fractions for DC and public L2.  Defaults
    # match RAW_SHARES (DCFC 0.20, Public_L2 0.08); they sum to
    # ``location_weights['public']`` (0.28).  Under ``energy_budget`` mode each
    # day's public-assigned kWh is split greedily toward these targets.
    public_level_weights: dict[str, float] = field(
        default_factory=lambda: {"DC": 0.20, "L2": 0.08}
    )
    # ---- home housing split (when location == home) -----------------------
    home_housing_weights: dict[str, float] = field(
        default_factory=lambda: {"single family": 56.4, "multi family": 38.9}
    )

    # ---- daily driving model ----------------------------------------------
    # Mean and coefficient-of-variation of per-vehicle *typical* daily VMT.
    # Each vehicle draws a personal mean; each day applies multiplicative noise.
    mean_daily_vmt: float = 32.0
    vmt_between_vehicle_cv: float = 0.45   # spread of personal means
    vmt_day_to_day_cv: float = 0.55        # day-to-day noise around personal mean
    # Probability a vehicle does not drive at all on a given day.
    no_travel_prob: float = 0.08

    # ---- bins --------------------------------------------------------------
    bin_edges: tuple[float, ...] = DEFAULT_BIN_EDGES

    # ---- optional real-data inputs (used if present) ----------------------
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    sd_trips_pkl: str = "data/mobility_data/CSTDM_processed/EV trips_new_SDPTM_sample42.pkl"
    # Lightweight per-vehicle calibration table (~2M rows); built from SDPTM CSV if missing.
    sd_trips_profiles_pkl: str = (
        "data/mobility_data/CSTDM_processed/EV trips_new_SDPTM_sample42_profiles.pkl"
    )
    session_pool_pkl: str = "data/charging data/charging_session_all_clean.pkl"

    # ---- output ------------------------------------------------------------
    out_dir: str = "multiday_charging/outputs"

    def __post_init__(self) -> None:
        if self.location_selection not in LOCATION_SELECTION_MODES:
            raise ValueError(
                f"location_selection must be one of {LOCATION_SELECTION_MODES}, "
                f"got {self.location_selection!r}"
            )
        loc_sum = sum(self.location_weights.values())
        if abs(loc_sum - 1.0) > 0.01:
            raise ValueError(
                f"location_weights must sum to ~1.0 (fleet energy shares), got {loc_sum:.4f}"
            )
        pub_sum = sum(self.public_level_weights.values())
        public_share = self.location_weights.get("public", 0.0)
        if abs(pub_sum - public_share) > 0.01:
            raise ValueError(
                "public_level_weights must sum to location_weights['public'] "
                f"({public_share:.4f}); got {pub_sum:.4f}"
            )

    # -----------------------------------------------------------------------
    def normalised_battery_mix(self) -> dict[float, float]:
        total = sum(self.battery_mix.values())
        return {k: v / total for k, v in self.battery_mix.items()}

    def normalised_interval_probs(self) -> dict[int, float]:
        total = sum(self.charge_interval_probs.values())
        return {k: v / total for k, v in self.charge_interval_probs.items()}

    def sd_trips_path(self) -> Path:
        return self.base_dir / self.sd_trips_pkl

    def sd_trips_profiles_path(self) -> Path:
        return self.base_dir / self.sd_trips_profiles_pkl

    def session_pool_path(self) -> Path:
        return self.base_dir / self.session_pool_pkl

    def out_path(self) -> Path:
        p = self.base_dir / self.out_dir
        p.mkdir(parents=True, exist_ok=True)
        return p
