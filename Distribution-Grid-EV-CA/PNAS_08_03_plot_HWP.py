"""
PNAS_08_03_plot_HWP.py — Python port of ``PNAS_08_03_plot_HWP.R``.

Computes per-feeder/year share of H/W/P load plus category that dominates the
feeder, writes CSVs, and renders a pie chart of the share of charging demand
for a chosen year.
"""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
import pandas as pd

from common import read_rds_like

EV_PKL = (
    "data/result/charging profile by feeder/"
    "profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.pkl"
)
HWP_SHARE_CSV = "data/result/HWP/HWP share_sample42_draw12_split6699_sample36.csv"
HWP_MAX_CSV = "data/result/HWP/HWP max_sample42_draw12_split6699_sample36.csv"
FIG_DIR = "figures/PNAS"


def main() -> None:
    os.makedirs(os.path.dirname(HWP_SHARE_CSV), exist_ok=True)
    os.makedirs(FIG_DIR, exist_ok=True)

    ev = read_rds_like(EV_PKL)
    feeder_hwp = (
        ev.groupby(["FeederID", "charge_type", "year"], as_index=False)["load"]
        .sum()
        .rename(columns={"load": "tot_load_type"})
    )
    feeder_hwp["tot_load"] = feeder_hwp.groupby(["FeederID", "year"])[
        "tot_load_type"
    ].transform("sum")
    feeder_hwp["HWP_share"] = feeder_hwp["tot_load_type"] / feeder_hwp["tot_load"]
    feeder_hwp.to_csv(HWP_SHARE_CSV, index=False)

    feeder_hwp_cat = (
        feeder_hwp.sort_values(["FeederID", "year", "HWP_share"], ascending=[True, True, False])
        .drop_duplicates(subset=["FeederID", "year"])
        [["FeederID", "year", "charge_type"]]
        .rename(columns={"charge_type": "max_type"})
        .reset_index(drop=True)
    )
    feeder_hwp_cat.to_csv(HWP_MAX_CSV, index=False)

    check_energy = (
        ev.groupby(["charge_type", "year"], as_index=False)["load"]
        .sum()
        .rename(columns={"load": "tot_load_type"})
    )
    check_energy["tot_load"] = check_energy.groupby("year")["tot_load_type"].transform("sum")
    check_energy["HWP_share"] = check_energy["tot_load_type"] / check_energy["tot_load"]

    # Pie chart for 2045
    sub = check_energy[check_energy["year"] == 2045]
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.pie(
        sub["HWP_share"],
        labels=sub["charge_type"],
        autopct="%1.1f%%",
        colors=["#1f77b4", "#ff7f0e", "#2ca02c"],
    )
    ax.set_title("Share of Charging Demand (2045)")
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "HWP_charging demand.png"), dpi=500)
    plt.close(fig)


if __name__ == "__main__":
    main()
