"""
PNAS_08_10_plot_headroom_vs_overload.py — Python port of
``PNAS_08_10_plot_headroom vs overload.R``.

Plots the share of overloaded feeders as a function of capacity headroom for
several target years (2025, 2030, 2035, 2040, 2045).
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

YEAR_LABELS = {
    2025: "2025 ( 8% EV)",
    2030: "2030 (25% EV)",
    2035: "2035 (54% EV)",
    2040: "2040 (81% EV)",
    2045: "2045 (95% EV)",
}
YEAR_COLOURS = {
    "2025 ( 8% EV)": "#26294A",
    "2030 (25% EV)": "#017351",
    "2035 (54% EV)": "#EF6A32",
    "2040 (81% EV)": "#ED0345",
    "2045 (95% EV)": "#710162",
}


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

    ev_grid["tot_cap_share"] = (ev_grid["EVload"] + ev_grid["baseload"]) / ev_grid["capacity"]
    ev_grid = ev_grid.merge(feeder_overload, on=["FeederID", "year"])
    ev_grid["headroom_share"] = ev_grid["headroom"] / ev_grid["capacity"]

    feeder_vs = (
        ev_grid.groupby(["FeederID", "year", "overload_cat"], as_index=False)
        .agg(
            min_headroom_share=("headroom_share", "min"),
            max_overload_intensity=("tot_cap_share", "max"),
        )
    )
    feeder_vs = feeder_vs.merge(feeder_type, on=["FeederID", "year"])

    bins = np.arange(0, 1 + 0.05, 0.05)
    centres = 0.5 * (bins[:-1] + bins[1:])

    fig, ax = plt.subplots(figsize=(8.6, 4.3))
    for yr, label in YEAR_LABELS.items():
        sub = feeder_vs[
            (feeder_vs["year"] == yr)
            & (feeder_vs["overload_cat"].isin(("EV Overload", "No Overload")))
            & (feeder_vs["min_headroom_share"] > 0)
            & (feeder_vs["min_headroom_share"] < 1)
        ]
        if sub.empty:
            continue
        total, _ = np.histogram(sub["min_headroom_share"], bins=bins)
        ev_only = sub[sub["overload_cat"] == "EV Overload"]
        ov, _ = np.histogram(ev_only["min_headroom_share"], bins=bins)
        with np.errstate(divide="ignore", invalid="ignore"):
            share = np.where(total > 0, ov / total, np.nan)
        ax.plot(centres, share, label=label, color=YEAR_COLOURS[label], lw=1.4)

    ax.set_xlabel("Capacity Headroom")
    ax.set_ylabel("Share of Overload Feeders")
    ax.set_xticks(np.arange(0, 1.01, 0.2))
    ax.legend(title="Year", fontsize=8, loc="upper right")
    fig.tight_layout()
    fig.savefig(
        os.path.join(FIG_DIR, "headroom vs overload_share_years_0.05.png"),
        dpi=500,
        bbox_inches="tight",
    )
    plt.close(fig)


if __name__ == "__main__":
    main()
