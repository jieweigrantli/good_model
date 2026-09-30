"""
10_10_cost_transmission_vs_storage.py

Levelised cost per tonne of CO2 avoided, for the corridor upgrades sized by
08_15 and the battery fleet sized by 08_14.

Why this replaces the earlier $/kW estimate
-------------------------------------------
The first pass costed transmission with Li & Jenn's per-kW bracket. Theirs is
drawn from PG&E's Distribution Investment Deferral Framework, i.e. **distribution
feeder** projects, and most of what 08_15 wants upgraded is sub-transmission at
60-115 kV with a few 230 kV corridors. Applying a feeder cost there was a
placeholder, and it was carrying the conclusion. This script costs each corridor
from its own voltage, length and required circuit count instead.

Two things the sizing exposes, both of which change the answer
--------------------------------------------------------------
**42% of the "corridor" build is not a line.** Of the 11,838 MW, 4,916 MW sits on
42 BA interfaces. Those are not physical corridors: 09_01 derives an interface
capacity from the external transmission a voltage gateway sees beyond the modelled
network, so there is no route, no length and no voltage. They cannot be costed per
mile, and are costed as substation capacity instead, separately and flagged.

**Sixteen of the 116 real corridors are at distribution voltage** (13 at 12 kV,
3 at 4.16 kV, 183 MW between them). For those Li & Jenn's feeder bracket is the
*right* source rather than the wrong one, so it is used there and only there.

Sources
-------
* Line cost per mile by voltage: CTC Global (2025), $200-400k/mile for 34.5-69 kV,
  $400-800k for 115-138 kV, $1.2-2.0M for 230 kV. Cross-checked against
  WECC/Black & Veatch (2014) via UT Austin FCe (2017), which gives $959,700/mile
  for 230 kV single circuit in 2014 dollars; escalated at US CPI to 2024 that is
  about $1.29M, inside the CTC band. The CTC figures are a vendor white paper with
  no citations, which is why the WECC cross-check matters and why a range is
  carried rather than a point.
* Line terminal per position: MISO Transmission Cost Estimation Guide MTEP19,
  assembled from circuit breaker, disconnect switch, voltage and current
  transformers and protection and control. 2019 dollars, escalated 1.20x to 2024.
* Substation capacity for the BA interfaces: WECC/Black & Veatch transformer cost,
  $75,000-250,000 per MVA.
* Distribution-voltage corridors: Li & Jenn (2024), $240-800/kW, the 25th to 75th
  percentile of reported DIDF project cost per kW.
* Storage: EPA Platform v6 Table 4-35, $1,977/kW for a 4-hour battery, 15-year
  life.

Discounting
-----------
Levelised with a capital recovery factor rather than straight-line, because the
two assets have very different lives and straight-line flatters the longer one.
At 5% real, CRF is 0.0583 over 40 years and 0.0963 over 15, a ratio of 1.65,
where straight-line annualisation implied 40/15 = 2.67 in transmission's favour.
Discounting therefore narrows transmission's advantage, and the rate is worth
stating explicitly for that reason.

Writes astr_meso_results/cost_per_tonne.csv
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

OUT_CSV = C.ASTR_RESULTS_DIR / "cost_per_tonne.csv"

MILES_PER_M = 1.0 / 1609.344

# Route circuity: substation coordinates give a straight line, real routes are
# longer. 1.25 is the middle of the 1.2-1.4 commonly assumed for transmission
# routing; it multiplies every line cost, so it is called out rather than buried.
CIRCUITY = 1.25

# $/mile, single circuit overhead, 2024 dollars. (low, high)
LINE_COST_PER_MILE = [
    (0.0, 34.0, None),                  # distribution voltage: costed per kW instead
    (34.0, 100.0, (200_000, 400_000)),   # CTC 34.5-69 kV
    (100.0, 160.0, (400_000, 800_000)),  # CTC 115-138 kV
    (160.0, 1000.0, (1_200_000, 2_000_000)),  # CTC 230 kV
]

# $/position, both ends of every new circuit. MISO MTEP19 assembled, x1.20 to 2024.
TERMINAL_COST = [(0.0, 100.0, 536_000), (100.0, 160.0, 575_000), (160.0, 1000.0, 721_000)]

SUBSTATION_PER_MVA = (75_000, 250_000)    # WECC transformer, for the BA interfaces
DISTRIBUTION_PER_KW = (240, 800)          # Li & Jenn, for the <34 kV corridors
STORAGE_PER_KW = 1_977                    # EPA Platform v6, 4-hour

LIFE_LINE, LIFE_STORAGE = 40, 15
WEEKS_PER_YEAR = 52 / 4                   # four seasonal weeks stand in for the year


def _band(kv, table):
    for lo, hi, v in table:
        if lo <= kv < hi:
            return v
    return table[-1][2]


def crf(rate: float, life: int) -> float:
    if rate <= 0:
        return 1.0 / life
    return rate / (1.0 - (1.0 + rate) ** -life)


def _haversine_miles(lon1, lat1, lon2, lat2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a)) * MILES_PER_M


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="four_week_WEC_CALN")
    ap.add_argument("--rate", type=float, default=0.05, help="Real discount rate.")
    ap.add_argument("--circuity", type=float, default=CIRCUITY)
    args = ap.parse_args()

    net = json.loads(Path(C.CA_NETWORK_JSON).read_text(encoding="utf-8"))
    coords = {n["hub_id"]: (n["lon"], n["lat"]) for n in net["nodes"]}
    edges = {}
    for e in net["edges"]:
        edges[(e["source"], e["target"])] = e
        edges[(e["target"], e["source"])] = e

    spec = pd.read_csv(C.MESO_DIR / "transmission_sized_to_overload.csv")
    up = spec[spec.need_MW >= 0.1].copy()
    up["is_iface"] = up.line.str.startswith("iface_")

    rows = []
    for _, r in up.iterrows():
        e = edges.get((r.source, r.target))

        # `or 0.0` is not enough: a NaN is truthy, so it would survive the guard
        # and then blow up when the class label casts it to int.
        def num(key):
            v = e.get(key) if e else None
            try:
                v = float(v)
            except (TypeError, ValueError):
                return 0.0
            return 0.0 if not np.isfinite(v) else v

        kv = num("rated_kv")
        mva = num("rated_mva")
        n_lines = max(int(e.get("n_lines") or 1), 1) if e else 1
        per_circuit = (mva / n_lines) if mva > 0 else 0.0

        miles = 0.0
        if not r.is_iface and r.source in coords and r.target in coords:
            miles = _haversine_miles(*coords[r.source], *coords[r.target]) * args.circuity

        circuits = math.ceil(r.need_MW / per_circuit) if per_circuit > 0 else 0

        rows.append(dict(line=r.line, need_MW=r.need_MW, is_iface=bool(r.is_iface),
                         kv=kv, per_circuit_MVA=per_circuit, circuits=circuits, miles=miles))
    d = pd.DataFrame(rows)

    # --- cost each class on its own basis -----------------------------------
    def cost(row, side):
        i = 0 if side == "low" else 1
        if row.is_iface:
            return row.need_MW * SUBSTATION_PER_MVA[i]
        if row.kv < 34.0:
            return row.need_MW * 1e3 * DISTRIBUTION_PER_KW[i]
        band = _band(row.kv, LINE_COST_PER_MILE)
        line = row.circuits * row.miles * band[i]
        term = 2 * row.circuits * _band(row.kv, TERMINAL_COST)
        return line + term

    d["cost_low"] = d.apply(lambda r: cost(r, "low"), axis=1)
    d["cost_high"] = d.apply(lambda r: cost(r, "high"), axis=1)

    def cls(r):
        if r.is_iface:
            return "BA interface (substation capacity)"
        if r.kv < 34.0:
            return "distribution voltage corridor"
        return f"{int(r.kv)} kV corridor"

    d["klass"] = d.apply(cls, axis=1)
    by = d.groupby("klass").agg(n=("need_MW", "size"), MW=("need_MW", "sum"),
                                circuits=("circuits", "sum"), miles=("miles", "sum"),
                                low=("cost_low", "sum"), high=("cost_high", "sum"))
    by = by.sort_values("MW", ascending=False)

    print(f"circuity factor {args.circuity}   discount rate {args.rate:.1%}")
    print()
    print("transmission build, costed by class:")
    show = by.copy()
    show["low"] = (show.low / 1e6).round(0)
    show["high"] = (show.high / 1e6).round(0)
    show["miles"] = show.miles.round(0)
    print(show.to_string(float_format=lambda v: f"{v:,.0f}"))
    t_low, t_high = d.cost_low.sum(), d.cost_high.sum()
    print(f"\ntotal transmission: {d.need_MW.sum():,.0f} MW, "
          f"{d.miles.sum():,.0f} route-miles, {int(d.circuits.sum()):,} new circuits")
    print(f"  overnight: ${t_low/1e9:.2f}B to ${t_high/1e9:.2f}B")

    # --- benefits -----------------------------------------------------------
    root = C.ASTR_RESULTS_DIR / args.tag

    def co2(s):
        t = (root / s / "objective.txt").read_text(encoding="utf-8")
        return float(re.search(r"co2_kg=([\d.eE+]+)", t).group(1))

    p_cong = (co2("S1") - co2("S5")) / 1e3 * WEEKS_PER_YEAR
    m_bess = (co2("S1") - co2("S2_550MW")) / 1e3 * WEEKS_PER_YEAR

    bess = pd.read_csv(C.MESO_DIR / "bess_sized_to_curtailment.csv")
    b_mw = float(bess.power_MW.sum())
    b_cost = b_mw * 1e3 * STORAGE_PER_KW

    crf_l, crf_s = crf(args.rate, LIFE_LINE), crf(args.rate, LIFE_STORAGE)
    print(f"\ncapital recovery factor at {args.rate:.1%}: "
          f"lines {crf_l:.4f} ({LIFE_LINE} yr), storage {crf_s:.4f} ({LIFE_STORAGE} yr)"
          f"  -> ratio {crf_s/crf_l:.2f}")
    print(f"annual benefit: transmission {p_cong:,.0f} t/yr, storage {m_bess:,.0f} t/yr")
    print()
    out = []
    for lab, cap, c, life, ben in (
        ("transmission (low)", t_low, crf_l, LIFE_LINE, p_cong),
        ("transmission (high)", t_high, crf_l, LIFE_LINE, p_cong),
        ("storage 550 MW", b_cost, crf_s, LIFE_STORAGE, m_bess),
    ):
        ann = cap * c
        out.append(dict(item=lab, overnight_USD=cap, life_yr=life,
                        annualised_USD=ann, annual_tonnes=ben, USD_per_tonne=ann / ben))
    res = pd.DataFrame(out)
    print(f"{'item':22} {'overnight':>12} {'levelised/yr':>14} {'$/t':>9}")
    for _, r in res.iterrows():
        print(f"{r['item']:22} ${r.overnight_USD/1e9:11,.2f}B ${r.annualised_USD/1e6:13,.0f}M "
              f"{r.USD_per_tonne:9,.0f}")

    C.ensure_dir(OUT_CSV.parent)
    res.to_csv(OUT_CSV, index=False)
    d.to_csv(C.ASTR_RESULTS_DIR / "transmission_cost_detail.csv", index=False)
    print(f"\n  wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
