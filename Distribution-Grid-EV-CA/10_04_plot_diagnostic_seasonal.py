"""
10_04_plot_diagnostic_seasonal.py

Diagnostic PNGs under figures/ASTR_diagnostics/ for load, congestion proxies,
and P_cong / M_BESS when available.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import common as C


def plot_ev_load_by_season() -> None:
    fig, axes = plt.subplots(2, 2, figsize=(10, 7), sharey=True)
    axes = axes.ravel()
    for ax, week in zip(axes, C.SEASONAL_WEEKS):
        name = week["name"]
        path = C.MESO_DIR / "seasonal" / name / "meso_hourly_kW.npy"
        if not path.is_file():
            ax.set_title(f"{name}: missing")
            continue
        load = np.load(path).sum(axis=0) / 1e6  # GW
        ax.plot(load, color="#2C3E50", lw=1.5)
        ax.set_title(name.capitalize())
        ax.set_xlabel("Hour of week")
        ax.set_ylabel("EV load (GW)")
        ax.grid(True, alpha=0.3)
    fig.suptitle("Aggregate CA EV charging load by seasonal week")
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "ev_load_seasonal_weeks.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_top_hubs() -> None:
    path = C.MESO_DIR / "meso_nodes.csv"
    if not path.is_file():
        return
    hubs = pd.read_csv(path).sort_values("mean_week_kwh", ascending=False).head(20)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(hubs["hub_id"][::-1], hubs["mean_week_kwh"][::-1] / 1e6, color="#2980B9")
    ax.set_xlabel("Mean seasonal-week EV energy (GWh)")
    ax.set_title("Top 20 meso hubs by EV energy")
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "top20_meso_hubs.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_corridor_capacities() -> None:
    path = C.MESO_DIR / "meso_edges.csv"
    if not path.is_file():
        return
    ed = pd.read_csv(path).sort_values("installed_capacity_W", ascending=False).head(25)
    fig, ax = plt.subplots(figsize=(8, 5))
    labels = ed["source"] + "–" + ed["target"]
    ax.barh(labels[::-1], ed["installed_capacity_W"][::-1] / 1e6, color="#16A085")
    ax.set_xlabel("Installed transfer capacity (MW)")
    ax.set_title("Top meso corridors (capacity heuristic from line kV)")
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "top_meso_corridors.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_pcong_mbess() -> None:
    path = C.ASTR_RESULTS_DIR / "P_cong_M_BESS.csv"
    if not path.is_file():
        print("P_cong_M_BESS.csv missing — skip metric bars")
        return
    df = pd.read_csv(path)
    df = df[df["season"] != "pooled_sum"]
    if df.empty or "P_cong" not in df.columns:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    x = np.arange(len(df))
    w = 0.35
    ax.bar(x - w / 2, df["P_cong"] / 1e6, width=w, label=r"$P_{cong}$", color="#C0392B")
    if "M_BESS" in df.columns:
        ax.bar(x + w / 2, df["M_BESS"] / 1e6, width=w, label=r"$M_{BESS}$", color="#27AE60")
    ax.set_xticks(x)
    ax.set_xticklabels(df["season"])
    ax.set_ylabel("kg CO$_2$ (millions)")
    ax.set_title("Congestion-emissions penalty and BESS mitigation")
    ax.legend()
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "P_cong_M_BESS_by_season.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_scenario_status() -> None:
    frames = []
    for week in C.SEASONAL_WEEKS:
        p = C.ASTR_RESULTS_DIR / week["name"] / "scenario_summary.csv"
        if p.is_file():
            frames.append(pd.read_csv(p))
    if not frames:
        return
    df = pd.concat(frames, ignore_index=True)
    fig, ax = plt.subplots(figsize=(8, 4))
    status = df.groupby(["season", "scenario"])["status"].first().unstack()
    # encode ok=1 else 0
    enc = status.apply(lambda c: c.map(lambda v: 1 if v == "ok" else 0))
    im = ax.imshow(enc.to_numpy(), aspect="auto", cmap="Greens")
    ax.set_xticks(range(enc.shape[1]))
    ax.set_xticklabels(enc.columns)
    ax.set_yticks(range(enc.shape[0]))
    ax.set_yticklabels(enc.index)
    ax.set_title("Scenario solve status (green=ok)")
    fig.colorbar(im, ax=ax, fraction=0.046)
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "scenario_solve_status.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def main() -> None:
    C.ensure_dir(C.FIGURES_ASTR_DIR)
    plot_ev_load_by_season()
    plot_top_hubs()
    plot_corridor_capacities()
    plot_pcong_mbess()
    plot_scenario_status()
    print(f"Diagnostics in {C.FIGURES_ASTR_DIR}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
