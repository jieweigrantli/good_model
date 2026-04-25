"""
PNAS_08_05_plot_overload_intensity.py — Python port of
``PNAS_08_05_plot_overload_intensity.R``.

Plots PDFs of overload intensity (total load / capacity) and maximum overload
size for EV-overloaded feeders, stratified by dominant charge type, and a
stacked-area plot of upgrade need (GW) by utility.
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
    totload_cap = (
        ev_grid.groupby(["FeederID", "year", "overload_cat"], as_index=False)["tot_cap_share"].max()
    )
    overload_max = (
        ev_grid.groupby(["FeederID", "year", "overload_cat"], as_index=False)["overload"].max()
        .rename(columns={"overload": "max_overload"})
    )

    ev_grid = ev_grid.merge(feeder_type, on=["FeederID", "year"])
    totload_cap = totload_cap.merge(feeder_type, on=["FeederID", "year"])
    overload_max = overload_max.merge(feeder_type, on=["FeederID", "year"])

    # --- Overload intensity PDF for 2045 -----------------------------------
    yr = 2045
    sub = totload_cap[
        (totload_cap["year"] == yr)
        & (totload_cap["overload_cat"] == "EV Overload")
    ]
    fig, ax = plt.subplots(figsize=(6, 4))
    bins = np.arange(1, 5 + 0.1, 0.1)
    for cat, colour in zip(("home", "work", "public"), ("#d62728", "#2ca02c", "#1f77b4")):
        vals = sub.loc[sub["max_type"] == cat, "tot_cap_share"].dropna()
        if vals.empty:
            continue
        counts, edges = np.histogram(vals, bins=bins, density=True)
        centres = 0.5 * (edges[:-1] + edges[1:])
        ax.plot(centres, counts, label=cat, color=colour, lw=1.5)
    ax.set_xlim(1, 5)
    ax.set_xlabel("Total Load / Capacity")
    ax.set_ylabel("Density of Count of Feeders")
    ax.set_title("Overload Intensity (2045)")
    ax.legend(title="Dominant charge type", fontsize=8, loc="upper right")
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, f"tot_cap_pdf_HWP_{yr}.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)

    # --- Overload size PDF and count ---------------------------------------
    sub_ov = overload_max[
        (overload_max["year"] == yr) & (overload_max["overload_cat"] == "EV Overload")
    ]
    fig, ax = plt.subplots(figsize=(6, 4))
    bins = np.arange(0, 30000 + 100, 100)
    for cat, colour in zip(("home", "work", "public"), ("#d62728", "#2ca02c", "#1f77b4")):
        vals = sub_ov.loc[sub_ov["max_type"] == cat, "max_overload"].dropna()
        if vals.empty:
            continue
        counts, edges = np.histogram(vals, bins=bins)
        centres = 0.5 * (edges[:-1] + edges[1:])
        ax.plot(centres, counts, label=cat, color=colour, lw=1.5)
    ax.set_xlim(1, 30000)
    ax.set_xlabel("Overload Size (kW)")
    ax.set_ylabel("Count of Feeders")
    ax.set_title("Overload Intensity (2045)")
    ax.legend(title="Dominant charge type", fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, f"overloadsize_count_HWP_{yr}.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)

    # --- Upgrade need by utility (stacked area) ----------------------------
    max_overload_util = (
        ev_grid.loc[ev_grid["overload_cat"] == "EV Overload"]
        .groupby(["FeederID", "year", "utility"], as_index=False)["overload"]
        .max()
        .rename(columns={"overload": "overload_max"})
    )
    upgrade = (
        max_overload_util.groupby(["year", "utility"], as_index=False)["overload_max"].sum()
    )
    upgrade["upgrade_GW"] = upgrade["overload_max"] / 1e6
    wide = (
        upgrade.pivot_table(index="year", columns="utility", values="upgrade_GW", fill_value=0)
        .sort_index()
    )
    fig, ax = plt.subplots(figsize=(4.3, 4.3))
    ax.stackplot(wide.index, wide.T.values, labels=wide.columns, alpha=0.9)
    ax.set_xlabel("Year")
    ax.set_ylabel("Required Capacity Upgrade (GW)")
    ax.legend(title="Utility", loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "upgrade_total_year_area.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
