"""
08_13_size_bess_to_deficit.py

Size each substation's battery against the deficit the LP actually leaves
there, instead of against its EV peak.

Why the existing sizing fails
-----------------------------
`09_01::candidate_bess` sizes every battery at `max(EV_peak * 0.5, 1 MW)`.
That scales storage to EV load, but the unserved energy in these runs is
dominated by base load that cannot be delivered. The mismatch is an order of
magnitude: across the eight worst nodes the prescribed fleet is 12.3 MW against
a 118.2 MW average deficit, a ratio of 0.10, and most sites sit on the 1 MW
floor. The consequence is that S2 changed almost nothing -- 11 of 786 batteries
discharged at all, 0.85 full cycles over four weeks, 3% utilisation, and an
M_BESS of 315 t that sits inside solver noise. That is not a finding about
storage; it is a finding about the sizing rule.

What this sizes to
------------------
Two ratings, because a battery is bounded by two different things:

* **Power** must cover the peak hourly deficit. Taken as the maximum across
  S0 and S1 so the battery is sized for the worse of "no EV" and "with EV",
  which is what a planner would install against.
* **Energy** only has to cover the deficit inside one contiguous run of short
  hours, since the battery recharges once the node recovers. A node short
  20 MW for six hours a night needs ~120 MWh; a node short 20 MW for a week
  needs storage it can never recharge.

That second point is the real test. Where the deficit is sustained rather than
peaky, no battery of any size helps, because there is no surplus hour to charge
from -- the constraint is delivery, not timing. `--max-run-hours` caps the
duration that will be prescribed so the output stays a buildable fleet rather
than a notional one, and the script reports how much deficit sits beyond that
cap as *unstorable*.

Data-deficit nodes are excluded
-------------------------------
Nodes whose transformer rating is itself a guess are dropped, because a battery
sized against a deficit that an invented rating created would be sized against
our own error. Excluded by default: any node whose rating_source is not `ica`,
which removes the `HIFLD_*` substations that have no PG&E identity at all
(~40 GWh of S0 shortfall, rated `peak / 0.856`) and the substations with ICA
headroom but no measured baseload such as SF K and SF L (~14 GWh). What remains
is deficit measured against capacity PG&E itself publishes.

Writes data/meso/bess_sized_to_deficit.csv
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

OUT_CSV = C.MESO_DIR / "bess_sized_to_deficit.csv"
RESULTS = C.ASTR_RESULTS_DIR


def _load(tag: str, scen: str) -> pd.DataFrame:
    p = RESULTS / tag / scen / "shortfall_by_node.csv"
    if not p.is_file():
        raise SystemExit(
            f"{p} missing. Re-run {scen} so 10_01 writes the per-node deficit shape."
        )
    d = pd.read_csv(p)
    d["scenario"] = scen
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="four_week_WEC_CALN")
    ap.add_argument("--scenarios", nargs="*", default=["S0", "S1"],
                    help="Size to the worst deficit across these.")
    ap.add_argument("--max-run-hours", type=float, default=8.0,
                    help="Longest contiguous deficit a battery is asked to cover. "
                         "Beyond this the deficit is reported as unstorable.")
    ap.add_argument("--power-headroom", type=float, default=1.0,
                    help="Multiply the peak deficit by this to get power rating.")
    ap.add_argument("--allow-sources", nargs="*", default=["ica"],
                    help="Only size nodes whose rating came from these sources.")
    ap.add_argument("--min-mw", type=float, default=0.5)
    args = ap.parse_args()

    parts = [_load(args.tag, s) for s in args.scenarios]
    df = pd.concat(parts, ignore_index=True)

    # worst case across the scenarios, per node
    per = df.groupby("node", as_index=False).agg(
        peak_MW=("peak_MW", "max"),
        shortfall_GWh=("shortfall_GWh", "max"),
        hours_short=("hours_short", "max"),
        longest_run_h=("longest_run_h", "max"),
        longest_run_MWh=("longest_run_MWh", "max"),
    )
    print(f"nodes with a deficit in {'/'.join(args.scenarios)}: {len(per):,}")

    ratings = pd.read_csv(C.MESO_DIR / "substation_ratings.csv")
    per = per.merge(
        ratings[["hub_id", "rating_W", "rating_source"]],
        left_on="node", right_on="hub_id", how="left",
    )

    excluded = per[~per["rating_source"].isin(args.allow_sources)]
    per = per[per["rating_source"].isin(args.allow_sources)].copy()
    print(f"  excluded (rating is itself a guess): {len(excluded):,} nodes, "
          f"{excluded['shortfall_GWh'].sum():,.1f} GWh of deficit")
    print(f"  sizing against                     : {len(per):,} nodes, "
          f"{per['shortfall_GWh'].sum():,.1f} GWh")
    if per.empty:
        raise SystemExit("Nothing left to size.")

    per["power_MW"] = (per["peak_MW"] * args.power_headroom).clip(lower=args.min_mw)
    # Energy covers one contiguous run, capped so the fleet stays buildable.
    per["duration_h"] = per["longest_run_h"].clip(upper=args.max_run_hours).clip(lower=1.0)
    per["energy_MWh"] = per["power_MW"] * per["duration_h"]
    # What a battery cannot reach: deficit inside runs longer than the cap.
    over = per["longest_run_h"] > args.max_run_hours
    per["unstorable_MWh"] = np.where(
        over,
        per["longest_run_MWh"] - per["energy_MWh"],
        0.0,
    ).clip(min=0)

    per["capex_capacity_W"] = per["power_MW"] * 1e6
    per["substation_id"] = per["node"].str.replace("SUB_", "", regex=False)

    C.ensure_dir(OUT_CSV.parent)
    cols = ["node", "substation_id", "power_MW", "duration_h", "energy_MWh",
            "peak_MW", "shortfall_GWh", "hours_short", "longest_run_h",
            "longest_run_MWh", "unstorable_MWh", "rating_W", "capex_capacity_W"]
    per[cols].sort_values("power_MW", ascending=False).to_csv(OUT_CSV, index=False)

    print()
    print(f"  fleet power   : {per['power_MW'].sum():,.0f} MW")
    print(f"  fleet energy  : {per['energy_MWh'].sum():,.0f} MWh")
    print(f"  vs prescribed : 1,163 MW / 4,652 MWh (EV-peak rule)")
    print(f"  scale-up      : {per['power_MW'].sum() / 1163:,.1f}x power")
    print()
    print(f"  deficit inside runs <= {args.max_run_hours:.0f} h (storable) : "
          f"{(per['longest_run_MWh'] - per['unstorable_MWh']).sum():,.0f} MWh")
    print(f"  deficit in longer runs (unstorable)        : "
          f"{per['unstorable_MWh'].sum():,.0f} MWh")
    print()
    print("  longest contiguous deficit run, distribution (h):")
    print(per["longest_run_h"].describe().to_string())
    print(f"\n  wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
