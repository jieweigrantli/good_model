"""
10_06_invariant_checks.py

Validate the framework rather than the numbers.

Why invariants and not results
------------------------------
Much of the input is still interpolated: 1,829 of 3,044 transformer ratings
are derived, 342 radial feeds are synthetic, SDG&E and the publicly owned
utilities have no distribution data, and 2,749 corridors are inferred from
geometry. Those gaps move magnitudes, so a headline number computed on them
means little.

What they must not do is fake the mechanism. P_cong is a difference between a
constrained and a relaxed case, so any gap that manufactures scarcity -- an
islanded node, an undersized interface -- registers as congestion and biases
the result toward the finding. These checks are designed to catch that:
several of them are falsification tests that a broken network fails and a
working one passes regardless of how good the input data is.

Structural checks need no solve. Behavioural checks (--solve) run scenarios
on a short horizon.

Usage:
  python 10_06_invariant_checks.py                 structural only
  python 10_06_invariant_checks.py --solve --horizon june
"""

from __future__ import annotations

import argparse
import json

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd

import common as C

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Checks:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool, detail: str, warn_only: bool = False):
        status = PASS if ok else (WARN if warn_only else FAIL)
        self.rows.append((status, name, detail))
        print(f"  [{status:4}] {name}: {detail}")

    def report(self) -> int:
        n_fail = sum(1 for s, _, _ in self.rows if s == FAIL)
        n_warn = sum(1 for s, _, _ in self.rows if s == WARN)
        print(f"\n{len(self.rows)} checks: "
              f"{sum(1 for s, _, _ in self.rows if s == PASS)} pass, "
              f"{n_warn} warn, {n_fail} fail")
        return n_fail


