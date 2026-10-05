"""
10_11_emission_accounting.py

Emission accounting after the solve: several sets of emission factors, four ways
of comparing scenarios that serve different amounts of load, and the hourly
consequential generation behind each comparison.

CO2 is not in the objective. It is computed from what was dispatched, so the
factors can be changed and the comparison redone without re-solving. 10_01 saves
what that needs: per-asset energy, heat rate and fuel burned, the load actually
served, and hourly generation by fuel.

Emission-factor sets
--------------------
  egrid2023_plant        each plant's own CO2 output rate from eGRID 2023, read
                         from the raw file by ORIS code; coal, gas and oil at a
                         plant with no usable rate use heat rate x carbon
                         content; fuels that burn nothing are zero. The
                         accounting 10_01 writes. PRIMARY.
  egrid2023_checked      as primary, but a coal, gas or oil unit whose plant rate
                         is outside 0.5 to 1.5 times what its own heat rate
                         implies takes the heat-rate value. A plant rate is a
                         plant average, so a peaker beside a combined cycle, or
                         gas units at a site that also reports solar, carry a
                         rate their heat rate does not support.
  unit_heat_rate         coal, gas and oil all from unit heat rates
  eia_fuel_average_2023  flat per fuel: coal 1,048, gas 435, oil 1,116 kg/MWh
  published              the values on the assets in WEC_modified.json. Wrong
                         for every plant at or above 1,000 lb/MWh, because the
                         build mis-read thousands separators in the 2023 file;
                         kept so earlier results stay traceable.

Coefficients are EIA's "Carbon Dioxide Emissions Coefficients" (released
2024-09-18); the fuel averages are EIA's 2023 pounds of CO2 per kWh.

Four comparisons
----------------
An intervention X (storage, corridor relief) is compared with S1. X usually
serves load that S1 sheds, and serving load emits, so the raw difference counts
newly served load against X. All four are kept because they answer different
questions. Positive means X emits less.

  consequential   E(S1) - E(X). What the atmosphere sees, with no adjustment
                  for X serving more load.
  average         [E(S1)/D(S1) - E(X)/D(X)] x D(X). Compares CO2 per MWh
                  served, i.e. credits the extra load at the system average.
  marginal        E(S1) + r x [D(X) - D(S1)] - E(X). Credits the extra load at
                  r, the rate at which the model itself serves added load:
                  r = [E(S1) - E(S0)] / [D(S1) - D(S0)], the EV increment.
  facts           no netting: the extra load served, in GWh, and the emission
                  rate on it, [E(X) - E(S1)] / [D(X) - D(S1)], in kg/MWh.

D is load served: generation net of storage, less wastage and line losses.

Writes, under the results directory for the tag:
  emission_accounting.csv               one row per factor set and scenario
  emission_accounting_range.csv         min / primary / max across factor sets
  consequential_generation_hourly.csv.gz   hourly generation by fuel, X minus S1
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

LB_PER_KWH_2023 = {"coal": 2.31, "natural gas": 0.96, "oil": 2.46}
PRIMARY = "egrid2023_plant"
CHECK_BAND = (0.5, 1.5)     # plant rate over heat-rate-implied rate
SETS = [PRIMARY, "egrid2023_checked", "unit_heat_rate", "eia_fuel_average_2023", "published"]


_ORIS_BY_HANDLE: dict[str, str] | None = None
# Set by main() to the fleet the results were run on.
_FLEET_JSON: Path | None = None


def _oris_by_handle() -> dict[str, str]:
    """Asset handle -> ORIS code, from the model's own input file."""
    global _ORIS_BY_HANDLE
    if _ORIS_BY_HANDLE is None:
        _ORIS_BY_HANDLE = {}
        with open(_FLEET_JSON or C.resolve_wec_json(), encoding="utf-8") as fh:
            for node in json.load(fh)["nodes"]:
                for handle, asset in (node.get("assets") or {}).items():
                    key = C.oris_key(asset.get("oris_code"))
                    if key is not None:
                        _ORIS_BY_HANDLE.setdefault(handle, key)
    return _ORIS_BY_HANDLE


def _with_fallback(g: pd.DataFrame, ef: pd.Series, known: pd.Series) -> pd.Series:
    """Rows with no usable factor take the median of their own fuel.

    ``known`` marks rows whose value is a reported number even when it is zero.
    """
    ef = pd.to_numeric(ef, errors="coerce")
    have = (ef.fillna(0.0) > 0) | (known & ef.notna())
    pos = ef.fillna(0.0) > 0
    med = ef[pos].groupby(g.loc[pos, "fuel"]).median()
    return ef.where(have, g["fuel"].map(med)).fillna(0.0)


