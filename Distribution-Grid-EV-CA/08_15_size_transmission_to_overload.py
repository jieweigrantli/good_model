"""
08_15_size_transmission_to_overload.py

Size each corridor's upgrade to the overload actually observed, instead of
multiplying every corridor by a blanket factor.

Why this replaces the 10x relaxation
------------------------------------
S3 and S4 multiply every corridor and interface by ten. That is a useful
diagnostic -- it answers "how much CO2 is corridor congestion costing, if
delivery were free" -- but it is not a build, and it makes the answer
uncostable. Measured on the four-week PG&E run, S3 is granted 6,521,774 MW of
extra capacity and its peak flows exceed the base rating on only 155 of 3,084
corridors, by 11,830 MW in total: **0.181% of the grant**. So 99.8% of the
relaxation does nothing, and dividing the 31,700 t benefit by the granted
capacity gives a meaningless number.

Sizing to the observed overload puts transmission on the same footing as
storage. `08_14` reads a solved scenario's per-node curtailment and builds the
battery fleet the model actually needs; this reads a solved scenario's
per-corridor flows and builds the transmission the model actually needs. Both
are then costable, and P_cong and M_BESS become comparable per MW and per dollar.

Following Li & Jenn (2024)
--------------------------
Their feeder upgrade rule is adopted here for corridors, because it is the
published method this project is anchored to and it answers the same question
one level up the grid:

* **Upgrade need = maximum overload over the horizon.** They take, per feeder,
  `max_t(baseload_t + EV_t - capacity)` clipped at zero, using the single worst
  hour in each year with no averaging. Here the equivalent is
  `max_t(flow_t) - capacity` on each corridor, taken from a run where the
  corridor was not binding, so the peak flow reveals the capacity the
  unconstrained optimum wants.
* **Only the overload is costed**, not full replacement of the line.
* **Congestion is reported in their two metrics**, so the numbers are directly
  comparable with their feeder results:
    - *overload intensity* = peak load / capacity  (>1 means the corridor would
      carry more than its rating if allowed to)
    - *overload frequency* = overload hours / all hours

One difference is worth stating. Li & Jenn compute overload arithmetically --
load is given, so the overload is just a subtraction. Flow on a meshed network
is not given, it is a decision, so the "load" a corridor would carry has to come
from a solve in which that corridor was free. The relaxed scenario supplies it.
That makes this a one-pass estimate: the upgraded network may route differently
and want slightly more somewhere else. The run itself checks that -- if the
targeted build reproduces the relaxed scenario's shortfall, one pass sufficed.

Writes data/meso/transmission_sized_to_overload.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

OUT_CSV = C.MESO_DIR / "transmission_sized_to_overload.csv"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="four_week_WEC_CALN")
    ap.add_argument("--relaxed", default="S3",
                    help="Scenario whose peak flows reveal the wanted capacity. "
                         "S3 relaxes corridors only; S4 also relaxes the substation "
                         "transformers, so sizing from S4 answers a different question.")
    ap.add_argument("--base", default="S1",
                    help="Scenario supplying the as-built rating each upgrade is measured against.")
    ap.add_argument("--headroom", type=float, default=1.0,
                    help="Multiply the required capacity by this before writing it. 1.0 sizes "
                         "exactly to the observed peak, which is Li & Jenn's rule. Anything "
                         "above 1.0 is a planning margin and should be stated as such.")
    ap.add_argument("--min-need-mw", type=float, default=0.1,
                    help="Ignore upgrades smaller than this; they are solver noise, not builds.")
    args = ap.parse_args()

    root = C.ASTR_RESULTS_DIR / args.tag
    paths = {s: root / s / "line_flows_summary.csv" for s in (args.base, args.relaxed)}

    for s, p in paths.items():
        if not p.is_file():
            raise SystemExit(
                f"{p} missing. Run {s} in this horizon/tag first so 10_01 writes the "
                f"per-corridor flow summary."
            )

    key = ["source", "target", "line"]
    base = pd.read_csv(paths[args.base])[key + ["capacity_MW", "peak_flow_MW", "binding_hours", "n_hours"]]
    relax = pd.read_csv(paths[args.relaxed])[key + ["capacity_MW", "peak_flow_MW", "binding_hours"]]
    d = base.merge(relax, on=key, suffixes=("_base", "_relaxed"))

    if d.empty:
        raise SystemExit("No corridors matched between the two scenarios.")

    n_hours = int(d["n_hours"].max())

    # Li & Jenn's two congestion metrics, on the as-built network.
    d["overload_intensity"] = d["peak_flow_MW_relaxed"] / d["capacity_MW_base"].replace(0, np.nan)
    d["overload_frequency"] = d["binding_hours_base"] / max(n_hours, 1)

    # Upgrade need: the peak the unconstrained optimum wants, less what is built.
    d["need_MW"] = (d["peak_flow_MW_relaxed"] - d["capacity_MW_base"]).clip(lower=0.0)
    d["target_capacity_MW"] = d["capacity_MW_base"] + d["need_MW"] * args.headroom

    granted = float((d["capacity_MW_relaxed"] - d["capacity_MW_base"]).sum())
    up = d[d["need_MW"] >= args.min_need_mw].copy()

    print(f"corridors: {len(d):,}   horizon: {n_hours} h")
    print(f"as-built capacity          : {d['capacity_MW_base'].sum():,.0f} MW")
    print(f"binding line-hours         : {args.base} {int(d['binding_hours_base'].sum()):,}"
          f"  ->  {args.relaxed} {int(d['binding_hours_relaxed'].sum()):,}")
    print()
    print(f"corridors needing an upgrade : {len(up):,} of {len(d):,} "
          f"({len(up)/len(d)*100:.1f}%)")
    print(f"capacity actually required   : {up['need_MW'].sum():,.0f} MW")

    if granted > 0:
        print(f"capacity the {args.relaxed} relaxation granted : {granted:,.0f} MW")
        print(f"  fraction of the grant that does work : "
              f"{up['need_MW'].sum() / granted * 100:.3f}%")

    print()
    print("Li & Jenn congestion metrics on the corridors needing an upgrade:")
    print(f"  overload intensity (peak wanted / rating)  median {up['overload_intensity'].median():.2f}"
          f"   max {up['overload_intensity'].max():.1f}")
    print(f"  overload frequency (binding hours / all)   median {up['overload_frequency'].median():.2f}"
          f"   max {up['overload_frequency'].max():.2f}")
    print()
    print("largest upgrades:")
    show = ["line", "capacity_MW_base", "peak_flow_MW_relaxed", "need_MW",
            "overload_intensity", "overload_frequency"]
    print(up.nlargest(10, "need_MW")[show].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    # Everything is written, not only the upgraded corridors, so 09_02 can apply
    # one lookup and leave untouched corridors at their as-built rating rather
    # than having to decide what a missing row means.
    d["target_capacity_W"] = d["target_capacity_MW"] * 1e6
    cols = key + ["capacity_MW_base", "peak_flow_MW_base", "peak_flow_MW_relaxed",
                  "need_MW", "target_capacity_MW", "target_capacity_W",
                  "overload_intensity", "overload_frequency", "binding_hours_base"]
    C.ensure_dir(OUT_CSV.parent)
    d[cols].to_csv(OUT_CSV, index=False)

    print()
    print(f"  fleet-equivalent comparison: transmission {up['need_MW'].sum():,.0f} MW added"
          f" vs the 550 MW storage fleet in S2")
    print(f"  wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
