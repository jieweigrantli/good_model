"""
10_12_dispatch_check.py

Does the model run plants the way they actually ran?

Compares the fossil generation each variant of the run dispatches, by state and
fuel, with what eGRID 2023 reports those states' plants generated. The no-EV
scenario is the one compared, since 2023 had little EV load, and its four
seasonal weeks are scaled to a year (x 8760 / 672).

It is a coarse check and is meant as one. The model's generator list is NEEDS
v6.21, its demand is not 2023's, and four weeks are not a year. A state whose gas
fleet runs at a third or at three times its actual output is still a finding.

A variant is a results directory: the reference tag, or the tag with a suffix
such as ".rps_hard" or ".rps_load".

Writes dispatch_check.csv beside the reference results.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

WEST = ["CA", "AZ", "NV", "NM", "UT", "CO", "WY", "MT", "ID", "OR", "WA"]
EGRID_GEN = {"coal": "Plant annual coal net generation (MWh)",
             "natural gas": "Plant annual gas net generation (MWh)"}
CA_BAS = ("WECC_IID", "WECC_SCE", "WEC_BANC", "WEC_CALN", "WEC_LADW", "WEC_SDGE")


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")


def actual_2023() -> pd.DataFrame:
    """TWh of coal and gas generation by state, from the eGRID 2023 plant file."""
    cols = ["Plant state abbreviation"] + list(EGRID_GEN.values())
    df = pd.read_csv(C.EGRID_PLANT_CSV, dtype=str, low_memory=False, usecols=cols)
    out = pd.DataFrame({fuel: _num(df[col]).groupby(df["Plant state abbreviation"]).sum() / 1e6
                        for fuel, col in EGRID_GEN.items()})
    return out.reindex(WEST)


_BA: dict[str, str] | None = None


def _parent_ba(node: str) -> str:
    global _BA
    if _BA is None:
        with open(C.CA_NETWORK_JSON, encoding="utf-8") as fh:
            _BA = {n["hub_id"]: n.get("parent_ba") for n in json.load(fh).get("nodes") or []}
    return _BA.get(node) or node


_ATTR: dict[str, dict] | None = None


def _attrs() -> dict[str, dict]:
    global _ATTR
    if _ATTR is None:
        _ATTR = {}
        with open(C.resolve_wec_json(), encoding="utf-8") as fh:
            for node in json.load(fh)["nodes"]:
                for handle, asset in (node.get("assets") or {}).items():
                    _ATTR.setdefault(handle, asset)
    return _ATTR


def modelled(run_dir: Path, scenario: str) -> tuple[pd.DataFrame, dict]:
    g = pd.read_csv(run_dir / scenario / "generation_by_asset.csv")
    attrs = _attrs()
    base = g["asset"].map(lambda a: attrs.get(str(a).split("__")[0]) or {})
    if "jurisdiction" not in g.columns:
        g["jurisdiction"] = base.map(lambda a: a.get("jurisdiction"))
    mult = g["asset"].map(lambda a: int(str(a).split("__x")[1]) if "__x" in str(a) else 1)
    g["MW"] = base.map(lambda a: abs(float(a.get("installed_capacity") or 0.0)) / 1e6) * mult
    with open(run_dir / scenario / "balance.json", encoding="utf-8") as fh:
        hours = 672.0
        bal = json.load(fh)
    scale = 8760.0 / hours
    fossil = g[g["fuel"].isin(EGRID_GEN)]
    twh = (fossil.groupby(["jurisdiction", "fuel"])["energy_MWh"].sum().unstack() * scale / 1e6).reindex(WEST)
    cf = (fossil.groupby(["jurisdiction", "fuel"])["energy_MWh"].sum()
          / (fossil.groupby(["jurisdiction", "fuel"])["MW"].sum() * hours)).unstack().reindex(WEST)
    extra = {"served_GWh": bal["served_MWh"] / 1e3}
    # California's own balance. Generation and dumped energy at nodes in its six
    # balancing areas are in every variant's files; its demand is only written by
    # the newer runs, so it is filled in by the caller for the others.
    ba = g["node"].map(_parent_ba)
    extra["ca_generation_MWh"] = float(g.loc[ba.isin(CA_BAS), "energy_MWh"].sum())
    for key, fname, col in (("ca_wastage_MWh", "wastage_by_node.csv", "wastage_MWh"),
                            ("ca_shortfall_MWh", "shortfall_by_node.csv", "shortfall_MWh")):
        f = run_dir / scenario / fname
        d = pd.read_csv(f) if f.is_file() else pd.DataFrame(columns=["node", col])
        extra[key] = float(d.loc[d["node"].map(_parent_ba).isin(CA_BAS), col].sum()) if len(d) else 0.0
    lb = run_dir / scenario / "load_by_ba.csv"
    if lb.is_file():
        d = pd.read_csv(lb).set_index("ba").reindex(CA_BAS)
        extra["ca_demand_MWh"] = float(d["demand_MWh"].sum())
    return twh, {"cf": cf, **extra}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="four_week_WECC_SCE+WEC_CALN+WEC_SDGE")
    ap.add_argument("--variants", nargs="*", default=[".rps_hard", "", ".rps_load"],
                    help="Suffixes on the tag; '' is the reference run.")
    ap.add_argument("--scenario", default="S0")
    args = ap.parse_args()

    act = actual_2023()
    labels = {".rps_hard": "hard RPS", "": "RPS $50", ".rps_load": "RPS on load"}
    runs = {}
    for suffix in args.variants:
        d = C.ASTR_RESULTS_DIR / f"{args.tag}{suffix}"
        if (d / args.scenario / "generation_by_asset.csv").is_file() and (d / args.scenario / "balance.json").is_file():
            runs[labels.get(suffix, suffix or "reference")] = modelled(d, args.scenario)
    if not runs:
        raise SystemExit("No variant has the scenario solved.")

    rows = []
    for fuel in EGRID_GEN:
        for st in WEST:
            rec = {"fuel": fuel, "state": st, "actual_2023_TWh": act.loc[st, fuel]}
            for name, (twh, extra) in runs.items():
                v = twh.loc[st, fuel] if fuel in twh.columns else np.nan
                rec[f"{name}_TWh"] = v
                rec[f"{name}_ratio"] = v / act.loc[st, fuel] if act.loc[st, fuel] > 0.5 else np.nan
                rec[f"{name}_CF"] = extra["cf"].loc[st, fuel] if fuel in extra["cf"].columns else np.nan
            rows.append(rec)
    out = pd.DataFrame(rows)
    out.to_csv(C.ASTR_RESULTS_DIR / args.tag / "dispatch_check.csv", index=False)

    pd.set_option("display.width", 220)
    names = list(runs)
    print(f"fossil generation in {args.scenario}, scaled to a year, against eGRID 2023 (TWh)\n")
    for fuel in EGRID_GEN:
        t = out[out["fuel"] == fuel].set_index("state")
        show = t[["actual_2023_TWh"] + [f"{n}_TWh" for n in names]].copy()
        show.columns = ["actual 2023"] + names
        show.loc["West"] = show.sum()
        print(fuel)
        print(show.round(1).to_string())
        r = show.loc["West"]
        print("   modelled / actual, West: " + ", ".join(f"{n} {r[n] / r['actual 2023']:.2f}" for n in names))
        ca = show.loc["CA"]
        if ca["actual 2023"] > 0.5:
            print("   modelled / actual, California: " + ", ".join(f"{n} {ca[n] / ca['actual 2023']:.2f}" for n in names))
        print()
    # Demand is the same in every variant of a scenario, so one variant's figure
    # serves the rest.
    demand = next((e["ca_demand_MWh"] for _, e in runs.values() if "ca_demand_MWh" in e), None)
    if demand is not None:
        print("California net imports as a share of the load it serves: " + ", ".join(
            f"{n} {(1 - (e['ca_generation_MWh'] - e['ca_wastage_MWh']) / (demand - e['ca_shortfall_MWh'])) * 100:.0f}%"
            for n, (_, e) in runs.items()))
    print("\ncapacity factor of California gas: " + ", ".join(
        f"{n} {runs[n][1]['cf'].loc['CA', 'natural gas']:.2f}" for n in names))
    print("capacity factor of Arizona coal:   " + ", ".join(
        f"{n} {runs[n][1]['cf'].loc['AZ', 'coal']:.2f}" for n in names))


if __name__ == "__main__":
    main()
