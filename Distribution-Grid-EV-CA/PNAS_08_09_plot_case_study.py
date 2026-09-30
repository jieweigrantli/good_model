"""
PNAS_08_09_plot_case_study.py — Python port of
``PNAS_08_09_plot_case study.R``.

Plots two feeder-level case studies (one home-dominated, one public-dominated)
showing H/W/P load stacked areas with baseload & headroom lines.
"""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common import read_rds_like

OVERLOAD_PKL = "data/result/overload/overload_sample42_draw12_split6699_sample36.pkl"
HWP_MAX_CSV = "data/result/HWP/HWP max_sample42_draw12_split6699_sample36.csv"
HWP_SHARE_CSV = "data/result/HWP/HWP share_sample42_draw12_split6699_sample36.csv"
EV_PKL = (
    "data/result/charging profile by feeder/"
    "profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.pkl"
)
FIG_DIR = "figures/PNAS"


def _plot_case(ev: pd.DataFrame, ev_grid: pd.DataFrame, feeder: str, year: int, month: int, title: str) -> None:
    ev_sub = ev[(ev["FeederID"] == feeder) & (ev["year"] == year)]
    grid_sub = ev_grid[
        (ev_grid["FeederID"] == feeder) & (ev_grid["year"] == year) & (ev_grid["month"] == month)
    ].copy()
    grid_sub["totload"] = grid_sub["EVload"] + grid_sub["baseload"]

    hours = np.arange(24)
    # stacked bottom to top: home, work, public
    stacks = []
    for ct in ("home", "work", "public"):
        s = ev_sub[ev_sub["charge_type"] == ct].set_index("hour")["load"].reindex(hours, fill_value=0)
        stacks.append(s.values / 1000.0)  # kW → MW
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.stackplot(
        hours,
        *stacks,
        labels=("home", "work", "public"),
        colors=("#d62728", "#2ca02c", "#1f77b4"),
        alpha=0.85,
    )

    if not grid_sub.empty:
        baseload = grid_sub.set_index("hour")["baseload"].reindex(hours, fill_value=0).values / 1000.0
        headroom = grid_sub.set_index("hour")["headroom"].reindex(hours, fill_value=0).values / 1000.0
        ax.plot(hours, baseload, color="k", lw=1.5, label="baseload")
        ax.plot(hours, headroom, color="grey", lw=1.5, linestyle="--", label="headroom")

    ax.set_title(title)
    ax.set_xlabel("Hour")
    ax.set_ylabel("MW")
    ax.set_xticks(np.arange(0, 25, 6))
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    os.makedirs(FIG_DIR, exist_ok=True)
    fig.savefig(
        os.path.join(FIG_DIR, f"case_{title}_{feeder}_{year}_{month}.png"),
        dpi=500,
        bbox_inches="tight",
    )
    plt.close(fig)


def main() -> None:
    ev_grid = read_rds_like(OVERLOAD_PKL)
    ev = read_rds_like(EV_PKL)

    # The R script picks example feeders based on candidates; we reproduce the
    # hard-coded examples directly for simplicity.
    _plot_case(ev, ev_grid, feeder="152282101", year=2045, month=8, title="Home Charging Dominated Feeder")
    _plot_case(ev, ev_grid, feeder="515", year=2045, month=8, title="Public Charging Dominated Feeder")


if __name__ == "__main__":
    main()
