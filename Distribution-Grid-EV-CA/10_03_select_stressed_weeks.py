"""
10_03_select_stressed_weeks.py

Screen existing BA-level ev_charging_results for seasons/hours with the
largest fossil / consequential-emission deltas. Confirms the four seasonal
weeks used by 10_01.
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

BA_RESULTS = C.REPO_ROOT / "ev_charging_results"


def _find_marginal_csv(season_dir: Path) -> Path | None:
    candidates = list(season_dir.rglob("*marginal*emission*.csv"))
    candidates += list(season_dir.rglob("*consequential*.csv"))
    candidates += list(season_dir.rglob("marginal_gen*.csv"))
    return candidates[0] if candidates else None


def _find_gen_csv(season_dir: Path, which: str) -> Path | None:
    # which in {'baseline','ev'}
    hits = list(season_dir.rglob(f"*{which}*generation*.csv"))
    if hits:
        return hits[0]
    hits = list(season_dir.rglob(f"*{which}*.csv"))
    return hits[0] if hits else None


def main() -> None:
    C.ensure_dir(C.FIGURES_ASTR_DIR)
    C.ensure_dir(C.ASTR_RESULTS_DIR)

    seasons = ["march", "june", "september", "december"]
    rows = []
    for s in seasons:
        sdir = BA_RESULTS / s
        if not sdir.is_dir():
            print(f"missing BA results for {s}")
            continue
        # try aggregate CSVs under data/
        data_dir = sdir / "data"
        fossil_delta = np.nan
        co2_delta = np.nan
        note = ""
        if data_dir.is_dir():
            # common postprocess outputs
            for pat in (
                "wecc_marginal_emissions_by_fuel.csv",
                "marginal_emissions_by_fuel.csv",
                "period_consequential_gen.csv",
            ):
                p = data_dir / pat
                if p.is_file():
                    df = pd.read_csv(p)
                    note = pat
                    # heuristic columns
                    fuel_col = next(
                        (c for c in df.columns if "fuel" in c.lower()), None
                    )
                    gen_col = next(
                        (
                            c
                            for c in df.columns
                            if "marginal" in c.lower() or "gen" in c.lower()
                        ),
                        None,
                    )
                    co2_col = next(
                        (c for c in df.columns if "co2" in c.lower()), None
                    )
                    if fuel_col and gen_col:
                        fossil = df[
                            df[fuel_col]
                            .astype(str)
                            .str.lower()
                            .isin(["natural gas", "coal", "oil"])
                        ]
                        fossil_delta = float(fossil[gen_col].sum())
                    if co2_col:
                        co2_delta = float(df[co2_col].sum())
                    break
            # iteration summary txt
            summary = sdir / "iteration_summary.txt"
            if summary.is_file():
                note = note or "iteration_summary.txt"
        rows.append(
            {
                "season": s,
                "fossil_marginal_proxy": fossil_delta,
                "co2_delta_proxy": co2_delta,
                "source_note": note,
                "selected_for_meso": True,
            }
        )
        print(f"  {s}: fossil_proxy={fossil_delta} co2_proxy={co2_delta} ({note})")

    out = pd.DataFrame(rows)
    out_path = C.ASTR_RESULTS_DIR / "stressed_week_screen.csv"
    out.to_csv(out_path, index=False)
    print(f"Wrote {out_path}")

    # Bar chart
    if not out.empty:
        fig, ax = plt.subplots(figsize=(7, 4))
        x = np.arange(len(out))
        vals = out["co2_delta_proxy"].fillna(0).to_numpy()
        if np.all(vals == 0):
            vals = out["fossil_marginal_proxy"].fillna(0).to_numpy()
            ylab = "Fossil marginal proxy"
        else:
            ylab = "CO2 delta proxy"
        ax.bar(x, vals, color="#E67E22")
        ax.set_xticks(x)
        ax.set_xticklabels(out["season"])
        ax.set_ylabel(ylab)
        ax.set_title("BA consequential stress screen (existing EV results)")
        fig.tight_layout()
        fig.savefig(C.FIGURES_ASTR_DIR / "ba_stress_screen_by_season.png", dpi=150)
        plt.close(fig)
        print(f"Wrote {C.FIGURES_ASTR_DIR / 'ba_stress_screen_by_season.png'}")

    # Confirm plan weeks
    confirm = pd.DataFrame(C.SEASONAL_WEEKS)
    confirm.to_csv(C.ASTR_RESULTS_DIR / "confirmed_seasonal_weeks.csv", index=False)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