def factor_sets(g: pd.DataFrame) -> dict[str, pd.Series]:
    # A table written before the plant codes were saved gets them from the input
    # file, by the handle each asset name starts with.
    if "oris_code" not in g.columns:
        lookup = _oris_by_handle()
        g["oris_code"] = g["asset"].map(lambda a: lookup.get(str(a).split("__")[0]))
    hr = pd.to_numeric(g["heat_rate_btu_per_kWh"], errors="coerce")
    published = pd.to_numeric(g["co2_kg_per_MWh_published"], errors="coerce")
    fuel = g["fuel"].astype(str).str.lower()

    rule = [C.emission_factor(f, o, h, pb) for f, o, h, pb in zip(fuel, g["oris_code"], hr, published)]
    value = pd.Series([r[0] for r in rule], index=g.index)
    origin = pd.Series([r[1] for r in rule], index=g.index)
    known = origin.isin(C.KNOWN_FACTOR_SOURCES)
    primary = _with_fallback(g, value, known)

    coef = fuel.map(C.CO2_KG_PER_MMBTU)
    from_hr = (hr / 1000.0 * coef).where((hr > 0) & coef.notna())

    ratio = primary / from_hr
    off = (origin == "egrid2023_plant") & from_hr.notna() & ((ratio < CHECK_BAND[0]) | (ratio > CHECK_BAND[1]))
    checked = primary.where(~off, from_hr)

    heat = primary.where(from_hr.isna(), from_hr)

    flat = primary.copy()
    for f, lb in LB_PER_KWH_2023.items():
        flat[fuel == f] = lb * C.KG_PER_LB * 1000.0

    non_emitting = fuel.isin(C.NON_EMITTING_FUELS)
    pub = _with_fallback(g, published.where(~non_emitting, 0.0), non_emitting)

    g["_origin"] = origin
    g["_off_band"] = off
    return {PRIMARY: primary, "egrid2023_checked": checked, "unit_heat_rate": heat,
            "eia_fuel_average_2023": flat, "published": pub}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="four_week_WECC_SCE+WEC_CALN+WEC_SDGE")
    ap.add_argument("--reference", default="S1", help="Scenario every intervention is compared with.")
    ap.add_argument("--baseline", default="S0", help="Scenario without the added load; sets the marginal rate.")
    args = ap.parse_args()

    root = C.ASTR_RESULTS_DIR / args.tag
    global _FLEET_JSON
    _FLEET_JSON = C.results_fleet_json(root / args.reference)
    scen = sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.endswith(".stale")
                  and (d / "generation_by_asset.csv").is_file() and (d / "balance.json").is_file())
    if args.reference not in scen or args.baseline not in scen:
        raise SystemExit(f"Need {args.reference} and {args.baseline} solved by the current 10_01 "
                         f"(with balance.json) under {root}; found {scen}.")

    rows, fuel_rows = [], []
    for s in scen:
        g = pd.read_csv(root / s / "generation_by_asset.csv")
        if "co2_kg_per_MWh_published" not in g.columns or "heat_rate_btu_per_kWh" not in g.columns:
            raise SystemExit(f"{s}/generation_by_asset.csv predates the saved heat rates; re-run {s}.")
        bal = json.load(open(root / s / "balance.json", encoding="utf-8"))
        sets = factor_sets(g)
        if s == args.reference:
            fos = g["fuel"].isin(C.PLANT_RATE_FALLBACK_FUELS) & (g["energy_MWh"] > 0)
            src_gwh = (g.loc[fos, "energy_MWh"].groupby(g.loc[fos, "_origin"]).sum() / 1e3).round(0).to_dict()
            off_gwh = float(g.loc[fos & g["_off_band"], "energy_MWh"].sum()) / 1e3
            coverage = (f"fossil energy in {s} by source of its factor, GWh: {src_gwh}; "
                        f"{off_gwh:,.0f} GWh on units whose plant rate is outside "
                        f"{CHECK_BAND[0]} to {CHECK_BAND[1]} x their heat-rate value")
        for name, ef in sets.items():
            kg = g["energy_MWh"] * ef
            rows.append({"ef_set": name, "scenario": s, "co2_t": float(kg.sum()) / 1e3,
                         "served_GWh": bal["served_MWh"] / 1e3, "shortfall_GWh": bal["shortfall_MWh"] / 1e3})
            by = kg.groupby(g["fuel"]).sum() / 1e3
            en = g["energy_MWh"].groupby(g["fuel"]).sum() / 1e3
            for fuel in by.index:
                if by[fuel] != 0:
                    fuel_rows.append({"ef_set": name, "scenario": s, "fuel": fuel, "energy_GWh": float(en[fuel]),
                                      "co2_t": float(by[fuel]), "kg_per_MWh": float(by[fuel] / en[fuel]) if en[fuel] else np.nan})
    A = pd.DataFrame(rows)

    out = []
    for name, a in A.groupby("ef_set", sort=False):
        a = a.set_index("scenario")
        e, d = a["co2_t"], a["served_GWh"] * 1e3            # tonnes, MWh
        r = (e[args.reference] - e[args.baseline]) / (d[args.reference] - d[args.baseline])   # t per MWh
        for s in a.index:
            rec = {"ef_set": name, "scenario": s, "co2_Mt": e[s] / 1e6, "served_GWh": d[s] / 1e3,
                   "shortfall_GWh": a.loc[s, "shortfall_GWh"], "marginal_rate_reference_kg_per_MWh": r * 1e3}
            if s not in (args.reference, args.baseline):
                dd = d[s] - d[args.reference]
                rec.update({
                    "extra_load_GWh": dd / 1e3,
                    "consequential_t": e[args.reference] - e[s],
                    "average_t": (e[args.reference] / d[args.reference] - e[s] / d[s]) * d[s],
                    "marginal_t": e[args.reference] + r * dd - e[s],
                    # Undefined when the intervention serves no more load than the
                    # reference: the curtailment fleet changes emissions by tens of
                    # kt and load served by 0.03 GWh, and the ratio is noise.
                    "rate_on_extra_load_kg_per_MWh": (e[s] - e[args.reference]) / dd * 1e3 if abs(dd) > 1000.0 else np.nan,
                })
            out.append(rec)
    R = pd.DataFrame(out)
    R.to_csv(root / "emission_accounting.csv", index=False)
    pd.DataFrame(fuel_rows).to_csv(root / "emission_accounting_by_fuel.csv", index=False)

    metrics = ["consequential_t", "average_t", "marginal_t", "rate_on_extra_load_kg_per_MWh"]
    X = R[R["extra_load_GWh"].notna()]
    rng = []
    for s, x in X.groupby("scenario", sort=False):
        p = x[x["ef_set"] == PRIMARY].iloc[0]
        alt = x[x["ef_set"] != "published"]
        rec = {"scenario": s, "extra_load_GWh": p["extra_load_GWh"]}
        for m in metrics:
            rec[f"{m}_primary"] = p[m]
            rec[f"{m}_min"] = alt[m].min()
            rec[f"{m}_max"] = alt[m].max()
        rng.append(rec)
    RNG = pd.DataFrame(rng)
    RNG.to_csv(root / "emission_accounting_range.csv", index=False)

    # Hourly consequential generation: what each intervention changes, by fuel.
    ref = pd.read_csv(root / args.reference / "generation_hourly_by_fuel.csv", index_col=0)
    frames = []
    for s in scen:
        if s == args.reference:
            continue
        cur = pd.read_csv(root / s / "generation_hourly_by_fuel.csv", index_col=0)
        # the baseline is reported the other way round: reference minus baseline
        # is the generation that serves the added load
        delta = (ref.sub(cur, fill_value=0.0) if s == args.baseline else cur.sub(ref, fill_value=0.0))
        long = delta.stack().rename("delta_MW").reset_index()
        long.columns = ["hour", "fuel", "delta_MW"]
        long.insert(0, "comparison", f"{args.reference} minus {s}" if s == args.baseline else f"{s} minus {args.reference}")
        frames.append(long[long["delta_MW"].abs() > 1e-6])
    pd.concat(frames, ignore_index=True).to_csv(
        root / "consequential_generation_hourly.csv.gz", index=False, float_format="%.4f", compression="gzip")

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    prim = R[R["ef_set"] == PRIMARY]
    print(f"emission accounting for {args.tag}, reference {args.reference}, baseline {args.baseline}\n")
    print("CO2 by factor set (Mt):")
    print(A.pivot(index="scenario", columns="ef_set", values="co2_t").div(1e6).round(4)
          [SETS].to_string())
    print()
    print(coverage)
    byf = pd.DataFrame(fuel_rows)
    ref_f = byf[(byf["scenario"] == args.reference) & byf["fuel"].isin(["coal", "natural gas", "oil"])]
    print(f"\nkg/MWh as dispatched in {args.reference}:")
    print(ref_f.pivot(index="fuel", columns="ef_set", values="kg_per_MWh")[SETS].round(0).to_string())
    print(f"\nrate at which the model serves added load ({args.baseline} -> {args.reference}), kg/MWh:")
    print(R.drop_duplicates("ef_set").set_index("ef_set")["marginal_rate_reference_kg_per_MWh"].round(0).to_string())
    print(f"\nprimary factor set ({PRIMARY}); positive = the intervention emits less than {args.reference}:")
    show = prim[prim["extra_load_GWh"].notna()].set_index("scenario")
    print(show[["shortfall_GWh", "extra_load_GWh", "consequential_t", "average_t", "marginal_t",
                "rate_on_extra_load_kg_per_MWh"]].round({"shortfall_GWh": 2, "extra_load_GWh": 2, "consequential_t": 0,
                                                        "average_t": 0, "marginal_t": 0,
                                                        "rate_on_extra_load_kg_per_MWh": 0}).to_string())
    print("\nrange across factor sets (published excluded), tonnes:")
    for _, r_ in RNG.iterrows():
        print(f"  {r_['scenario']:18} consequential {r_['consequential_t_min']:>10,.0f} to {r_['consequential_t_max']:>10,.0f}"
              f"   average {r_['average_t_min']:>10,.0f} to {r_['average_t_max']:>10,.0f}"
              f"   marginal {r_['marginal_t_min']:>10,.0f} to {r_['marginal_t_max']:>10,.0f}")
    print(f"\nwrote emission_accounting.csv, emission_accounting_by_fuel.csv, emission_accounting_range.csv, "
          f"consequential_generation_hourly.csv.gz under {root}")


if __name__ == "__main__":
    main()
