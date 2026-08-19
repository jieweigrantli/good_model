"""
10_03_select_stressed_weeks.py

If 8760 dispatch exists, rank weeks by binding line-hours and fossil generation.
Otherwise confirm the four representative seasonal weeks used by the 4-week test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import common as C

BA_RESULTS = C.REPO_ROOT / "ev_charging_results"


def _weeks_from_8760_binding() -> pd.DataFrame | None:
    path = C.ASTR_RESULTS_DIR / "8760" / "S1" / "line_flows_summary.csv"
    hourly = C.ASTR_RESULTS_DIR / "8760" / "S1" / "line_hourly_binding.parquet"
    if hourly.is_file():
        df = pd.read_parquet(hourly)
        if "hour" in df.columns and "binding" in df.columns:
            df["week"] = (df["hour"] // 168).astype(int)
            agg = df.groupby("week", as_index=False)["binding"].sum().rename(columns={"binding": "binding_hours"})
            agg = agg.sort_values("binding_hours", ascending=False)
            return agg
    if path.is_file():
        # Annual corridor totals only — cannot recover week ranks; fall through
        return None
    return None


def _ba_screen() -> pd.DataFrame:
    seasons = ["march", "june", "september", "december"]
    rows = []
    for s in seasons:
        sdir = BA_RESULTS / s
        fossil_delta = np.nan
        co2_delta = np.nan
        note = ""
        data_dir = sdir / "data"
        if data_dir.is_dir():
            for pat in (
                "wecc_marginal_emissions_by_fuel.csv",
                "marginal_emissions_by_fuel.csv",
                "period_consequential_gen.csv",
            ):
                p = data_dir / pat
                if not p.is_file():
                    continue
                df = pd.read_csv(p)
                note = pat
                fuel_col = next((c for c in df.columns if "fuel" in c.lower()), None)
                gen_col = next(
                    (c for c in df.columns if "marginal" in c.lower() or "gen" in c.lower()),
                    None,
                )
                co2_col = next((c for c in df.columns if "co2" in c.lower()), None)
                if fuel_col and gen_col:
                    fossil = df[df[fuel_col].astype(str).str.lower().isin(["natural gas", "coal", "oil"])]
                    fossil_delta = float(fossil[gen_col].sum())
                if co2_col:
                    co2_delta = float(df[co2_col].sum())
                break
        rows.append(
            {
                "season": s,
                "fossil_marginal_proxy": fossil_delta,
                "co2_delta_proxy": co2_delta,
                "source_note": note,
                "selected_for_meso": True,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    C.ensure_dir(C.FIGURES_ASTR_DIR)
    C.ensure_dir(C.ASTR_RESULTS_DIR)

    ranked = _weeks_from_8760_binding()
    if ranked is not None and len(ranked):
        ranked.to_csv(C.ASTR_RESULTS_DIR / "stressed_weeks_8760.csv", index=False)
        top = ranked.head(8)
        print("Top congestion weeks from 8760 S1 binding hours:")
        print(top.to_string(index=False))
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(ranked["week"].astype(str), ranked["binding_hours"], color="#C0392B")
        ax.set_xlabel("Week of year")
        ax.set_ylabel("Binding line-hours")
        ax.set_title("8760 S1 congestion by week")
        fig.tight_layout()
        fig.savefig(C.FIGURES_ASTR_DIR / "stressed_weeks_8760.png", dpi=150)
        plt.close(fig)
    else:
        print("No 8760 binding-hour series yet — confirming the four seasonal weeks.")

    out = _ba_screen()
    out_path = C.ASTR_RESULTS_DIR / "stressed_week_screen.csv"
    out.to_csv(out_path, index=False)
    print(f"Wrote {out_path}")

    if not out.empty:
        fig, ax = plt.subplots(figsize=(7, 4))
        x = np.arange(len(out))
        vals = out["co2_delta_proxy"].fillna(0).to_numpy()
        ylab = "CO2 delta proxy"
        if np.all(vals == 0):
            vals = out["fossil_marginal_proxy"].fillna(0).to_numpy()
            ylab = "Fossil marginal proxy"
        ax.bar(x, vals, color="#E67E22")
        ax.set_xticks(x)
        ax.set_xticklabels(out["season"])
        ax.set_ylabel(ylab)
        ax.set_title("BA consequential stress screen (existing EV results)")
        fig.tight_layout()
        fig.savefig(C.FIGURES_ASTR_DIR / "ba_stress_screen_by_season.png", dpi=150)
        plt.close(fig)

    confirm = pd.DataFrame(C.SEASONAL_WEEKS)
    confirm.to_csv(C.ASTR_RESULTS_DIR / "confirmed_seasonal_weeks.csv", index=False)
    meta = {
        "representative_weeks": C.SEASONAL_WEEKS,
        "four_week_hours": C.NUM_HOURS_FOUR_WEEK,
        "used_8760_ranking": ranked is not None,
    }
    (C.ASTR_RESULTS_DIR / "stressed_week_selection.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
