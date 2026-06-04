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
