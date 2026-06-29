"""
aggregate_showcase.py — Cross-scenario comparison figures for the showcase.

Builds summary_metrics.csv and comparison plots under
ev_charging_showcase_results/aggregate/.
"""

from __future__ import annotations

import glob
import os
import sys
import warnings

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ev_charging_showcase.config as config
from ev_charging_project.postprocess import FUEL_COLORS, FUEL_ORDER, _fuel_stack_order
from ev_charging_showcase.utils import load_scaled_ev_profile


def _data_dir(output_root: str, scenario_name: str) -> str:
    return os.path.join(output_root, scenario_name, "data")


def _read_csv_or_empty(path: str, columns: list[str] | None = None) -> pd.DataFrame:
    """Read CSV; return empty frame with expected columns if file is missing/blank."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        cols = columns or []
        return pd.DataFrame(columns=cols)
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        cols = columns or []
        return pd.DataFrame(columns=cols)


_CAPEX_COLS = ["region", "asset", "fuel", "capex_MW"]


def run_aggregate_showcase(output_root: str | None = None) -> str | None:
    output_root = os.path.abspath(output_root or os.path.join(_ROOT, config.OUTPUT_DIR))
    scenarios = list(config.SCENARIOS)
    num_hours = config.NUM_HOURS
    start_hour = config.JUNE_START_HOUR

    missing = []
    for sc in scenarios:
        dd = _data_dir(output_root, sc["name"])
        for fn in (
            "baseline_generation.csv",
            "ev_generation.csv",
            "period_consequential_emissions.csv",
        ):
            if not os.path.isfile(os.path.join(dd, fn)):
                missing.append(os.path.join(dd, fn))
                break

    if missing:
        print(
            "Aggregate showcase skipped — missing data:\n  "
            + "\n  ".join(missing[:6])
        )
        return None

    out_dir = os.path.join(output_root, "aggregate")
    plots_dir = os.path.join(out_dir, "plots")
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(plots_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    loaded = []
    for sc in scenarios:
        dd = _data_dir(output_root, sc["name"])
        loaded.append(
            {
                "scenario": sc,
                "baseline_gen": pd.read_csv(os.path.join(dd, "baseline_generation.csv")),
                "ev_gen": pd.read_csv(os.path.join(dd, "ev_generation.csv")),
                "hourly": pd.read_csv(os.path.join(dd, "hourly_consequential_emissions.csv")),
                "period": pd.read_csv(os.path.join(dd, "period_consequential_emissions.csv")),
                "capex_b": _read_csv_or_empty(
                    os.path.join(dd, "capex_baseline.csv"), _CAPEX_COLS
                ),
                "capex_e": _read_csv_or_empty(
                    os.path.join(dd, "capex_ev.csv"), _CAPEX_COLS
                ),
                "ev_profile_w": load_scaled_ev_profile(sc, start_hour, num_hours),
            }
        )

    rows = []
    for L in loaded:
        sc = L["scenario"]
        per = L["period"]
        mg = per["marginal_generation_kwh"].sum()
        c2 = per["co2_emissions_kg"].sum()
        with np.errstate(divide="ignore", invalid="ignore"):
            g_co2 = float(c2 / mg * 1000.0) if abs(mg) > 1e-9 else np.nan

        def _capex_mw(df: pd.DataFrame) -> dict:
            if df is None or df.empty:
                return {"solar": 0.0, "wind": 0.0, "battery": 0.0}
            g = df.groupby("fuel")["capex_MW"].sum()
            return {
                k: float(g.get(k, 0.0) + g.get(k.capitalize(), 0.0))
                for k in ("solar", "wind", "battery")
            }

        cb, ce = _capex_mw(L["capex_b"]), _capex_mw(L["capex_e"])
        ev_kwh = float(np.sum(L["ev_profile_w"]) / 1000.0)
        rows.append(
            {
                "scenario": sc["name"],
                "label": sc["label"],
                "year": sc["year"],
                "fleet_millions": sc["fleet_millions"],
                "ev_scale": sc["ev_scale"],
                "total_ev_load_kwh": ev_kwh,
                "g_co2_per_kwh_marginal": g_co2,
                "delta_capex_solar_mw": ce["solar"] - cb["solar"],
                "delta_capex_wind_mw": ce["wind"] - cb["wind"],
                "delta_capex_battery_mw": ce["battery"] - cb["battery"],
                "ev_capex_solar_mw": ce["solar"],
                "ev_capex_wind_mw": ce["wind"],
                "ev_capex_battery_mw": ce["battery"],
            }
        )

    summary_df = pd.DataFrame(rows)
    csv_path = os.path.join(data_dir, "summary_metrics.csv")
    summary_df.to_csv(csv_path, index=False)
    print(f"  Aggregate summary: {csv_path}")

    # --- Objective comparison (read from scenario_meta if present) ---
    obj_rows = []
    for sc in scenarios:
        meta_path = os.path.join(output_root, sc["name"], "scenario_meta.json")
        if os.path.isfile(meta_path):
            import json

            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            obj_rows.append(
                {
                    "label": sc["label"],
                    "year": sc["year"],
                    "baseline": meta.get("baseline_objective"),
                    "ev": meta.get("ev_objective"),
                }
            )
    if obj_rows:
        obj_df = pd.DataFrame(obj_rows)
        fig, ax = plt.subplots(figsize=(10, 5))
        x = np.arange(len(obj_df))
        w = 0.35
        ax.bar(x - w / 2, obj_df["baseline"] / 1e6, w, label="Baseline", color="#4C72B0")
        ax.bar(x + w / 2, obj_df["ev"] / 1e6, w, label="EV scenario", color="#DD8452")
        ax.set_xticks(x)
        ax.set_xticklabels(
            [f"{r['label']}\n({int(r['year'])})" for _, r in obj_df.iterrows()],
            rotation=0,
            fontsize=9,
        )
        ax.set_ylabel("Objective ($M)")
        ax.set_title("Showcase scenarios — optimization objective (June week)")
        ax.legend()
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        p = os.path.join(plots_dir, "aggregate_objectives_by_scenario.png")
        fig.savefig(p, dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"  Saved: {p}")

    # --- Marginal CO2 intensity vs fleet size ---
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(
        summary_df["fleet_millions"],
        summary_df["g_co2_per_kwh_marginal"],
        "o-",
        color="#C44E52",
        linewidth=2,
        markersize=8,
    )
    for _, r in summary_df.iterrows():
        ax.annotate(
            str(int(r["year"])),
            (r["fleet_millions"], r["g_co2_per_kwh_marginal"]),
            textcoords="offset points",
            xytext=(4, 4),
            fontsize=9,
        )
    ax.set_xlabel("EV fleet (millions)")
    ax.set_ylabel("Marginal CO₂ (g/kWh)")
    ax.set_title("Consequential emission factor vs fleet size (June week)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    p = os.path.join(plots_dir, "marginal_co2_vs_fleet.png")
    fig.savefig(p, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {p}")

    # --- CAPEX delta by scenario ---
    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(summary_df))
    w = 0.25
    ax.bar(x - w, summary_df["delta_capex_solar_mw"], w, label="Solar ΔMW", color=FUEL_COLORS.get("solar", "#FDB813"))
    ax.bar(x, summary_df["delta_capex_wind_mw"], w, label="Wind ΔMW", color=FUEL_COLORS.get("wind", "#5B9BD5"))
    ax.bar(x + w, summary_df["delta_capex_battery_mw"], w, label="Battery ΔMW", color=FUEL_COLORS.get("battery", "#70AD47"))
    ax.set_xticks(x)
    ax.set_xticklabels(summary_df["label"], rotation=15, ha="right", fontsize=9)
    ax.set_ylabel("Δ CAPEX (MW, EV − baseline)")
    ax.set_title("Incremental CAPEX expansion by scenario")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    p = os.path.join(plots_dir, "delta_capex_by_scenario.png")
    fig.savefig(p, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {p}")

    # --- Hourly marginal generation by fuel (5-panel) ---
    n = len(loaded)
    ncols = 3
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows), sharex=True)
    axes = np.atleast_1d(axes).flatten()
    fuels = list(FUEL_ORDER)
    for i, L in enumerate(loaded):
        ax = axes[i]
        sc = L["scenario"]
        hourly = L["hourly"]
        hours = np.arange(num_hours)
        stacks = []
        labels = []
        for fuel in fuels:
            sub = hourly[hourly["fuel"] == fuel]
            if sub.empty:
                continue
            vals = sub.groupby("hour")["marginal_generation_kwh"].sum().reindex(hours, fill_value=0).values
            if np.abs(vals).sum() > 0:
                stacks.append(vals)
                labels.append(fuel)
        if stacks:
            ax.stackplot(
                hours / 24.0,
                *stacks,
                labels=labels,
                colors=[FUEL_COLORS.get(f, "#CCC") for f in labels],
                alpha=0.85,
            )
        ax.set_title(f"{sc['label']} ({sc['year']})")
        ax.set_xlabel("Day")
        ax.set_ylabel("Marginal gen (kWh)")
        ax.grid(alpha=0.3)
    for j in range(len(loaded), len(axes)):
        axes[j].set_visible(False)
    handles, labels = axes[0].get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc="upper center", ncol=5, bbox_to_anchor=(0.5, 1.02))
    fig.suptitle("Hourly consequential generation by fuel — showcase scenarios", y=1.05)
    fig.tight_layout()
    p = os.path.join(plots_dir, "aggregate_marginal_gen_by_scenario.png")
    fig.savefig(p, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {p}")

    return csv_path


if __name__ == "__main__":
    run_aggregate_showcase()
