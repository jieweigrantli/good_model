"""
08_19_build_fleet_from_egrid2023.py

Bring the generator fleet to what existed in 2023, using eGRID 2023 as the
reference, and write it as Examples/WEC_egrid2023.json.

Why
---
WEC_modified.json is not a fleet that existed. Its located units are the
"existing" units of EPA's IPM v6.17 output, a vintage of about 2018, and on top
of them sit 78 assets with no plant code and no location that are the plants
that model *projected* would be built by 2023: 8,423 MW of wind, 7,795 MW of it
in California, which in fact added about 400 MW, 9,169 MW of solar, and so on.
All wind then runs on IPM's best resource class at a 50% capacity factor, where
western wind actually ran at 20 to 39%. California came out with 58 TWh of wind a
year against 14, and as a net exporter where it imports at least 17% of what it
consumes.

What this does
--------------
eGRID 2023's generator sheet lists every generator with its fuel, prime mover,
nameplate and status. Plants are compared with the model fuel by fuel:

  * a model asset with no plant code is a projected plant and is removed. The one
    exception is IPM's import, which is a boundary flow and not a projection;
  * where eGRID has no operating generator of a fuel at a plant, the model's
    units of that fuel are removed -- retired, or never built;
  * where the model has none of a fuel at a plant and eGRID does, it is added;
  * where both have it, the model's units are kept as they are, since they carry
    unit-level heat rates and costs. If eGRID shows more than 1.5 times the
    model's capacity, the difference is added as one unit.

Gas and oil are compared together, as are biomass and waste, and hydro and pumped
hydro, because the two sources often label the same unit differently and treating
that as a retirement plus a new build would churn 3 GW for nothing.

What eGRID does not give, and how each gap is filled
----------------------------------------------------
  capacity basis   eGRID is nameplate, the model is net. Added capacity is divided
                   by the nameplate-to-model ratio measured, per fuel, on the
                   plants both sources hold.
  operating cost   cost per unit of heat, from the model's own units of that fuel
                   in the region, times the new unit's heat rate; for fuels with
                   no heat rate, the regional median cost.
  heat rate        eGRID's plant nominal heat rate, else the regional median.
  model region     the plant's existing region if the model already holds it;
                   else its balancing-authority code where that names one region;
                   else the majority of the five nearest plants with the same code
                   whose region is known. The run prints how often that is right
                   on plants with a known answer.
  storage          eGRID gives battery power only. Duration is whatever
                   good.migrate assumes for an existing battery (4 hours).
  wind and solar   output is set to what each region's plants actually produced
                   in 2023. The hourly shape is the IPM resource class whose mean
                   is nearest that figure, scaled to match it. Scaling the class-1
                   shape down instead would stop wind ever reaching nameplate.

Writes Examples/WEC_egrid2023.json and data/meso/fleet_changes_egrid2023.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

RAW = C.REPO_ROOT / "good_datasets-main" / "Data" / "US" / "Raw"
XLSX = RAW / "egrid2023_data_rev2.xlsx"
SRC = C.REPO_ROOT / "Examples" / "WEC_modified.json"
OUT = C.REPO_ROOT / "Examples" / "WEC_egrid2023.json"
LOG = C.MESO_DIR / "fleet_changes_egrid2023.csv"

# OP operating, SB standby, OA out of service but expected back. OS (not expected
# back) and RE (retired) are not counted as existing.
OPERATING = ("OP", "SB", "OA")
EXCESS_RATIO = 1.5
MIN_ADD_MW = 1.0

COAL = {"BIT", "SUB", "LIG", "RC", "WC", "SGC", "ANT"}
OIL = {"DFO", "RFO", "JF", "KER", "PC", "WO"}
BIO = {"WDS", "BLQ", "AB", "OBS", "OBL", "WDL", "SLW"}
WASTE = {"MSW", "LFG", "OBG", "TDF"}
# fuels compared together when asking whether a plant still has them
GROUP = {"natural gas": "gas/oil", "oil": "gas/oil", "biomass": "bio", "waste": "bio",
         "non-fossil": "bio", "hydro": "hydro", "pump hydro": "hydro"}
VRE = {"wind": "WT", "solar": "PV"}

# The model regions each California balancing authority runs. IPM drew its regions
# on a map, so it files 81 California ISO plants in Los Angeles and Imperial County
# under LADWP and IID. A plant added here goes to a region its own balancing
# authority runs: El Segundo delivers to Edison's grid though LADWP's plants
# surround it, and the Mount Signal solar farms deliver to San Diego's.
BA_REGIONS = {
    "CISO": ("WECC_SCE", "WEC_CALN", "WEC_SDGE"),
    "LDWP": ("WEC_LADW",),
    "IID": ("WECC_IID",),
    "BANC": ("WEC_BANC",),
}


def egrid_fuel(fuel: str, prime_mover: str) -> str | None:
    if fuel in COAL:
        return "coal"
    if fuel == "NG":
        return "natural gas"
    if fuel in OIL:
        return "oil"
    if fuel == "NUC":
        return "nuclear"
    if fuel == "WAT":
        return "pump hydro" if prime_mover == "PS" else "hydro"
    if fuel == "SUN":
        return "solar"
    if fuel == "WND":
        return "wind"
    if fuel == "GEO":
        return "geothermal"
    if fuel == "MWH":
        return "battery"
    if fuel in BIO:
        return "biomass"
    if fuel in WASTE:
        return "waste"
    return None


def group_of(fuel: str) -> str:
    return GROUP.get(fuel, fuel)


def model_fuel(asset: dict) -> str:
    f = str(asset.get("fuel")).lower()
    if asset.get("_class") == "Store" and "pump" not in f:
        return "battery"
    return f


def load_egrid() -> tuple[pd.DataFrame, pd.DataFrame]:
    gen_csv, plnt_csv = RAW / "egrid2023_gen.csv", RAW / "egrid2023_plnt.csv"
    if not (gen_csv.is_file() and plnt_csv.is_file()):
        C.require_file(XLSX, hint="Download eGRID2023 (xlsx) from epa.gov/egrid/detailed-data")
        pd.read_excel(XLSX, sheet_name="GEN23", header=1).to_csv(gen_csv, index=False)
        pd.read_excel(XLSX, sheet_name="PLNT23", header=1).to_csv(plnt_csv, index=False)
    gen = pd.read_csv(gen_csv, low_memory=False)
    plnt = pd.read_csv(plnt_csv, low_memory=False)
    gen["oris"] = gen["ORISPL"].map(C.oris_key)
    plnt["oris"] = plnt["ORISPL"].map(C.oris_key)
    gen["fuel"] = [egrid_fuel(f, p) for f, p in zip(gen["FUELG1"].astype(str), gen["PRMVR"].astype(str))]
    return gen, plnt.drop_duplicates("oris").set_index("oris")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-vre-profiles", action="store_true",
                    help="Leave the wind and solar profiles as they are instead of setting "
                         "them to 2023 output.")
    args = ap.parse_args()

    with open(SRC, encoding="utf-8") as fh:
        graph = json.load(fh)
    nodes = {n["id"]: n for n in graph["nodes"]}
    gen, plnt = load_egrid()
    west = plnt[plnt["NERC"] == "WECC"]
    # Existence is checked against every plant in the file: the model's western
    # regions reach into Texas, South Dakota and Nebraska, whose plants eGRID
    # files under other reliability regions. Only western plants are added.
    op_all = gen[gen["GENSTAT"].isin(OPERATING) & gen["fuel"].notna()].copy()
    op_all["group"] = op_all["fuel"].map(group_of)
    op = op_all[op_all["oris"].isin(west.index)].copy()

    # ---- what the model holds -------------------------------------------------
    inv = []
    for rid, node in nodes.items():
        for handle, a in (node.get("assets") or {}).items():
            if str(handle).startswith("optional_") or a.get("_class") not in ("Producer", "Load", "Store"):
                continue
            if a.get("_class") == "Load" and str(a.get("type")).lower() == "load":
                continue
            f = model_fuel(a)
            inv.append({"region": rid, "handle": handle, "oris": C.oris_key(a.get("oris_code")) or "0",
                        "fuel": f, "group": group_of(f),
                        "MW": abs(float(a.get("installed_capacity") or 0.0)) / 1e6})
    inv = pd.DataFrame(inv)
    before = inv.groupby("fuel")["MW"].sum()

    E = op_all.groupby(["oris", "group"])["NAMEPCAP"].sum()
    M = inv[inv["oris"] != "0"].groupby(["oris", "group"])["MW"].sum()
    pair = pd.concat([M.rename("model"), E.rename("egrid")], axis=1).fillna(0.0)

    # nameplate over model capacity, per group, on plants both sources hold
    both = pair[(pair["model"] > 0) & (pair["egrid"] > 0)].copy()
    both["r"] = both["egrid"] / both["model"]
    k = (both[(both["r"] > 0.7) & (both["r"] < EXCESS_RATIO)].groupby(level="group")["r"].median()).to_dict()
    for g in ("solar", "wind", "battery"):
        k[g] = 1.0

    changes = []

    # ---- removals -------------------------------------------------------------
    drop = set()
    for r in inv.itertuples():
        if r.oris == "0":
            if r.fuel == "import":
                continue
            drop.add((r.region, r.handle))
            changes.append({"action": "removed, projected plant", "oris": "", "region": r.region,
                            "fuel": r.fuel, "MW": -r.MW})
        elif E.get((r.oris, r.group), 0.0) <= 0.0:
            drop.add((r.region, r.handle))
            changes.append({"action": "removed, not operating in 2023", "oris": r.oris, "region": r.region,
                            "fuel": r.fuel, "MW": -r.MW})
    for rid, handle in drop:
        del nodes[rid]["assets"][handle]

    # ---- where each plant goes ------------------------------------------------
    kept = inv[~inv.apply(lambda r: (r.region, r.handle) in drop, axis=1) & (inv["oris"] != "0")]
    region_of = kept.groupby("oris")["region"].agg(lambda s: s.value_counts().index[0]).to_dict()
    known = west.loc[west.index.intersection(list(region_of))].copy()
    known["region"] = [region_of[o] for o in known.index]
    by_ba = known.groupby(["BACODE", "PSTATABB"])["region"].agg(lambda s: s.value_counts().index[0])
    one_region = known.groupby(["BACODE", "PSTATABB"])["region"].nunique() == 1
    from scipy.spatial import cKDTree

    def xy(df):
        lat0 = np.radians(known["LAT"].mean())
        return np.column_stack([np.radians(df["LON"]) * np.cos(lat0) * 6371.0, np.radians(df["LAT"]) * 6371.0])

    # Neighbours are taken from plants with the same balancing-authority code, and
    # for the California authorities only from those in a region that authority
    # runs (BA_REGIONS).
    consistent = np.array([r in BA_REGIONS.get(b, (r,)) for b, r in zip(known["BACODE"], known["region"])])
    trees = {"*": (cKDTree(xy(known)), known["region"].to_numpy())}
    for code, sub in known[consistent].groupby("BACODE"):
        trees[code] = (cKDTree(xy(sub)), sub["region"].to_numpy())

    def nearest_vote(p, exclude_self: bool = False) -> str:
        tree, regions = trees.get(p["BACODE"], trees["*"])
        n = min(5 + int(exclude_self), len(regions))
        _, idx = tree.query(xy(pd.DataFrame([p])), k=n)
        idx = np.atleast_1d(idx[0])
        if exclude_self and len(idx) > 1:
            idx = idx[1:]
        return Counter(regions[idx]).most_common(1)[0][0]

    def by_code(code: str, state: str) -> str | None:
        """The region a code names on its own, if it names one."""
        if len(BA_REGIONS.get(code, ())) == 1:
            return BA_REGIONS[code][0]
        return by_ba[(code, state)] if bool(one_region.get((code, state), False)) else None

    # How good the rule is, on plants where the answer is known. Plants IPM filed
    # outside their balancing authority's regions are left out of the test: the
    # rule is meant to disagree with the model about those.
    test = known[consistent]
    amb = test[[by_code(b, s) is None for b, s in zip(test["BACODE"], test["PSTATABB"])]]
    hit = np.array([nearest_vote(p, exclude_self=True) == p["region"] for _, p in amb.iterrows()])
    cap = pd.to_numeric(test["NAMEPCAP"], errors="coerce").fillna(0.0)
    right_mw = cap[~test.index.isin(amb.index)].sum() + cap.loc[amb.index][hit].sum()
    off = known[~consistent]
    placement_note = (f"region rule, tested leave-one-out on {len(test):,} plants with a known region: "
                      f"right for {(len(test) - len(amb) + hit.sum()) / len(test) * 100:.1f}% of plants, "
                      f"{right_mw / cap.sum() * 100:.1f}% of capacity "
                      f"({len(amb):,} plants needed the neighbour vote, {hit.mean() * 100:.1f}% of them right)\n"
                      f"left where the model has them, outside their balancing authority's regions: "
                      f"{len(off):,} plants, {pd.to_numeric(off['NAMEPCAP'], errors='coerce').sum():,.0f} MW nameplate "
                      f"{off.groupby(['BACODE', 'region']).size().to_dict()}")

    placed: dict[str, tuple[str, str]] = {}

    def place(oris: str) -> tuple[str, str]:
        if oris not in placed:
            placed[oris] = _place(oris)
        return placed[oris]

    def _place(oris: str) -> tuple[str, str]:
        if oris in region_of:
            return region_of[oris], "already in the model"
        p = west.loc[oris]
        named = by_code(p["BACODE"], p["PSTATABB"])
        if named is not None:
            return named, "balancing-authority code"
        return nearest_vote(p), "five nearest plants with the same code"

    # ---- templates and costs from the model's own units -----------------------
    template: dict[tuple[str, str], dict] = {}
    stats = []
    for rid, node in nodes.items():
        for handle in sorted(node.get("assets") or {}):
            a = node["assets"][handle]
            if str(handle).startswith("optional_") or a.get("_class") not in ("Producer", "Load", "Store"):
                continue
            if a.get("_class") == "Load" and str(a.get("type")).lower() == "load":
                continue
            f = model_fuel(a)
            template.setdefault((rid, f), a)
            template.setdefault(("*", f), a)
            hr = float(a.get("heat_rate") or 0.0)
            stats.append({"region": rid, "fuel": f, "MW": abs(float(a.get("installed_capacity") or 0.0)) / 1e6,
                          "op": float(a.get("operating_cost") or 0.0), "hr": hr})
    stats = pd.DataFrame(stats)

    def median(fuel: str, region: str, col: str, positive: bool = True) -> float:
        for sel in (stats[(stats["fuel"] == fuel) & (stats["region"] == region)], stats[stats["fuel"] == fuel]):
            v = sel[col]
            v = v[v > 0] if positive else v
            if len(v):
                return float(v.median())
        return 0.0

    def cost_per_heat(fuel: str, region: str) -> float:
        for sel in (stats[(stats["fuel"] == fuel) & (stats["region"] == region)], stats[stats["fuel"] == fuel]):
            s = sel[(sel["hr"] > 0) & (sel["op"] > 0)]
            if len(s):
                return float((s["op"] / s["hr"]).median())
        return 0.0

    egrid_rate = C.egrid_plant_co2_kg_per_mwh()

    def add(oris: str, fuel: str, nameplate_mw: float, why: str) -> None:
        mw = nameplate_mw / k.get(group_of(fuel), 1.0)
        if mw < MIN_ADD_MW:
            return
        region, how = place(oris)
        base = template.get((region, fuel)) or template.get(("*", fuel))
        if base is None:
            return
        p = west.loc[oris]
        a = deepcopy(base)
        a.update({
            "oris_code": int(oris), "egrid_id": f"{oris}_E23_{fuel.replace(' ', '_')}",
            "region": region, "jurisdiction": p["PSTATABB"], "utility": p.get("OPRNAME"),
            "x": float(p["LON"]), "y": float(p["LAT"]),
            "installed_capacity": mw * 1e6, "extensible": False, "capex_capacity": 0, "capex_cost": 0,
        })
        if isinstance(a.get("profile"), str) and ":" in a["profile"]:
            a["profile"] = f"{region}:{a['profile'].split(':', 1)[1]}"
        if fuel in ("coal", "natural gas", "oil", "biomass", "waste"):
            hr = pd.to_numeric(p.get("PLHTRT"), errors="coerce")
            hr = float(hr) / 3412.14 if pd.notna(hr) and hr > 0 else median(fuel, region, "hr")
            a["heat_rate"] = hr
            cph = cost_per_heat(fuel, region)
            a["operating_cost"] = cph * hr if cph > 0 and hr > 0 else median(fuel, region, "op")
            rate = egrid_rate.get(oris)
            if rate is not None and fuel in C.PLANT_RATE_FALLBACK_FUELS:
                a["co2"] = rate / 3.6e9
        elif fuel not in ("solar", "wind", "battery"):
            a["operating_cost"] = median(fuel, region, "op", positive=False)
        nodes[region]["assets"][f"installed_e23_{oris}_{fuel.replace(' ', '_')}"] = a
        changes.append({"action": why, "oris": oris, "region": region, "fuel": fuel, "MW": mw,
                        "plant": p["PNAME"], "state": p["PSTATABB"], "region_from": how,
                        "nameplate_MW": nameplate_mw})

    # ---- additions --------------------------------------------------------------
    by_fuel = op.groupby(["oris", "group", "fuel"])["NAMEPCAP"].sum()
    for (oris, group), row in pair.iterrows():
        if row["egrid"] <= 0 or oris not in west.index:
            continue
        fuels = by_fuel.loc[(oris, group)]
        if row["model"] <= 0:
            for fuel, mw in fuels.items():
                add(oris, fuel, float(mw), "added, not in the model")
        elif row["egrid"] / row["model"] > EXCESS_RATIO:
            extra = row["egrid"] - row["model"] * k.get(group, 1.0)
            add(oris, fuels.idxmax(), float(extra), "added, more capacity at a plant the model holds")

    # ---- wind and solar output --------------------------------------------------
    vre_rows = []
    if not args.keep_vre_profiles:
        full_year = op[pd.to_numeric(op["GENYRONL"], errors="coerce") < 2023]
        for fuel, pm in VRE.items():
            g = full_year[(full_year["fuel"] == fuel) & (full_year["PRMVR"] == pm) & (full_year["NAMEPCAP"] > 0)].copy()
            g["region"] = [place(o)[0] for o in g["oris"]]
            west_cf = g["GENNTAN"].sum() / (g["NAMEPCAP"].sum() * 8760.0)
            cf = (g.groupby("region")["GENNTAN"].sum() / (g.groupby("region")["NAMEPCAP"].sum() * 8760.0)).to_dict()
            mw = g.groupby("region")["NAMEPCAP"].sum().to_dict()
            n_plants = g.groupby("region")["oris"].nunique().to_dict()
            state_cf = (g.groupby("PSTATABB")["GENNTAN"].sum()
                        / (g.groupby("PSTATABB")["NAMEPCAP"].sum() * 8760.0)).to_dict()
            # the state most of a region's plants (of any kind) are in
            region_state = (kept.assign(state=kept["oris"].map(plnt["PSTATABB"]))
                            .groupby("region")["state"].agg(lambda s: s.value_counts().index[0]).to_dict())
            for rid, node in nodes.items():
                profiles = node.get("profiles") or {}
                key = f"{rid}:{fuel}:"
                if key not in profiles:
                    continue
                # A region needs at least three plants and 100 MW of the fuel to set its
                # own figure. LADWP's wind is one 135 MW plant that ran at 5% in 2023;
                # one plant's bad year is not a region's resource. Thin regions take
                # their state's figure, and failing that the West's.
                own = mw.get(rid, 0.0) >= 100.0 and n_plants.get(rid, 0) >= 3
                target = cf[rid] if own else state_cf.get(region_state.get(rid), west_cf)
                classes = {kk: np.asarray(v, dtype=float) for kk, v in profiles.items()
                           if kk.startswith(key) and kk != key and kk[len(key):].isdigit()}
                old = np.asarray(profiles[key], dtype=float)
                pick, shape = min(classes.items(), key=lambda kv: abs(kv[1].mean() - target)) if classes else (key, old)
                new = shape.copy()
                for _ in range(20):                      # scaling can clip at 1, so repeat
                    m = new.mean()
                    if m <= 0 or abs(m - target) < 1e-6:
                        break
                    new = np.clip(new * target / m, 0.0, 1.0)
                profiles[f"{key}ipm_default"] = profiles[key]
                profiles[key] = new.tolist()
                vre_rows.append({"region": rid, "fuel": fuel, "was": old.mean(), "actual_2023": target,
                                 "from_region_data": own,
                                 "ipm_class": pick[len(key):], "class_mean": shape.mean(), "now": new.mean()})

    # ---- write ------------------------------------------------------------------
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(graph, fh)
    ch = pd.DataFrame(changes)
    C.ensure_dir(LOG.parent)
    ch.to_csv(LOG, index=False)

    after = defaultdict(float)
    for rid, node in nodes.items():
        for handle, a in (node.get("assets") or {}).items():
            if str(handle).startswith("optional_") or a.get("_class") not in ("Producer", "Load", "Store"):
                continue
            if a.get("_class") == "Load" and str(a.get("type")).lower() == "load":
                continue
            after[model_fuel(a)] += abs(float(a.get("installed_capacity") or 0.0)) / 1e6
    pd.set_option("display.width", 200)
    t = pd.DataFrame({
        "before": before,
        "projected removed": -ch[ch["action"] == "removed, projected plant"].groupby("fuel")["MW"].sum(),
        "not operating removed": -ch[ch["action"] == "removed, not operating in 2023"].groupby("fuel")["MW"].sum(),
        "added": ch[ch["action"].str.startswith("added")].groupby("fuel")["MW"].sum(),
        "after": pd.Series(after),
        "eGRID 2023 nameplate": op.groupby("fuel")["NAMEPCAP"].sum(),
    }).fillna(0.0).sort_values("after", ascending=False)
    t.loc["total"] = t.sum()
    print("fleet, MW (model capacities are net; eGRID is nameplate)")
    print(t.round(0).astype(int).to_string())
    print("\n" + placement_note)
    print("\nnameplate over model capacity used to convert additions:",
          {g: round(v, 2) for g, v in sorted(k.items())})
    a_ = ch[ch["action"].str.startswith("added")]
    print(f"\nadded: {len(a_):,} assets, {a_['MW'].sum():,.0f} MW; region assigned by: "
          f"{a_.groupby('region_from')['MW'].sum().round(0).to_dict()}")
    print("  by state:", a_.groupby("state")["MW"].sum().round(0).sort_values(ascending=False).head(11).to_dict())
    r_ = ch[ch["action"] == "removed, not operating in 2023"]
    print(f"removed as not operating in 2023: {len(r_):,} assets, {-r_['MW'].sum():,.0f} MW")
    if vre_rows:
        v = pd.DataFrame(vre_rows)
        print("\nwind and solar capacity factor by region:")
        print(v.pivot(index="region", columns="fuel", values=["was", "actual_2023", "now"]).round(3).to_string())
        thin = v[~v["from_region_data"]]
        print("  regions using their state's figure:", sorted(set(thin["region"] + " " + thin["fuel"])))
        print("  IPM class used:", {f: v[v.fuel == f].set_index("region")["ipm_class"].to_dict() for f in VRE})
    print(f"\nwrote {OUT}\nwrote {LOG}")


if __name__ == "__main__":
    main()
