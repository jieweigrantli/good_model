"""
choice.py -- probability matrices and vectorised samplers for:

1. charging *location* (home / work / public) among the feasible set on a day;
2. public charger *level* (DC / L2);
3. home *housing* type (single / multi family).

The charging-*frequency* matrix (charge every k days) is realised in
``fleet.py`` as each vehicle's ``interval_pref_days``; this module documents it
and exposes a normalised view for reporting.
"""

from __future__ import annotations

import numpy as np

from config import MultiDayConfig

LOCATIONS = ("home", "work", "public")


def location_weight_vector(cfg: MultiDayConfig) -> np.ndarray:
    return np.array([cfg.location_weights[loc] for loc in LOCATIONS], dtype=float)


def _greedy_energy_budget(
    demand: np.ndarray,
    budgets: np.ndarray,
    feasible: np.ndarray | None,
    fallback_priorities: np.ndarray,
    labels: tuple[str, ...],
    rng: np.random.Generator,
) -> np.ndarray:
    """Assign each row to a label by depleting per-category kWh budgets greedily."""
    m = demand.shape[0]
    k = len(labels)
    remaining = budgets.astype(float).copy()
    out = np.empty(m, dtype=object)
    order = rng.permutation(m)

    for i in order:
        d = float(demand[i])
        if feasible is None:
            feas_idx = np.arange(k, dtype=int)
        else:
            feas_idx = np.flatnonzero(feasible[i])

        if feas_idx.size == 0:
            choice = int(np.argmax(fallback_priorities))
        elif feas_idx.size == 1:
            choice = int(feas_idx[0])
        else:
            choice = int(feas_idx[np.argmax(remaining[feas_idx])])

        out[i] = labels[choice]
        remaining[choice] -= d

    return out


def assign_locations_energy_budget(
    feasible: np.ndarray,
    demand: np.ndarray,
    target_shares: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Assign locations so each day's recharge kWh tracks fleet energy shares.

    ``target_shares`` are absolute fleet fractions (home / work / public) that
    sum to 1.  Among feasible locations, each vehicle is assigned to the type
    with the largest remaining daily kWh budget.
    """
    shares = target_shares / target_shares.sum()
    total_demand = float(demand.sum())
    budgets = shares * total_demand
    return _greedy_energy_budget(
        demand,
        budgets,
        feasible,
        shares,
        LOCATIONS,
        rng,
    )


def assign_public_levels_energy_budget(
    demand_pub: np.ndarray,
    dc_share: float,
    l2_share: float,
    total_day_demand: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Assign DC vs L2 for public sessions using absolute fleet energy shares.

    ``dc_share`` and ``l2_share`` are fractions of total fleet recharge kWh
    (e.g. 0.20 and 0.08 from Reference RAW_SHARES), not shares within public.
    """
    levels = ("DC", "L2")
    n = demand_pub.shape[0]
    if n == 0:
        return np.array([], dtype="U8")

    shares = np.array([dc_share, l2_share], dtype=float)
    budgets = shares * total_day_demand
    priorities = shares / shares.sum()
    return _greedy_energy_budget(
        demand_pub,
        budgets,
        None,
        priorities,
        levels,
        rng,
    ).astype("U8")


def choose_locations(
    feasible: np.ndarray,            # [m, 3] bool, columns = LOCATIONS
    base_weights: np.ndarray,        # [3]
    rng: np.random.Generator,
) -> np.ndarray:
    """Return an array of length m with a chosen location per row.

    Infeasible locations get zero weight; remaining weights are renormalised.
    Rows with no feasible location fall back to 'public' (the always-available
    backstop in reality).
    """
    m = feasible.shape[0]
    w = feasible.astype(float) * base_weights[None, :]
    row_sum = w.sum(axis=1)

    # fallback: if nothing feasible, force public (column 2)
    dead = row_sum <= 0
    if dead.any():
        w[dead, 2] = 1.0
        row_sum = w.sum(axis=1)

    probs = w / row_sum[:, None]
    cdf = np.cumsum(probs, axis=1)
    u = rng.random(m)
    choice_idx = (u[:, None] < cdf).argmax(axis=1)
    return np.array(LOCATIONS, dtype=object)[choice_idx]


def sample_public_levels(
    n: int, cfg: MultiDayConfig, rng: np.random.Generator
) -> np.ndarray:
    w = cfg.public_level_weights
    vals = np.array(list(w.keys()))
    p = np.array(list(w.values()), dtype=float)
    p = p / p.sum()
    return rng.choice(vals, size=n, p=p)


def sample_home_housing(
    n: int, cfg: MultiDayConfig, rng: np.random.Generator
) -> np.ndarray:
    w = cfg.home_housing_weights
    vals = np.array(list(w.keys()))
    p = np.array(list(w.values()), dtype=float)
    p = p / p.sum()
    return rng.choice(vals, size=n, p=p)
