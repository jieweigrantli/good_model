"""
PNAS_08_04_plot_overload_spatial.py — Python port of
``PNAS_08_04_plot_overload_spatial.R``.

Plots the fraction of overloaded feeders (over time) by utility, as a stacked
area chart.  Requires the overload table produced by ``07_05_grid_EV.py``.
"""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
import pandas as pd

from common import read_rds_like

OVERLOAD_PKL = "data/result/overload/overload_sample42_draw12_split6699_sample36.pkl"
HWP_MAX_CSV = "data/result/HWP/HWP max_sample42_draw12_split6699_sample36.csv"
FIG_DIR = "figures/PNAS"


def main() -> None:
    os.makedirs(FIG_DIR, exist_ok=True)
    ev_grid = read_rds_like(OVERLOAD_PKL)
    feeder_type = pd.read_csv(HWP_MAX_CSV)

    ev_grid["season"] = "Spring"
    ev_grid.loc[ev_grid["month"].isin([6, 7, 8]), "season"] = "Summer"
    ev_grid.loc[ev_grid["month"].isin([9, 10, 11]), "season"] = "Fall"
    ev_grid.loc[ev_grid["month"].isin([12, 1, 2]), "season"] = "Winter"

    feeder_overload = (
        ev_grid.groupby(["FeederID", "year"], as_index=False)
        .agg(overload01=("overload", lambda s: bool((s > 0).any())))
    )
    nat = set(ev_grid.loc[(ev_grid["year"] == 2022) & (ev_grid["headroom"] <= 0), "FeederID"].unique())

    feeder_overload["overload_cat"] = "No Overload"
    feeder_overload.loc[feeder_overload["overload01"] == True, "overload_cat"] = "EV Overload"
    feeder_overload.loc[feeder_overload["FeederID"].isin(nat), "overload_cat"] = "Baseload Overload"

    feeder_utility = ev_grid[["FeederID", "utility"]].drop_duplicates()
    feeder_overload = feeder_overload.merge(feeder_utility, on="FeederID")

    counts = (
        feeder_overload.groupby(["year", "overload_cat", "utility"], as_index=False).size()
        .rename(columns={"size": "count"})
    )
    counts["total"] = counts.groupby("year")["count"].transform("sum")
    counts["share"] = counts["count"] / counts["total"]

    ev_only = counts[counts["overload_cat"] == "EV Overload"]
    wide = ev_only.pivot_table(index="year", columns="utility", values="share", fill_value=0).sort_index()

    fig, ax = plt.subplots(figsize=(4.3, 4.3))
    ax.stackplot(wide.index, wide.T.values, labels=wide.columns, alpha=0.9)
    ax.set_xlabel("Year")
    ax.set_ylabel("Fraction of Overloaded Feeders")
    ax.set_ylim(0, 1)
    ax.legend(title="Utility", loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "overload_share_year_EV.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
