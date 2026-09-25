"""
08_05_assign_substation_ba.py

Canonical substation -> balancing-area assignment, from grid data rather than
from the demand that happens to land on a substation.

Why this exists
---------------
``08_03._assign_parent_ba`` decides a substation's BA by majority vote of the
TAZs that the k-nearest allocation attached to it. That works for a
load-serving distribution substation with many TAZs and fails for bulk
transmission hubs, which serve no local load and therefore appear in the TAZ
table only as incidental far-rank neighbours. Tesla -- a 500 kV PG&E switching
hub -- was assigned to WEC_BANC by a single TAZ 5.4 km away that ranked it 4th
of 4. Measured against the HIFLD ``Owner`` field, PG&E-owned substations were
assigned the wrong BA 66% of the time, and 13 voltage gateways carrying
16.2 GW attached to the wrong BA bus.

Ownership and service territory are the right inputs, and both are published.
Precedence:
  1. the substation's own ``Owner`` (HIFLD), or PG&E for GRIP substations
  2. the CEC load-serving-entity territory polygon containing it
  3. the nearest CEC territory polygon
  4. the nearest BA centroid (last resort)

Writes:
  data/meso/substation_ba.csv   substation_id, parent_ba, ba_source, utility
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import numpy as np
import pandas as pd

import common as C

OUT_CSV = C.MESO_DIR / "substation_ba.csv"

# Utility name fragment -> WECC BA, extending common.UTILITY_TO_BA with the
# publicly owned utilities that appear in the CEC territory layer. Members of
# the Balancing Authority of Northern California are grouped to WEC_BANC;
# small municipals embedded inside an IOU footprint are left to fall through
# to the geographic rule, since they are delivered through the host system.
EXTRA_UTILITY_TO_BA = (
    ("sacramento municipal", "WEC_BANC"),
    ("smud", "WEC_BANC"),
    ("modesto irrigation", "WEC_BANC"),
    ("turlock irrigation", "WEC_BANC"),
    ("roseville", "WEC_BANC"),
    ("redding", "WEC_BANC"),
    ("shasta lake", "WEC_BANC"),
    ("trinity", "WEC_BANC"),
    ("lassen", "WEC_BANC"),
    ("plumas-sierra", "WEC_BANC"),
    ("biggs", "WEC_BANC"),
    ("gridley", "WEC_BANC"),
    ("lodi", "WEC_BANC"),
    ("healdsburg", "WEC_BANC"),
    ("ukiah", "WEC_BANC"),
    ("los angeles department", "WEC_LADW"),
    ("imperial irrigation", "WECC_IID"),
    ("san diego gas", "WEC_SDGE"),
    ("southern california edison", "WECC_SCE"),
    ("pacific gas", "WEC_CALN"),
)


def utility_to_ba(name: str) -> str | None:
    s = str(name or "").lower()
    for frag, ba in tuple(C.UTILITY_TO_BA) + EXTRA_UTILITY_TO_BA:
        if frag in s:
            return ba
    return None


def load_nodes() -> gpd.GeoDataFrame:
    """Every substation the model may use, with owner where published."""
    frames = []

    grip = gpd.read_file(C.GRIP_ED_SUBSTATIONS).to_crs(C.CA_ALBERS_CRS)
    gid = C.pick_column(grip.columns, ("Substati00", "SUBSTATION", "SubstationID"))
    grip["substation_id"] = grip[gid].astype(str)
    grip["owner"] = "Pacific Gas & Electric Company"  # GRIP is PG&E's own portal
    frames.append(grip[["substation_id", "owner", "geometry"]])

    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        hif = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG).to_crs(C.CA_ALBERS_CRS)
        hif["substation_id"] = "HIFLD_" + hif["OBJECTID"].astype(str)
        oc = C.pick_column(hif.columns, ("Owner", "OWNER"))
        hif["owner"] = hif[oc] if oc else None
        frames.append(hif[["substation_id", "owner", "geometry"]])

    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True),
                           geometry="geometry", crs=C.CA_ALBERS_CRS)
    return gdf.drop_duplicates("substation_id").reset_index(drop=True)


def load_territories() -> gpd.GeoDataFrame:
    C.require_file(C.CEC_LSE_IOU_POU_GPKG, hint="Need data/cec/ca_lse_iou_pou.gpkg")
    t = gpd.read_file(C.CEC_LSE_IOU_POU_GPKG).to_crs(C.CA_ALBERS_CRS)
    name_col = C.pick_column(t.columns, ("Utility", "OnlineName", "Acronym"))
    t = t.rename(columns={name_col: "utility"})
    t["territory_ba"] = t["utility"].map(utility_to_ba)
    return t[["utility", "territory_ba", "geometry"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.parse_args()

    print("Loading substations...")
    nodes = load_nodes()
    print(f"  {len(nodes):,} substations")

    print("Loading CEC utility territories...")
    terr = load_territories()
    named = terr["territory_ba"].notna().sum()
    print(f"  {len(terr)} territories, {named} mapped to a BA by name")

    # 1. owner
    nodes["owner_ba"] = nodes["owner"].map(utility_to_ba)

    # 2. containing territory
    j = gpd.sjoin(nodes[["substation_id", "geometry"]], terr, how="left", predicate="within")
    j = j.sort_values("territory_ba").drop_duplicates("substation_id")
    contain = j.set_index("substation_id")["territory_ba"]
    contain_util = j.set_index("substation_id")["utility"]

    # 3. nearest territory that does map to a BA
    known = terr[terr["territory_ba"].notna()]
    nr = gpd.sjoin_nearest(nodes[["substation_id", "geometry"]], known,
                           how="left", distance_col="d").drop_duplicates("substation_id")
    nearest = nr.set_index("substation_id")["territory_ba"]

    # 4. nearest BA centroid
    ba_pts = gpd.GeoDataFrame(
        {"ba": list(C.BA_CENTROIDS_LL.keys()),
         "geometry": gpd.points_from_xy(
             [v[0] for v in C.BA_CENTROIDS_LL.values()],
             [v[1] for v in C.BA_CENTROIDS_LL.values()])},
        crs="EPSG:4326").to_crs(C.CA_ALBERS_CRS)
    cn = gpd.sjoin_nearest(nodes[["substation_id", "geometry"]], ba_pts,
                           how="left").drop_duplicates("substation_id")
    centroid = cn.set_index("substation_id")["ba"]

    sid = nodes["substation_id"]
    ba, src = [], []
    for s, ob in zip(sid, nodes["owner_ba"]):
        if pd.notna(ob) and ob:
            ba.append(ob); src.append("owner")
        elif pd.notna(contain.get(s)):
            ba.append(contain.get(s)); src.append("territory_contains")
        elif pd.notna(nearest.get(s)):
            ba.append(nearest.get(s)); src.append("territory_nearest")
        else:
            ba.append(centroid.get(s, "WEC_CALN")); src.append("ba_centroid")

    out = pd.DataFrame({
        "substation_id": sid,
        "parent_ba": ba,
        "ba_source": src,
        "utility": [contain_util.get(s) for s in sid],
        "owner": nodes["owner"].values,
    })

    C.ensure_dir(OUT_CSV.parent)
    out.to_csv(OUT_CSV, index=False)
    print(f"  wrote {OUT_CSV}")
    print()
    print("assignment source:")
    print(out.groupby("ba_source").size().to_string())
    print()
    print("parent_ba distribution:")
    print(out.groupby("parent_ba").size().to_string())

    # How much did this move relative to the TAZ-vote assignment in use today?
    nodes_csv = C.MESO_DIR / "meso_nodes.csv"
    if nodes_csv.is_file():
        cur = pd.read_csv(nodes_csv)
        cur["substation_id"] = cur["substation_id"].astype(str)
        m = cur.merge(out, on="substation_id", how="inner", suffixes=("_old", "_new"))
        chg = m["parent_ba_old"] != m["parent_ba_new"]
        print()
        print(f"vs current TAZ-vote assignment: {chg.sum():,}/{len(m):,} substations change BA "
              f"({100 * chg.mean():.0f}%), moving {m.loc[chg, 'total_peak_W'].sum() / 1e9:.1f} GW of peak")
        print(pd.crosstab(m.loc[chg, "parent_ba_old"], m.loc[chg, "parent_ba_new"]).to_string())


if __name__ == "__main__":
    main()
