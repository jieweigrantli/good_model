"""
08_14_size_bess_to_curtailment.py

Size each substation's battery against the energy being *spilled* there, not
against the deficit and not against its EV peak.

Why curtailment and not deficit
-------------------------------
Three fleets have now been tested and none moved the result. The tell was which
batteries actually cycled: in the EV-peak fleet, 8 of the 11 that discharged sat
at nodes with **no deficit at all** -- SUB_18295 (2,240 MW nuclear, 1,301 GWh
spilled) and SUB_04314 (926 MW geothermal, 608 GWh) among them. They were
soaking up stranded must-run generation. Sizing to the deficit then deleted
batteries from exactly those nodes, because a node with no shortfall gets no
allocation, and utilisation fell from 0.85 cycles to 0.08.

The asymmetry is physical. A battery at a **deficit** node cannot charge:
charging means importing more through the path that is already capped, and the
worst deficit nodes are short in 672 of 672 hours. A battery at a **surplus**
node has energy available by definition -- the generator is spilling it.

The catch, and what this script measures
----------------------------------------
Surplus-side storage has the mirror problem. A battery that charges from
curtailment must discharge later, which needs an hour when the outbound corridor
has slack. Curtailment happens precisely because that corridor is full, so if a
node spills in nearly every hour there may be no discharge window either.

So `hours_free` -- hours when the node is *not* spilling -- is the quantity that
decides whether this works, and a LONG curtailment run is bad here, the opposite
of how run length reads for deficits. SUB_18295 spills roughly 1,937 MW on
average against 2,240 MW of nuclear, i.e. most hours, which is why the batteries
there moved only 0.671 GWh despite 1,301 GWh being available. This script
reports that ratio explicitly rather than assuming the surplus is reachable.

Sizing rule
-----------
* **Power** = peak spilled MW at the node, capped by `--max-mw` so one nuclear
  site does not absorb the entire fleet.
* **Energy** = power x duration, with duration from `--duration-h`. Not taken
  from the curtailment run length, because a battery cannot usefully store more
  than it can later discharge, and the discharge window is `hours_free`.
* Nodes are ranked by spilled energy and taken until `--fleet-mw` is reached, so
  the result is comparable in scale to the fleets already tested.

Writes data/meso/bess_sized_to_curtailment.csv
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

OUT_CSV = C.MESO_DIR / "bess_sized_to_curtailment.csv"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="four_week_WEC_CALN")
    ap.add_argument("--scenario", default="S1")
    ap.add_argument("--duration-h", type=float, default=4.0)
    ap.add_argument("--max-mw", type=float, default=2000.0,
                    help="Cap on any single node's battery power.")
    ap.add_argument("--fleet-mw", type=float, default=None,
                    help="Stop adding nodes once the fleet reaches this power. "
                         "Default: no cap, size every spilling node.")
    ap.add_argument("--min-mw", type=float, default=1.0)
    ap.add_argument("--min-free-hours", type=int, default=None,
                    help="Skip nodes with fewer free hours than this. Defaults to "
                         "--duration-h, because a battery needs at least its own "
                         "duration in non-spilling hours to complete a single "
                         "discharge; below that it can charge and never deliver. The "
                         "previous default of 1 excluded only the hours_free = 0 case "
                         "and let a worse one through: at three-IOU scope SUB_HIFLD_2620 "
                         "and SUB_HIFLD_4169 spill in 671 of 672 hours with hours_free = 1, "
                         "and between them hold 361 GWh -- 51%% of all substation spill -- "
                         "so they claimed 983 MW of a 3,200 MW fleet while contributing "
                         "1.0 GWh of reachable energy.")
    args = ap.parse_args()
    if args.min_free_hours is None:
        args.min_free_hours = int(max(1, round(args.duration_h)))

    path = C.ASTR_RESULTS_DIR / args.tag / args.scenario / "wastage_by_node.csv"
    if not path.is_file():
        raise SystemExit(
            f"{path} missing. Re-run {args.scenario} so 10_01 writes the per-node "
            f"curtailment shape (_wastage_by_node)."
        )
    d = pd.read_csv(path)
    # Only substations can carry a battery in this model; BA nodes (WECC_IID and
    # friends) already have their own optional storage tier.
    d = d[d["node"].astype(str).str.startswith("SUB_")].copy()
    if d.empty:
        raise SystemExit("No substation-level curtailment found.")

    unreachable = d[d["hours_free"] < args.min_free_hours]
    if len(unreachable):
        print(f"  EXCLUDED, spill in every hour so a battery could never discharge: "
              f"{len(unreachable)} nodes, {unreachable['wastage_GWh'].sum():,.1f} GWh "
              f"({unreachable['wastage_GWh'].sum() / d['wastage_GWh'].sum() * 100:.0f}% of spill)")
        for _, r in unreachable.iterrows():
            print(f"     {r['node']:18} {r['wastage_GWh']:9,.1f} GWh  "
                  f"mean {r['mean_MW']:7,.1f} MW  hours_free {int(r['hours_free'])}")
    d = d[d["hours_free"] >= args.min_free_hours]
    if d.empty:
        raise SystemExit("Every spilling node spills continuously; storage cannot help.")

    d = d.sort_values("wastage_GWh", ascending=False)
    print(f"substation nodes spilling energy in {args.scenario}: {len(d)}")
    print(f"  total spilled: {d['wastage_GWh'].sum():,.1f} GWh")
    print()
    print("  the quantity that decides whether surplus-side storage can work is")
    print("  hours_free -- hours the node is NOT spilling, when a battery could discharge:")
    show = ["node", "wastage_GWh", "peak_MW", "mean_MW", "hours_spilling",
            "hours_free", "longest_run_h"]
    print(d[show].head(10).to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    d["power_MW"] = d["peak_MW"].clip(lower=args.min_mw, upper=args.max_mw)
    if args.fleet_mw:
        keep = d["power_MW"].cumsum() <= args.fleet_mw
        # always keep at least the largest node
        keep.iloc[0] = True
        d = d[keep]
    d["duration_h"] = args.duration_h
    d["energy_MWh"] = d["power_MW"] * d["duration_h"]
    d["capex_capacity_W"] = d["power_MW"] * 1e6
    d["substation_id"] = d["node"].str.replace("SUB_", "", regex=False)

    # How much of the spilled energy a battery could actually re-deliver, bounded
    # by the discharge window rather than by the surplus.
    d["dischargeable_MWh"] = np.minimum(
        d["energy_MWh"] * np.maximum(d["hours_free"], 0) / np.maximum(args.duration_h, 1e-9),
        d["wastage_GWh"] * 1e3,
    )

    C.ensure_dir(OUT_CSV.parent)
    cols = ["node", "substation_id", "power_MW", "duration_h", "energy_MWh",
            "peak_MW", "mean_MW", "wastage_GWh", "hours_spilling", "hours_free",
            "longest_run_h", "dischargeable_MWh", "capex_capacity_W"]
    d[cols].to_csv(OUT_CSV, index=False)

    print()
    print(f"  fleet: {len(d)} nodes, {d['power_MW'].sum():,.0f} MW, "
          f"{d['energy_MWh'].sum():,.0f} MWh")
    print(f"  for comparison: EV-peak 1,163 MW / 4,652 MWh; "
          f"deficit-sized 1,069 MW / 6,892 MWh")
    print()
    print(f"  spilled energy at these nodes      : {d['wastage_GWh'].sum():,.1f} GWh")
    print(f"  bounded by the discharge window    : {d['dischargeable_MWh'].sum() / 1e3:,.1f} GWh")
    frac = d["dischargeable_MWh"].sum() / 1e3 / max(d["wastage_GWh"].sum(), 1e-9)
    print(f"  reachable fraction                 : {frac * 100:.1f}%")
    if frac < 0.05:
        print()
        print("  NOTE: a low reachable fraction means these nodes spill in nearly every")
        print("  hour, so there is no window to discharge into. That would make")
        print("  surplus-side storage fail for the mirror of the reason deficit-side")
        print("  storage fails, and the run will show it.")
    print(f"\n  wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
