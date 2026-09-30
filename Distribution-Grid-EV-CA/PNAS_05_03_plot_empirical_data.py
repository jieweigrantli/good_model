"""
PNAS_05_03_plot_empirical_data.py — Python port of
``PNAS_05_03_plot_empirical data.R``.

Plots a density-normalised frequency polygon of empirical charge-event start
hours, grouped by charge type category.
"""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common import read_rds_like

IN_PATH = "data/charging data/charging_session_all_clean.pkl"
OUT_DIR = "figures/PNAS"


def _categorise(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["charge_type_category"] = "none"
    df.loc[df["housing"] == "multi family", "charge_type_category"] = "home-multi family"
    df.loc[df["housing"] == "single family", "charge_type_category"] = "home-single family"
    df.loc[df["charge_type"] == "work", "charge_type_category"] = "work"
    df.loc[
        (df["charge_type"] == "public") & (df["charger_level"] == "DC"),
        "charge_type_category",
    ] = "public-DC"
    df.loc[
        (df["charge_type"] == "public") & (df["charger_level"] == "L2"),
        "charge_type_category",
    ] = "public-L2"
    return df


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    charge_all = read_rds_like(IN_PATH)
    charge_all = _categorise(charge_all)

    categories = [
        "home-single family",
        "home-multi family",
        "work",
        "public-DC",
        "public-L2",
    ]

    fig, ax = plt.subplots(figsize=(6, 4))
    bins = np.arange(0, 25, 1)
    for cat in categories:
        sub = charge_all.loc[charge_all["charge_type_category"] == cat, "start_hour"].dropna()
        if sub.empty:
            continue
        counts, edges = np.histogram(sub, bins=bins, density=True)
        centres = 0.5 * (edges[:-1] + edges[1:])
        ax.plot(centres, counts, label=cat, lw=1.5)

    ax.set_xticks(np.arange(0, 25, 6))
    ax.set_xlabel("Charge Event Start Hour")
    ax.set_ylabel("Density")
    ax.legend(title="EV Charging Type", fontsize=8, loc="upper right")
    ax.grid(False)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "event_StartHour.png"), dpi=500, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
