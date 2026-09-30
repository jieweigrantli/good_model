"""
PNAS_09_01_plot_upgrade_cost.py — Python port of
``PNAS_09_01_plot_upgrade cost.R``.

Joins a cost-per-kW table with each feeder's maximum overload to estimate
total upgrade cost by utility and by year, then plots IQR ribbons + median
lines, in both total and per-customer units.
"""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common import read_rds_like

OVERLOAD_PKL = "data/result/overload/overload_sample42_draw12_split6699_sample36.pkl"
HWP_MAX_CSV = "data/result/HWP/HWP max_sample42_draw12_split6699_sample36.csv"
FIG_DIR = "figures/PNAS"

POP_UTIL = {"PGE": 59567564, "SCE": 61987760, "SDGE": 9707765}


COST_TABLE = pd.DataFrame(
    {
        "need_floor": [0, 1000, 2000, 4000, 8000],
        "need_ceil":  [1000, 2000, 4000, 8000, np.inf],
        "min":        [37.04, 9.9, 30.3, 78.15, 73.91],
        "p25":        [445.71, 251.84, 196.76, 268.65, 237.23],
        "median":     [1875, 1368.89, 673.35, 438.14, 367.85],
        "p75":        [5791.67, 2092.89, 1447.55, 785.3, 586.32],
        "max":        [383900, 5239.73, 5633.51, 3217.95, 1267.76],
    }
)


def _bin_cost(overload_max: pd.Series) -> pd.DataFrame:
    """Return cost rows aligned with the bin the ``overload_max`` value falls into."""
    out_rows = []
    for v in overload_max:
        mask = (COST_TABLE["need_floor"] <= v) & (COST_TABLE["need_ceil"] > v)
        row = COST_TABLE[mask].iloc[0]
        out_rows.append(row)
    return pd.DataFrame(out_rows).reset_index(drop=True)


def main() -> None:
    os.makedirs(FIG_DIR, exist_ok=True)
    ev_grid = read_rds_like(OVERLOAD_PKL)
    feeder_type = pd.read_csv(HWP_MAX_CSV)

    feeder_overload = (
        ev_grid.groupby(["FeederID", "year"], as_index=False)
        .agg(overload01=("overload", lambda s: bool((s > 0).any())))
    )
    nat = set(ev_grid.loc[(ev_grid["year"] == 2022) & (ev_grid["headroom"] <= 0), "FeederID"].unique())
    feeder_overload["overload_cat"] = "No Overload"
    feeder_overload.loc[feeder_overload["overload01"] == True, "overload_cat"] = "EV Overload"
    feeder_overload.loc[feeder_overload["FeederID"].isin(nat), "overload_cat"] = "Baseload Overload"

    ev_grid = ev_grid.merge(feeder_overload, on=["FeederID", "year"])
    ev_grid = ev_grid.merge(feeder_type, on=["FeederID", "year"])

    max_overload = (
        ev_grid[ev_grid["overload_cat"] == "EV Overload"]
        .groupby(["FeederID", "year", "utility", "max_type"], as_index=False)["overload"]
        .max()
        .rename(columns={"overload": "overload_max"})
    )

    costs = _bin_cost(max_overload["overload_max"].to_numpy())
    joined = pd.concat([max_overload.reset_index(drop=True), costs], axis=1)

    # each bin's ceiling is used as the scaling factor, matching the R logic
    for c in ("min", "p25", "median", "p75", "max"):
        joined[f"cost_{c}"] = joined[c] * joined["need_ceil"]

    cost_utility = (
        joined.groupby(["year", "utility"], as_index=False)[
            ["cost_min", "cost_p25", "cost_median", "cost_p75", "cost_max"]
        ].sum()
        .rename(
            columns={
                "cost_min": "cost_min_tot",
                "cost_p25": "cost_p25_tot",
                "cost_median": "cost_median_tot",
                "cost_p75": "cost_p75_tot",
                "cost_max": "cost_max_tot",
            }
        )
    )

    # --- Total cost plot ----------------------------------------------------
    fig, ax = plt.subplots(figsize=(4.3, 4.3))
    for util, colour in zip(("PGE", "SCE", "SDGE"), ("#1f77b4", "#2ca02c", "#d62728")):
        sub = cost_utility[cost_utility["utility"] == util].sort_values("year")
        if sub.empty:
            continue
        ax.fill_between(
            sub["year"],
            sub["cost_p25_tot"] / 1e9,
            sub["cost_p75_tot"] / 1e9,
            alpha=0.2,
            color=colour,
        )
        ax.plot(sub["year"], sub["cost_median_tot"] / 1e9, color=colour, lw=1.5, label=util)
    ax.set_xlabel("Year")
    ax.set_ylabel("Total Upgrade Cost ($B)")
    ax.legend(title="Utility", fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "cost_utility.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)

    # --- Per-customer cost plot --------------------------------------------
    cost_utility["population"] = cost_utility["utility"].map(POP_UTIL)
    for c in ("median", "p25", "p75"):
        cost_utility[f"{c}_pcp"] = cost_utility[f"cost_{c}_tot"] / cost_utility["population"]

    fig, ax = plt.subplots(figsize=(4.3, 4.3))
    for util, colour in zip(("PGE", "SCE", "SDGE"), ("#1f77b4", "#2ca02c", "#d62728")):
        sub = cost_utility[cost_utility["utility"] == util].sort_values("year")
        if sub.empty:
            continue
        ax.fill_between(sub["year"], sub["p25_pcp"], sub["p75_pcp"], alpha=0.2, color=colour)
        ax.plot(sub["year"], sub["median_pcp"], color=colour, lw=1.5, label=util)
    ax.set_xlabel("Year")
    ax.set_ylabel("Upgrade Cost Per Customer ($)")
    ax.legend(title="Utility", fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "cost_utility_per customer.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