# ---------------------------------------------------------------------------
# Structural: the network is complete and self-consistent
# ---------------------------------------------------------------------------
def structural(c: Checks) -> None:
    print("\nStructural invariants")

    hubs = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg")
    edges = pd.read_csv(C.MESO_DIR / "meso_edges.csv")
    ifaces = pd.read_csv(C.MESO_DIR / "meso_ba_interfaces.csv")

    # 1. every node reachable
    g = nx.Graph()
    g.add_nodes_from(hubs["hub_id"])
    g.add_edges_from(zip(edges["source"], edges["target"]))
    comps = sorted(nx.connected_components(g), key=len, reverse=True)
    peak = dict(zip(hubs["hub_id"], hubs["total_peak_W"]))
    in_main = sum(peak.get(n, 0.0) for n in comps[0]) / max(sum(peak.values()), 1)
    c.add("network is connected",
          len(comps) == 1,
          f"{len(comps)} component(s), {100 * in_main:.1f}% of peak in the largest")

    # 2. every node can import its own peak
    rat = pd.read_csv(C.MESO_DIR / "substation_ratings.csv")
    short = rat[rat["rating_W"] < rat["total_peak_W"]]
    c.add("transformer rating covers assigned peak",
          len(short) == 0,
          f"{len(short):,} nodes rated below their assigned peak "
          f"({short['total_peak_W'].sum() / 1e9:.2f} GW)",
          warn_only=True)

    # 3. every BA can import its own peak
    ba = pd.read_csv(C.MESO_DIR / "substation_ba.csv")
    ba["substation_id"] = ba["substation_id"].astype(str)
    nodes = pd.read_csv(C.MESO_DIR / "meso_nodes.csv")
    nodes["substation_id"] = nodes["substation_id"].astype(str)
    nodes = nodes.drop(columns=["parent_ba"], errors="ignore").merge(
        ba[["substation_id", "parent_ba"]], on="substation_id", how="left")
    cap = ifaces.groupby("parent_ba")["interface_capacity_W"].sum()
    pk = nodes.groupby("parent_ba")["total_peak_W"].sum()
    ratio = (cap / pk).dropna()
    c.add("every BA interface covers BA peak",
          bool((ratio >= 0.999).all()),
          "min ratio %.2f (%s)" % (ratio.min(), ratio.idxmin()))

    # 4. interfaces are not so large they bypass the network
    c.add("BA interfaces are not a bulk bypass",
          bool((ratio <= 3.0).all()),
          "max ratio %.2f (%s)" % (ratio.max(), ratio.idxmax()),
          warn_only=True)

    # 5. demand allocation conserves
    fm = C.DATA_DIR / "mapping" / "taz_to_substation_feeder.csv"
    if fm.is_file():
        w = pd.read_csv(fm).groupby("TAZ")[["w_home", "w_work"]].sum().round(6)
        bad = int(((w["w_home"] != 1.0) | (w["w_work"] != 1.0)).sum())
        c.add("TAZ weights sum to one", bad == 0,
              f"{bad:,} of {len(w):,} TAZs do not sum to 1")

    # 6. no corridor is implausibly long for its voltage
    xy = {r.hub_id: (r.geometry.x, r.geometry.y) for r in hubs.itertuples()}
    ln = np.array([
        np.hypot(*(np.array(xy[s]) - np.array(xy[t]))) / 1000
        if s in xy and t in xy else np.nan
        for s, t in zip(edges["source"], edges["target"])
    ])
    kv = pd.to_numeric(edges["rated_kv"], errors="coerce").to_numpy()
    too_long = int(np.sum((ln > 150) & (kv < 200)))
    c.add("corridor length plausible for voltage", too_long == 0,
          f"{too_long:,} corridors over 150 km below 200 kV", warn_only=True)

    # 7. provenance is recorded and the bulk tier is evidence-backed
    if "provenance" in edges.columns:
        hv = edges[pd.to_numeric(edges["rated_kv"], errors="coerce") >= 100]
        backed = hv["provenance"].isin(["asserted", "confirmed"]).mean() if len(hv) else 0
        c.add("bulk corridors backed by a published source",
              backed >= 0.5, f"{100 * backed:.0f}% of >=100 kV corridors asserted/confirmed",
              warn_only=True)

    # 8. the nested graph carries the transformer limits
    ng = C.MESO_DIR / "wecc_ca_nested_graph.json"
    if ng.is_file():
        with open(ng, encoding="utf-8") as fh:
            graph = json.load(fh)
        subs = [n for n in graph["nodes"] if str(n["id"]).startswith("SUB_")]
        with_tx = sum(1 for n in subs if n.get("transformer_capacity"))
        c.add("transformer limit on every substation node",
              with_tx == len(subs), f"{with_tx:,}/{len(subs):,} substation nodes")
        ca = set(C.CALIFORNIA_REGIONS)
        internal = [e for e in graph["edges"]
                    if e["source"] in ca and e["target"] in ca]
        c.add("no CA-internal BA-to-BA bypass", len(internal) == 0,
              f"{len(internal)} internal BA edges remain")

    # 9. how much of the model rests on interpolated input
    synth = int((edges.get("provenance") == "synthetic_feed").sum()) if "provenance" in edges else 0
    derived = int((rat["rating_source"] == "derived").sum())
    print(f"\n  interpolated input in play: {synth:,} synthetic feeds, "
          f"{derived:,}/{len(rat):,} derived transformer ratings")


# ---------------------------------------------------------------------------
# Behavioural: the mechanism responds the way congestion must
# ---------------------------------------------------------------------------
BEHAVIOURAL = """
Behavioural invariants (require solves; run with --solve)

  B1  energy balance closes at every node and hour
  B2  S0 shortfall is ~zero
        A no-EV baseline that cannot serve itself is structural scarcity,
        not physics. This is the single most diagnostic check.
  B3  P_cong > 0, i.e. relaxed delivery (S3) emits less than constrained (S1)
  B4  P_cong -> 0 as interface and corridor capacity are scaled up
        The falsification test. If relaxing every constraint does not drive
        the penalty to zero, P_cong is measuring something other than
        congestion and no amount of better data will fix it.
  B5  generation rises monotonically with EV load
  B6  BESS sited at binding nodes reduces P_cong; BESS sited at random nodes
        does not. This separates temporal storage value from congestion
        relief, which is the paper's central distinction.
"""


def behavioural(c: Checks, horizon: str) -> None:
    print(BEHAVIOURAL)
    print(f"  (not yet wired to the solver; horizon would be '{horizon}')")
    print("  Run 10_01_run_scenarios_S0_S3.py for S0/S1/S3, then re-run with the")
    print("  capacity-scaled variant for B4 and the random-BESS control for B6.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solve", action="store_true")
    ap.add_argument("--horizon", default="june")
    args = ap.parse_args()

    c = Checks()
    structural(c)
    if args.solve:
        behavioural(c, args.horizon)
    raise SystemExit(1 if c.report() else 0)


if __name__ == "__main__":
    main()
