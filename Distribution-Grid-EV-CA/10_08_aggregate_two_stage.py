"""
10_08_aggregate_two_stage.py

Sum the four stage-2 seasonal weeks into one annual-equivalent result per
scenario, and derive P_cong and M_BESS from the totals.

Why two stages
--------------
The 672 h four-week LP does not solve. Its barrier stalls at a relative gap of
~3e-4 and hands a sub-optimal point to crossover, which then runs 2.27M
iterations in 4 h without converging -- crossover cost grows superlinearly in
model size, and at 1.25M presolved rows it is past what this machine can push.
Each single week is a quarter that size (314k presolved rows) and does reach a
vertex in 20-55 min.

Splitting into four independent weekly LPs would be wrong on its own, because
each week would buy its own capacity and the four would describe four different
systems. So stage 1 solves all four weeks jointly, barrier-only at a sizing
tolerance, purely to decide the build; stage 2 fixes that build and dispatches
each week against it. Capacity is co-optimised across seasons exactly as the
single-shot LP intended; only the dispatch is separable, and dispatch genuinely
is separable once capacity is fixed, because nothing in this model couples one
seasonal week to the next -- the weeks are already non-contiguous samples and
storage state does not carry between them.

What this costs
---------------
Stage 2 cannot re-optimise capacity against the dispatch it discovers, so the
build is only as good as stage 1's sizing tolerance. Reported objectives are
therefore an upper bound on the true single-shot optimum: a feasible build
dispatched optimally. The gap is bounded by stage 1's own gap (~3e-4 relative),
which is far smaller than the differences between scenarios that this study
reports.

Accounting
----------
CO2, shortfall, wastage and generation are extensive in time, so they sum over
the four weeks. The objective does not: stage 1's CAPEX is an annual charge
incurred once, while each stage-2 week carries only operating cost. This script
therefore reports summed operating cost and keeps CAPEX separate rather than
quietly counting it four times.

Writes:
  astr_meso_results/two_stage_summary.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

SCEN_ORDER = ["S0", "S0R", "S1", "S2", "S3", "S4"]


def _week_tag(season: str, only_ba: set[str] | None) -> str:
    return f"{season}_{'+'.join(sorted(only_ba))}" if only_ba else season


def collect(scen: str, only_ba: set[str] | None) -> dict | None:
    """Sum one scenario over the four seasonal weeks. None if any week is missing."""
    tot = {
        "scenario": scen,
        "weeks": 0,
        "n_hours": 0,
        "operating_cost": 0.0,
        "co2_kg": 0.0,
        "shortfall_GWh": 0.0,
        "wastage_GWh": 0.0,
    }
    fuels: dict[str, float] = {}
    missing = []
    for wk in C.SEASONAL_WEEKS:
        d = C.ASTR_RESULTS_DIR / _week_tag(wk["name"], only_ba) / scen
        obj_path = d / "objective.txt"
        if not obj_path.is_file():
            missing.append(f"{wk['name']}/{scen}")
            continue
        kv = dict(
            line.split("=", 1)
            for line in obj_path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        tot["operating_cost"] += float(kv.get("objective", 0.0))
        tot["co2_kg"] += float(kv.get("co2_kg", 0.0))
        tot["n_hours"] += int(float(kv.get("n_hours", 0)))
        tot["weeks"] += 1

        sw = d / "shortfall_wastage.json"
        if sw.is_file():
            blob = json.loads(sw.read_text(encoding="utf-8"))
            tot["shortfall_GWh"] += float(blob.get("shortfall_GWh", 0.0))
            tot["wastage_GWh"] += float(blob.get("wastage_GWh", 0.0))

        gen = d / "generation_by_asset.csv"
        if gen.is_file():
            g = pd.read_csv(gen)
            if not g.empty:
                for fuel, e in g.groupby("fuel")["energy_MWh"].sum().items():
                    fuels[str(fuel)] = fuels.get(str(fuel), 0.0) + float(e)

    if missing:
        print(f"  {scen}: incomplete, missing {', '.join(missing)}")
        return None
    for fuel, e in fuels.items():
        # MWh -> GWh; v1 summed energy_J and divided by 3.6e12
        tot[f"gen_{fuel}_GWh"] = e / 1e3
    return tot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-ba", nargs="*", default=None,
                    help="Same value passed to 10_01, so the week tags match.")
    ap.add_argument("--stage1-capex", default=None,
                    help="stage1 capex JSON, to report the build alongside dispatch.")
    args = ap.parse_args()
    only_ba = set(args.only_ba) if args.only_ba else None

    rows = []
    for scen in SCEN_ORDER:
        rec = collect(scen, only_ba)
        if rec is not None:
            rows.append(rec)
    if not rows:
        raise SystemExit("No complete scenario found. Run stage 2 first.")

    df = pd.DataFrame(rows).set_index("scenario")

    if args.stage1_capex and Path(args.stage1_capex).is_file():
        blob = json.loads(Path(args.stage1_capex).read_text(encoding="utf-8"))
        df["stage1_build_MW"] = [
            sum(v for _, _, v in blob.get(s, []) if v) / 1e6 for s in df.index
        ]

    out = C.ASTR_RESULTS_DIR / "two_stage_summary.csv"
    df.to_csv(out)

    pd.set_option("display.width", 200)
    print("\n" + df.to_string(float_format=lambda v: f"{v:,.3f}"))

    co2 = df["co2_kg"].to_dict()

    def have(*keys):
        return all(k in co2 for k in keys)

    print("\nDerived metrics (t CO2):")
    if have("S0", "S1", "S3"):
        p_cong = (co2["S1"] - co2["S0"]) - (co2["S3"] - co2["S0"])
        print(f"  P_cong  (vs shared S0)    : {p_cong / 1e3:>12,.0f}")
        if have("S0R"):
            p_cong_fixed = (co2["S1"] - co2["S0"]) - (co2["S3"] - co2["S0R"])
            print(f"  P_cong  (S3 vs own S0R)   : {p_cong_fixed / 1e3:>12,.0f}   <- use this one")
        else:
            print("    S0R absent: this figure still charges EVs for the fact that relaxing "
                  "transmission\n    also delivers base load better. Run --scenarios S0R to "
                  "separate the two.")
    if have("S0", "S1", "S2"):
        m_bess = (co2["S1"] - co2["S0"]) - (co2["S2"] - co2["S0"])
        print(f"  M_BESS                    : {m_bess / 1e3:>12,.0f}")

    print(f"\n  wrote {out}")


if __name__ == "__main__":
    main()
