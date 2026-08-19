"""
10_04_plot_diagnostic_seasonal.py

Figures under figures/ASTR_diagnostics/:
  - substation load heatmaps / seasonal EV curves
  - top binding transmission corridors
  - P_cong vs M_BESS bars
  - diurnal dispatch curves when generation CSVs exist
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
        load = np.load(path).sum(axis=0) / 1e6
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


def plot_substation_heatmap() -> None:
    path = C.MESO_DIR / "seasonal" / "june" / "meso_hourly_kW.npy"
    ids_path = C.MESO_DIR / "seasonal" / "june" / "meso_hub_ids.npy"
    if not path.is_file():
        return
    load = np.load(path)
    ids = np.load(ids_path, allow_pickle=True).astype(str)
    peak = load.max(axis=1)
    top = np.argsort(peak)[-25:][::-1]
    fig, ax = plt.subplots(figsize=(11, 6))
    im = ax.imshow(load[top] / 1e3, aspect="auto", cmap="YlOrRd")
    ax.set_yticks(range(len(top)))
    ax.set_yticklabels([ids[i].replace("SUB_", "")[:16] for i in top], fontsize=7)
    ax.set_xlabel("Hour of week")
    ax.set_title("June EV load heatmap — top 25 substations (MW)")
    fig.colorbar(im, ax=ax, fraction=0.03, label="MW")
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "substation_load_heatmap_june.png"
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
    ax.set_title("Top 20 substations by EV energy")
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
    labels = ed["source"].astype(str) + "–" + ed["target"].astype(str)
    ax.barh(labels[::-1], ed["installed_capacity_W"][::-1] / 1e6, color="#16A085")
    ax.set_xlabel("Installed transfer capacity (MW)")
    ax.set_title("Top substation corridors (rated MVA or kV heuristic)")
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "top_meso_corridors.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_binding_lines() -> None:
    candidates = [
        C.ASTR_RESULTS_DIR / "four_week" / "S1" / "line_flows_summary.csv",
        C.ASTR_RESULTS_DIR / "8760" / "S1" / "line_flows_summary.csv",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        for week in C.SEASONAL_WEEKS:
            p = C.ASTR_RESULTS_DIR / week["name"] / "S1" / "line_flows_summary.csv"
            if p.is_file():
                path = p
                break
    if path is None:
        print("No line_flows_summary.csv — skip binding-line plot")
        return
    df = pd.read_csv(path)
    df = df.sort_values("binding_hours", ascending=False).head(20)
    if df.empty:
        return
    labels = df["source"].astype(str) + "→" + df["target"].astype(str)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(labels[::-1], df["binding_hours"][::-1], color="#C0392B")
    ax.set_xlabel("Binding hours")
    ax.set_title("Top binding transmission corridors (S1)")
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "top_binding_lines_S1.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_pcong_mbess() -> None:
    path = C.ASTR_RESULTS_DIR / "P_cong_M_BESS.csv"
    if not path.is_file():
        print("P_cong_M_BESS.csv missing — skip metric bars")
        return
    df = pd.read_csv(path)
    keycol = "key" if "key" in df.columns else "season"
    df = df[df[keycol] != "pooled_sum"]
    if df.empty or "P_cong" not in df.columns:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    x = np.arange(len(df))
    w = 0.35
    ax.bar(x - w / 2, df["P_cong"] / 1e6, width=w, label=r"$P_{cong}$", color="#C0392B")
    if "M_BESS" in df.columns:
        ax.bar(x + w / 2, df["M_BESS"] / 1e6, width=w, label=r"$M_{BESS}$", color="#27AE60")
    ax.set_xticks(x)
    ax.set_xticklabels(df[keycol].astype(str), rotation=20, ha="right")
    ax.set_ylabel("kg CO$_2$ (millions)")
    ax.set_title("Congestion-emissions penalty and BESS mitigation")
    ax.legend()
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "P_cong_M_BESS_by_season.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_diurnal_dispatch() -> None:
    """Mean 24-h generation mix from generation_by_asset.csv if hourly not stored."""
    tag_dirs = [C.ASTR_RESULTS_DIR / "four_week"]
    tag_dirs += [C.ASTR_RESULTS_DIR / w["name"] for w in C.SEASONAL_WEEKS]
    tag_dir = next((p for p in tag_dirs if (p / "S1" / "generation_by_asset.csv").is_file()), None)
    if tag_dir is None:
        return
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.6), sharey=True)
    fuels_keep = ["natural gas", "coal", "solar", "wind", "hydro", "nuclear", "battery"]
    colors = {
        "natural gas": "#e67e22",
        "coal": "#7f8c8d",
        "solar": "#f1c40f",
        "wind": "#3498db",
        "hydro": "#1abc9c",
        "nuclear": "#9b59b6",
        "battery": "#2ecc71",
    }
    for ax, scen in zip(axes, ("S0", "S1", "S2", "S3")):
        p = tag_dir / scen / "generation_by_asset.csv"
        if not p.is_file():
            ax.set_title(f"{scen}: missing")
            continue
        df = pd.read_csv(p)
        if "mean_W" not in df.columns:
            ax.set_title(f"{scen}: no mean_W")
            continue
        g = df.groupby("fuel")["mean_W"].sum() / 1e9
        labels = [f for f in fuels_keep if f in g.index]
        vals = [g[f] for f in labels]
        ax.bar(range(len(labels)), vals, color=[colors.get(f, "#333") for f in labels])
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=75, ha="right", fontsize=7)
        ax.set_title(scen)
        ax.set_ylabel("Mean generation (GW)")
    fig.suptitle(f"Diurnal-mean dispatch mix ({tag_dir.name})")
    fig.tight_layout()
    out = C.FIGURES_ASTR_DIR / "diurnal_dispatch_by_scenario.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Wrote {out}")


def plot_scenario_status() -> None:
    frames = []
    for p in [C.ASTR_RESULTS_DIR / "four_week" / "scenario_summary.csv"]:
        if p.is_file():
            frames.append(pd.read_csv(p))
    for week in C.SEASONAL_WEEKS:
        p = C.ASTR_RESULTS_DIR / week["name"] / "scenario_summary.csv"
        if p.is_file():
            frames.append(pd.read_csv(p))
    if not frames:
        return
    df = pd.concat(frames, ignore_index=True)
    idx = "tag" if "tag" in df.columns else "season"
    if idx not in df.columns:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    status = df.groupby([idx, "scenario"])["status"].first().unstack()
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
    plot_substation_heatmap()
    plot_top_hubs()
    plot_corridor_capacities()
    plot_binding_lines()
    plot_pcong_mbess()
    plot_diurnal_dispatch()
    plot_scenario_status()
    print(f"Diagnostics in {C.FIGURES_ASTR_DIR}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
