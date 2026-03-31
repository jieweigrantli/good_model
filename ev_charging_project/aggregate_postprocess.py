"""
aggregate_postprocess.py — Cross-season figures and summary tables.

Run after all seasonal iterations have produced data/ CSVs under OUTPUT_DIR.
Called automatically from run_all.py, or:

    python -m ev_charging_project.aggregate_postprocess
"""

from __future__ import annotations

import glob
import os
import sys
import warnings

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

warnings.filterwarnings("ignore")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ev_charging_project.config as config
from ev_charging_project.postprocess import (
    FUEL_COLORS,
    FUEL_ORDER,
    _fuel_stack_order,
    get_wecc_stackplot_bundle,
)
from ev_charging_project.utils import (
    load_ev_profile_for_hours,
    load_solution_json,
    prepare_graph,
    slice_graph_profiles,
)

# Row order in 2x2 / 4x2 grids: top-left, top-right, bottom-left, bottom-right
_SEASON_GRID = [
    ("march", "March"),
    ("june", "June"),
    ("september", "September"),
    ("december", "December"),
]


def _iter_data_dir(output_root: str, season_name: str) -> str:
    return os.path.join(output_root, season_name, "data")


def _find_latest_json(folder: str, prefix: str) -> str | None:
    pattern = os.path.join(folder, f"{prefix}_solution_*.json")
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def run_aggregate_postprocess(output_root: str | None = None) -> str | None:
    """
    Build aggregate plots and summary_metrics.csv under output_root/aggregate/.
    Returns path to CSV if successful, else None.
    """
    output_root = os.path.abspath(output_root or os.path.join(_ROOT, config.OUTPUT_DIR))
    num_hours = config.NUM_HOURS

    seasons_cfg = list(config.ITERATIONS)
    missing = []
    for it in seasons_cfg:
        dd = _iter_data_dir(output_root, it["name"])
        for fn in ("baseline_generation.csv", "ev_generation.csv", "period_consequential_emissions.csv"):
            if not os.path.isfile(os.path.join(dd, fn)):
                missing.append(os.path.join(dd, fn))
                break

    if missing:
        print(
            "Aggregate postprocess skipped: missing data for one or more seasons:\n  "
            + "\n  ".join(missing[:8])
        )
        return None

    out_dir = os.path.join(output_root, "aggregate")
    plots_dir = os.path.join(out_dir, "plots")
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(plots_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    # Load per-season data
    loaded = []
    for it in seasons_cfg:
        name = it["name"]
        dd = _iter_data_dir(output_root, name)
        b = pd.read_csv(os.path.join(dd, "baseline_generation.csv"))
        e = pd.read_csv(os.path.join(dd, "ev_generation.csv"))
        hourly = pd.read_csv(os.path.join(dd, "hourly_consequential_emissions.csv"))
        period = pd.read_csv(os.path.join(dd, "period_consequential_emissions.csv"))
        capex_b = pd.read_csv(os.path.join(dd, "capex_baseline.csv"))
        capex_e = pd.read_csv(os.path.join(dd, "capex_ev.csv"))
        ev_w = load_ev_profile_for_hours(config, it["start_hour"], num_hours)
        loaded.append(
            {
                "name": name,
                "month": it.get("month", name),
                "start_hour": it["start_hour"],
                "baseline_gen": b,
                "ev_gen": e,
                "hourly": hourly,
                "period": period,
                "capex_b": capex_b,
                "capex_e": capex_e,
                "ev_profile_w": ev_w,
            }
        )

    # --- Summary metrics table ---
    rows = []
    for L in loaded:
        bsum = L["baseline_gen"]["generation_kwh"].sum()
        esum = L["ev_gen"]["generation_kwh"].sum()
        ev_kwh_total = float(np.sum(L["ev_profile_w"]) / 1000.0)
        per = L["period"]
        mg = per["marginal_generation_kwh"].sum()
        c2 = per["co2_emissions_kg"].sum()
        nx = per["nox_emissions_kg"].sum()
        s2 = per["so2_emissions_kg"].sum()
        # period totals: emissions in kg, marginal gen in kWh → g pollutant / kWh = (kg/kWh) * 1000
        with np.errstate(divide="ignore", invalid="ignore"):
            g_co2 = float(c2 / mg * 1000.0) if abs(mg) > 1e-9 else np.nan
            g_nox = float(nx / mg * 1000.0) if abs(mg) > 1e-9 else np.nan
            g_so2 = float(s2 / mg * 1000.0) if abs(mg) > 1e-9 else np.nan
        # GHG-eq (CO2-only from marginal gen; same basis as grid carbon intensity)
        g_ghg_eq = g_co2

        def _capex_by_fuel(df: pd.DataFrame) -> dict:
            if df is None or df.empty:
                return {"solar": 0.0, "wind": 0.0, "battery": 0.0}
            g = df.groupby("fuel")["capex_MW"].sum()
            out = {}
            for k in ("solar", "wind", "battery"):
                out[k] = float(g.get(k, 0.0) + g.get(k.capitalize(), 0.0))
            return out

        cb = _capex_by_fuel(L["capex_b"])
        ce = _capex_by_fuel(L["capex_e"])
        rows.append(
            {
                "season": L["name"],
                "month_label": L["month"],
                "delta_total_generation_kwh": float(esum - bsum),
                "total_ev_load_kwh": ev_kwh_total,
                "total_baseline_generation_kwh": float(bsum),
                "total_ev_scenario_generation_kwh": float(esum),
                "g_co2_per_kwh_marginal": g_co2,
                "g_nox_per_kwh_marginal": g_nox,
                "g_so2_per_kwh_marginal": g_so2,
                "ghg_eq_gco2e_per_kwh_marginal": g_ghg_eq,
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
    try:
        summary_df.to_csv(csv_path, index=False)
        print(f"  Aggregate summary: {csv_path}")
    except PermissionError as exc:
        # If the CSV is open in Excel/another viewer, overwrite may fail.
        print(f"  WARNING: could not write {csv_path} ({exc}). Continuing with plots.")

    # --- 1) Eight-panel WECC balance (stack + storage + loads; x = days 0–7) ---
    import good
    from good.reload import deep_reload

    season_bundles: list[tuple[dict, int]] = []
    json_ok = True
    deep_reload(good)
    raw_graph = good.graph.graph_from_json(config.GRAPH_FILE)
    data_graph = prepare_graph(raw_graph, config)

    for it, L in zip(seasons_cfg, loaded):
        sdir = os.path.join(output_root, it["name"])
        b_path = _find_latest_json(os.path.join(sdir, "baseline"), "baseline")
        e_path = _find_latest_json(os.path.join(sdir, "ev"), "ev")
        if not b_path or not e_path:
            print(f"  aggregate 8-panel: missing solution JSON under {sdir}")
            json_ok = False
            break
        try:
            baseline_sol, b_meta = load_solution_json(b_path)
            ev_sol, e_meta = load_solution_json(e_path)
            nh_loc = int(b_meta.get("num_hours", num_hours))
            sh_loc = int(b_meta.get("start_hour", it["start_hour"]))
        except Exception as exc:
            print(f"  aggregate 8-panel: failed loading JSON for {it['name']}: {exc}")
            json_ok = False
            break
        ev_slice = load_ev_profile_for_hours(config, sh_loc, nh_loc)
        baseline_graph = slice_graph_profiles(data_graph, sh_loc, nh_loc)
        ev_graph = slice_graph_profiles(data_graph, sh_loc, nh_loc)
        bundle = get_wecc_stackplot_bundle(
            baseline_sol,
            baseline_graph,
            ev_sol,
            ev_graph,
            L["baseline_gen"],
            L["ev_gen"],
            ev_slice,
            nh_loc,
        )
        season_bundles.append((bundle, nh_loc))

    if json_ok and len(season_bundles) == len(seasons_cfg):
        def _panel_y_extents(bun: dict, nh_loc: int, is_ev: bool) -> tuple[float, float]:
            bal = bun["e"] if is_ev else bun["b"]
            gdf = bun["e_gdf"] if is_ev else bun["b_gdf"]
            fl = bun["e_fo"] if is_ev else bun["b_fo"]
            if fl:
                ys = np.array([gdf[f].values for f in fl])
                bottom = ys.sum(axis=0)
            else:
                bottom = np.zeros(nh_loc)
            top = bottom + bal["dis_GW"][:nh_loc]
            ymin = float(np.min(-bal["chg_GW"][:nh_loc]))
            ymax = float(np.max(top))
            ymax = max(ymax, float(np.max(bal["load_GW"][:nh_loc])))
            if is_ev:
                bl = bun["b"]["load_GW"][:nh_loc]
                ymax = max(ymax, float(np.max(bl + bun["ev_charge_load_GW"][:nh_loc])))
            pad_y = (ymax - ymin) * 0.04 + 1e-6
            return ymin - pad_y, ymax + pad_y

        nh_max = max(nh for _, nh in season_bundles)
        x_day_max = nh_max / 24.0

        y_lo, y_hi = float("inf"), float("-inf")
        for bundle, nh_loc in season_bundles:
            for is_ev in (False, True):
                a, b = _panel_y_extents(bundle, nh_loc, is_ev)
                y_lo, y_hi = min(y_lo, a), max(y_hi, b)

        all_fuels = set()
        for bundle, _ in season_bundles:
            all_fuels |= set(bundle["b_fo"]) | set(bundle["e_fo"])
        fo = _fuel_stack_order(all_fuels)

        fig, axes = plt.subplots(4, 2, figsize=(11, 18), sharex=True, sharey=True)
        labels_panel = list("abcdefgh")
        day_grid = np.arange(1, int(np.ceil(nh_max / 24.0)) + 1)

        for r in range(4):
            bundle, nh_loc = season_bundles[r]

            for c, is_ev in enumerate((False, True)):
                ax = axes[r, c]
                days = np.arange(nh_loc, dtype=float) / 24.0
                bal = bundle["e"] if is_ev else bundle["b"]
                gdf = bundle["e_gdf"] if is_ev else bundle["b_gdf"]
                fl = [f for f in fo if f in gdf.columns and gdf[f].abs().max() > 0]
                if fl:
                    ys = [gdf[f].values for f in fl]
                    ax.stackplot(
                        days,
                        *ys,
                        colors=[FUEL_COLORS.get(f, "#CCC") for f in fl],
                        alpha=0.85,
                    )
                    bottom = np.sum(np.array(ys), axis=0)
                else:
                    bottom = np.zeros(nh_loc)

                ax.fill_between(
                    days,
                    bottom,
                    bottom + bal["dis_GW"][:nh_loc],
                    color=FUEL_COLORS["battery"],
                    alpha=0.6,
                    zorder=3,
                )
                ax.fill_between(
                    days,
                    0,
                    -bal["chg_GW"][:nh_loc],
                    color="#F39C12",
                    alpha=0.6,
                    zorder=3,
                )

                if is_ev:
                    bl = bundle["b"]["load_GW"][:nh_loc]
                    ax.plot(days, bl, "k-", lw=2.2, zorder=4)
                    ax.plot(
                        days,
                        bl + bundle["ev_charge_load_GW"][:nh_loc],
                        color="magenta",
                        ls="--",
                        lw=1.8,
                        zorder=4,
                    )
                else:
                    ax.plot(days, bal["load_GW"][:nh_loc], "k-", lw=2.2, zorder=4)

                sf_mask = bal["sf_GW"][:nh_loc] > 0.01
                if sf_mask.any():
                    ax.scatter(
                        days[sf_mask],
                        bal["load_GW"][:nh_loc][sf_mask],
                        color="red",
                        s=12,
                        zorder=5,
                    )

                for d in day_grid:
                    if d < x_day_max + 1e-9:
                        ax.axvline(d, color="#D8D8D8", lw=0.9, zorder=0)

                ax.axhline(0, color="gray", lw=0.5, zorder=1)
                ax.set_xlim(0, x_day_max)
                ax.set_ylim(y_lo, y_hi)
                ax.set_box_aspect(1)
                ax.grid(True, alpha=0.3, axis="y", zorder=2)
                ax.text(
                    0.02,
                    0.98,
                    f"({labels_panel[r * 2 + c]})",
                    transform=ax.transAxes,
                    va="top",
                    ha="left",
                    fontsize=11,
                    fontweight="bold",
                    zorder=6,
                )
                if r == 3:
                    ax.set_xlabel("Day")
                    ax.set_xticks(np.arange(0, int(x_day_max) + 1))
                if c == 0:
                    ax.set_ylabel("Power (GW)")

        h_fuel = [
            Rectangle((0, 0), 1, 1, fc=FUEL_COLORS.get(f, "#CCC"), alpha=0.85, ec="none")
            for f in fo
        ]
        leg_handles = h_fuel + [
            Patch(facecolor=FUEL_COLORS["battery"], alpha=0.6, edgecolor="none"),
            Patch(facecolor="#F39C12", alpha=0.6, edgecolor="none"),
            Line2D([0], [0], color="k", lw=2.2),
            Line2D([0], [0], color="magenta", ls="--", lw=1.8),
        ]
        leg_labels = list(fo) + [
            "Storage discharge",
            "Storage charging",
            "Base Load",
            "Base Load + EV",
        ]

        fig.tight_layout()
        # Narrower plot block so the legend sits clearly to the right of all panels
        fig.subplots_adjust(hspace=0.09, wspace=0.10, right=0.74, top=0.98)
        fig.legend(
            leg_handles,
            leg_labels,
            loc="upper left",
            bbox_to_anchor=(0.76, 0.99),
            fontsize=7,
            framealpha=0.95,
            borderpad=0.35,
        )
        p1 = os.path.join(plots_dir, "aggregate_stacked_generation_8panel.png")
        plt.savefig(p1, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  Saved: {p1}")
    else:
        print("  aggregate 8-panel: skipped (need baseline + ev solution JSON for each season).")

    # --- 2) Hourly consequential generation by fuel — 2×2, unified colors ---
    y_min, y_max = 0.0, 0.0
    for L in loaded:
        hr = L["hourly"]
        if hr.empty:
            continue
        for fuel, sub in hr.groupby("fuel"):
            if str(fuel).lower() == "import":
                continue
            v = sub["marginal_generation_kwh"].values / 1e6
            y_min = min(y_min, float(np.min(v)))
            y_max = max(y_max, float(np.max(v)))
    pad = (y_max - y_min) * 0.05 + 1e-6
    y0, y1 = y_min - pad, y_max + pad

    fig, axes = plt.subplots(2, 2, figsize=(11, 11), sharex=True, sharey=True)
    fuels_hourly = sorted(
        set().union(*[set(L["hourly"]["fuel"].unique()) for L in loaded if not L["hourly"].empty])
    )
    fuels_hourly = [f for f in FUEL_ORDER if f in fuels_hourly] + [
        f for f in fuels_hourly if f not in FUEL_ORDER
    ]
    fuels_hourly = [f for f in fuels_hourly if str(f).lower() != "import"]

    for ax, (sname, smonth), L in zip(
        axes.flat,
        _SEASON_GRID,
        loaded,
    ):
        hr = L["hourly"]
        for fuel in fuels_hourly:
            fd = hr[hr["fuel"] == fuel]
            if fd.empty:
                continue
            fd = fd.sort_values("hour")
            if fd["marginal_generation_kwh"].abs().sum() < 1e-9:
                continue
            ax.plot(
                fd["hour"],
                fd["marginal_generation_kwh"] / 1e6,
                lw=1.2,
                label=fuel,
                color=FUEL_COLORS.get(fuel, "#888"),
            )
        ax.set_xlim(0, num_hours - 1)
        ax.set_ylim(y0, y1)
        ax.set_box_aspect(1)
        ax.grid(True, alpha=0.3)
        ax.text(
            0.02,
            0.98,
            smonth,
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=10,
            fontweight="bold",
        )

    for ax in axes[1, :]:
        ax.set_xlabel("Hour")
    for ax in axes[:, 0]:
        ax.set_ylabel("Consequential generation (GW)")

    h_leg = [
        plt.Line2D([0], [0], color=FUEL_COLORS.get(f, "#888"), lw=2, label=f)
        for f in fuels_hourly
        if any(
            not L["hourly"][L["hourly"]["fuel"] == f].empty
            and L["hourly"][L["hourly"]["fuel"] == f]["marginal_generation_kwh"].abs().sum() > 1e-9
            for L in loaded
        )
    ]
    fig.tight_layout()
    # Tighter row gap; less empty margin on the right so legend sits nearer the panels
    fig.subplots_adjust(hspace=0.09, wspace=0.10, right=0.90, top=0.98)
    fig.legend(
        handles=h_leg,
        loc="upper right",
        bbox_to_anchor=(0.99, 0.99),
        fontsize=7,
        ncol=1,
        framealpha=0.95,
        borderpad=0.35,
    )
    p2 = os.path.join(plots_dir, "aggregate_consequential_gen_by_fuel_hourly_2x2.png")
    plt.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {p2}")

    # --- 3) Period total consequential gen by fuel — ranked bars, 2×2 ---
    xmax = 0.0
    period_plots = []
    for L in loaded:
        df = L["period"].copy()
        if df.empty:
            period_plots.append(df)
            continue
        df = df[df["marginal_generation_kwh"].abs() > 1e-6]
        df = df[df["fuel"].astype(str).str.lower() != "import"]
        df = df.assign(
            abs_m=lambda x: x["marginal_generation_kwh"].abs(),
            gwh=lambda x: x["marginal_generation_kwh"] / 1e6,
        )
        # Ascending |marginal| → barh places largest at top, descending top→bottom
        df = df.sort_values("abs_m", ascending=True)
        xmax = max(xmax, float(df["gwh"].abs().max()) if not df.empty else 0.0)
        period_plots.append(df)
    xmax *= 1.1
    if xmax <= 0:
        xmax = 1.0

    fig, axes = plt.subplots(2, 2, figsize=(11, 11), sharex=True)
    for ax, (sname, smonth), pdf in zip(axes.flat, _SEASON_GRID, period_plots):
        if pdf is None or pdf.empty:
            ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
            ax.set_box_aspect(1)
            continue
        fuels = pdf["fuel"].tolist()
        gwh = pdf["gwh"].tolist()
        y = np.arange(len(fuels))
        bar_h = 0.425  # ~half of prior 0.85 — thinner horizontal bars
        for i, (fuel, val) in enumerate(zip(fuels, gwh)):
            ax.barh(
                i,
                val,
                height=bar_h,
                color=FUEL_COLORS.get(fuel, "#CCC"),
                alpha=0.9,
                edgecolor="black",
                linewidth=0.4,
            )
        ax.set_yticks(y)
        ax.set_yticklabels(fuels, fontsize=7)
        ax.axvline(0, color="black", lw=1.0)
        ax.set_xlim(-xmax, xmax)
        ax.set_ylim(-0.5, len(fuels) - 0.5)
        ax.set_box_aspect(1)
        ax.grid(True, alpha=0.3, axis="x")
        ax.text(
            0.02,
            0.98,
            smonth,
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=10,
            fontweight="bold",
        )

    for ax in axes[1, :]:
        ax.set_xlabel("Consequential generation (GWh)")
    fuels_period_legend = set()
    for pdf in period_plots:
        if pdf is not None and not pdf.empty:
            fuels_period_legend |= set(pdf["fuel"].astype(str).unique())
    fuels_period_legend = {f for f in fuels_period_legend if str(f).lower() != "import"}
    fo_period = _fuel_stack_order(fuels_period_legend)

    fig.tight_layout()
    fig.subplots_adjust(hspace=0.09, wspace=0.10, right=0.90, top=0.98)
    fig.legend(
        handles=[
            Rectangle((0, 0), 1, 1, fc=FUEL_COLORS.get(f, "#CCC"), alpha=0.9, ec="k")
            for f in fo_period
        ],
        labels=fo_period,
        loc="upper right",
        bbox_to_anchor=(0.99, 0.99),
        fontsize=7,
        framealpha=0.95,
        borderpad=0.35,
    )
    p3 = os.path.join(plots_dir, "aggregate_period_consequential_gen_by_fuel_2x2.png")
    plt.savefig(p3, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {p3}")

    # --- 4) Combined metric 2×2 (dual-axis, square panels) ---
    # Left axis (GWh): EV load + total consequential generation
    # Right axis (g/kWh): gCO2, gSO2, gNOx for consequential marginal emissions
    colors = {
        "ev_load": "steelblue",
        "conseq_gen": "seagreen",
        "g_co2": "crimson",
        "g_so2": "darkorange",
        "g_nox": "purple",
    }

    _SEASON_ORDER = [it["name"] for it in seasons_cfg]  # expected: march, june, september, december
    season_to_panel = {name: (i // 2, i % 2) for i, name in enumerate(_SEASON_ORDER)}

    # Precompute values per season to keep axis scaling consistent.
    ev_load_gwhs = []
    conseq_gen_gwhs = []
    g_co2s = []
    g_so2s = []
    g_noxs = []
    per_season = {}
    for L in loaded:
        season = L["name"]
        ev_load_gwh = float(np.sum(L["ev_profile_w"]) / 1e9)  # W-sum (Wh) → GWh
        conseq_gen_gwh = float(L["period"]["marginal_generation_kwh"].sum() / 1e6)  # kWh → GWh

        def _get(col: str) -> float:
            row = summary_df.loc[summary_df["season"] == season, col]
            if row.empty:
                return float("nan")
            return float(row.iloc[0])

        g_co2 = _get("g_co2_per_kwh_marginal")
        g_so2 = _get("g_so2_per_kwh_marginal")
        g_nox = _get("g_nox_per_kwh_marginal")

        per_season[season] = {
            "ev_load_gwh": ev_load_gwh,
            "conseq_gen_gwh": conseq_gen_gwh,
            "g_co2": g_co2,
            "g_so2": g_so2,
            "g_nox": g_nox,
        }
        ev_load_gwhs.append(ev_load_gwh)
        conseq_gen_gwhs.append(conseq_gen_gwh)
        g_co2s.append(g_co2)
        g_so2s.append(g_so2)
        g_noxs.append(g_nox)

    def _finite_minmax(vals):
        finite = [v for v in vals if np.isfinite(v)]
        if not finite:
            return 0.0, 1.0
        return float(min(finite)), float(max(finite))

    gwh_lo, gwh_hi = _finite_minmax(ev_load_gwhs + conseq_gen_gwhs)
    gwh_pad = (gwh_hi - gwh_lo) * 0.15 + 1e-9
    gwh_lo, gwh_hi = gwh_lo - gwh_pad, gwh_hi + gwh_pad

    gi_lo, gi_hi = _finite_minmax(g_co2s + g_so2s + g_noxs)
    gi_pad = (gi_hi - gi_lo) * 0.12 + 1e-9
    gi_lo, gi_hi = gi_lo - gi_pad, gi_hi + gi_pad

    fig, axes = plt.subplots(2, 2, figsize=(13, 11))
    fig.subplots_adjust(hspace=0.25, wspace=0.20, right=0.80, top=0.90)

    # x positions inside each panel
    x_left = np.array([0, 1], dtype=float)          # EV load, consequential generation
    x_right = np.array([2, 3, 4], dtype=float)       # gCO2, gSO2, gNOx
    w = 0.55

    legend_handles = [
        Patch(facecolor=colors["ev_load"], alpha=0.95, label="EV load"),
        Patch(facecolor=colors["conseq_gen"], alpha=0.95, label="Consequential generation"),
        Patch(facecolor=colors["g_co2"], alpha=0.95, label="gCO2/kWh"),
        Patch(facecolor=colors["g_so2"], alpha=0.95, label="gSO2/kWh"),
        Patch(facecolor=colors["g_nox"], alpha=0.95, label="gNOx/kWh"),
    ]

    for (season, month_label) in _SEASON_GRID:
        if season not in per_season:
            continue
        r, c = season_to_panel[season]
        axL = axes[r, c]
        axR = axL.twinx()

        axL.set_box_aspect(1)
        axL.grid(True, alpha=0.25, axis="y")

        axL.bar(x_left[0], per_season[season]["ev_load_gwh"], width=w, color=colors["ev_load"], edgecolor="k", linewidth=0.35)
        axL.bar(x_left[1], per_season[season]["conseq_gen_gwh"], width=w, color=colors["conseq_gen"], edgecolor="k", linewidth=0.35)
        axL.set_ylim(gwh_lo, gwh_hi)
        axL.set_ylabel("GWh")

        # Right-axis intensity bars (always non-negative in practice)
        gco2 = per_season[season]["g_co2"] if np.isfinite(per_season[season]["g_co2"]) else 0.0
        gso2 = per_season[season]["g_so2"] if np.isfinite(per_season[season]["g_so2"]) else 0.0
        gnox = per_season[season]["g_nox"] if np.isfinite(per_season[season]["g_nox"]) else 0.0

        axR.bar(x_right[0], gco2, width=w, color=colors["g_co2"], edgecolor="k", linewidth=0.35)
        axR.bar(x_right[1], gso2, width=w, color=colors["g_so2"], edgecolor="k", linewidth=0.35)
        axR.bar(x_right[2], gnox, width=w, color=colors["g_nox"], edgecolor="k", linewidth=0.35)
        axR.set_ylim(gi_lo, gi_hi)
        axR.set_ylabel("g/kWh")

        # Panel label
        axL.text(0.02, 0.98, month_label, transform=axL.transAxes, va="top", ha="left",
                 fontsize=12, fontweight="bold")

        axL.set_xticks(np.arange(0, 5))
        axL.set_xticklabels(["EV", "Consequential", "gCO2", "gSO2", "gNOx"], fontsize=9, rotation=15, ha="right")
        axL.set_xlim(-0.6, 4.6)

    fig.legend(
        handles=legend_handles,
        loc="upper right",
        bbox_to_anchor=(0.99, 1.02),
        fontsize=8,
        framealpha=0.95,
        borderpad=0.35,
    )
    plt.tight_layout(rect=[0, 0, 0.80, 0.98])
    pmetric = os.path.join(plots_dir, "aggregate_metrics_2x2_dual_axis.png")
    plt.savefig(pmetric, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {pmetric}")

    # --- gCO2/kWh by month (single 4-bar plot) ---
    month_labels = []
    gco2_vals = []
    for season, month_label in _SEASON_GRID:
        month_labels.append(month_label)
        row = summary_df.loc[summary_df["season"] == season, "g_co2_per_kwh_marginal"]
        if row.empty:
            gco2_vals.append(np.nan)
        else:
            gco2_vals.append(float(row.iloc[0]))

    bar_colors = ["#4C78A8", "#F58518", "#54A24B", "#E45756"]  # distinct per month
    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    ax.bar(month_labels, gco2_vals, color=bar_colors[: len(month_labels)], edgecolor="k", linewidth=0.4, alpha=0.9)
    ax.set_ylabel("gCO2/kWh")
    ax.set_xlabel("Month (7-day run)")
    ax.axhline(0, color="k", lw=0.8)
    ax.grid(True, alpha=0.25, axis="y")
    plt.tight_layout()
    pg = os.path.join(plots_dir, "aggregate_gco2_per_kwh_marginal_4bars.png")
    plt.savefig(pg, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {pg}")

    # CAPEX — EV scenario optional expansion by tech (MW in CSV → GW on plot)
    _MW_TO_GW = 1e-3
    fig, axes = plt.subplots(2, 2, figsize=(10, 9), sharey=True)
    capex_fuels = ["solar", "wind", "battery"]
    xb = np.arange(len(capex_fuels))
    ymax_mw = 0.0
    for k in capex_fuels:
        col = f"ev_capex_{k}_mw"
        ymax_mw = max(ymax_mw, float(summary_df[col].max()))
    ymax = ymax_mw * _MW_TO_GW * 1.12 + 1e-9
    if ymax < 1e-9:
        ymax = 1.0

    for ax, (sname, smonth) in zip(axes.flat, _SEASON_GRID):
        row = summary_df.loc[summary_df["season"] == sname].iloc[0]
        vals = [max(0.0, float(row[f"ev_capex_{k}_mw"])) * _MW_TO_GW for k in capex_fuels]
        ax.bar(
            xb,
            vals,
            width=0.65,
            color=[FUEL_COLORS.get(k, "#888") for k in capex_fuels],
            edgecolor="k",
            linewidth=0.4,
        )
        ax.set_xticks(xb)
        ax.set_xticklabels(capex_fuels, rotation=15, ha="right")
        ax.set_ylim(0, ymax)
        ax.grid(True, alpha=0.3, axis="y")
        ax.text(0.02, 0.98, smonth, transform=ax.transAxes, va="top", ha="left", fontweight="bold")

    for ax in axes[:, 0]:
        ax.set_ylabel("CAPEX (EV - baseline) [GW]")

    plt.tight_layout()
    pcap = os.path.join(plots_dir, "metric_capex_ev_solar_wind_battery_2x2.png")
    plt.savefig(pcap, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {pcap}")

    print(f"  Aggregate plots directory: {plots_dir}")
    return csv_path


if __name__ == "__main__":
    run_aggregate_postprocess()
