"""
PNAS_08_06_plot_overload_frequency.py — Python port of
``PNAS_08_06_plot_overload_frequency.R``.

PDF of per-feeder overload frequency (overload hours / all hours) in 2045,
grouped by dominant charging type.
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

    freq = (
        ev_grid.assign(overload_flag=(ev_grid["overload"] > 0).astype(int))
        .groupby(["FeederID", "year"], as_index=False)
        .agg(overload_hour=("overload_flag", "sum"), all_hour=("overload_flag", "size"))
    )
    freq["freq"] = freq["overload_hour"] / freq["all_hour"]
    freq = freq.merge(feeder_type, on=["FeederID", "year"])

    yr = 2045
    sub = freq[(freq["year"] == yr) & (freq["freq"] > 0)]
    fig, ax = plt.subplots(figsize=(6, 4))
    bins = np.arange(0, 1 + 0.025, 0.025)
    for cat, colour in zip(("home", "work", "public"), ("#d62728", "#2ca02c", "#1f77b4")):
        vals = sub.loc[sub["max_type"] == cat, "freq"].dropna()
        if vals.empty:
            continue
        counts, edges = np.histogram(vals, bins=bins, density=True)
        centres = 0.5 * (edges[:-1] + edges[1:])
        ax.plot(centres, counts, label=cat, color=colour, lw=1.5)
    ax.set_xlabel("Overload Hours / All Hours")
    ax.set_ylabel("Density of Count of Feeders")
    ax.set_title(f"Overload Frequency ({yr})")
    ax.legend(title="Dominant charge type", fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, f"freq_pdf_HWP_{yr}.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
