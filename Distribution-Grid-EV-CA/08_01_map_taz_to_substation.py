"""
08_01_map_taz_to_substation.py

Utility-masked nearest join of TAZ centroids to substations in EPSG:3310:
  PG&E TAZs     → GRIP EDSubstations.shp
  non-PG&E TAZs → HIFLD / CEC substations

Writes data/mapping/taz_to_substation.csv
  columns: TAZ, substation_id, source, distance_m
  (plus substation_name, parent_ba, utility_mask for diagnostics)
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

import common as C

HIFLD_QUERY_URL = (
    "https://services1.arcgis.com/ZIL9uO234SBBPGL7/arcgis/rest/services/"
    "CA_Substations_Final/FeatureServer/7/query"
)
# California_Electric_Utility_Service_Areas was retired by CEC; the modern
# equivalent (ElectricLoadServingEntities_IOU_POU) is already cached on disk
# at C.CEC_LSE_IOU_POU_GPKG, checked first in load_utility_territories().
CEC_UTILITY_QUERY_URL = (
    "https://services3.arcgis.com/bWPjFyq029ChCGur/arcgis/rest/services/"
    "ElectricLoadServingEntities_IOU_POU/FeatureServer/0/query"
)

PGE_DISTANCE_FALLBACK_M = 25_000.0

# Each TAZ's load is split across its KNN_K nearest substations in its utility
# pool, weighted by inverse distance ** KNN_POWER, instead of all going to the
# single nearest one. A real TAZ is served by several substations, and
# single-nearest assignment let a few substations absorb dozens of TAZs.
#   KNN_MIN_DISTANCE_M floors distances so a TAZ centroid sitting on top of a
#     substation does not get an effectively infinite weight.
#   KNN_MAX_RATIO drops neighbours more than this multiple of the (floored)
#     nearest distance, so a TAZ next to a substation stays local while a
#     rural TAZ with no close substation still spreads across several.
KNN_K = 4
KNN_POWER = 2.0
KNN_MIN_DISTANCE_M = 1_000.0
KNN_MAX_RATIO = 3.0


def load_grip_substations() -> gpd.GeoDataFrame:
    C.require_file(
        C.GRIP_ED_SUBSTATIONS,
        hint="Unzip PG&E GRIP shapefiles into data/GRIP_SHP/GRIP_SHP/GRIP_SHP/GRIP_SHP/",
    )
    gdf = gpd.read_file(C.GRIP_ED_SUBSTATIONS)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(C.CA_ALBERS_CRS)
    id_col = C.pick_column(gdf.columns, ("Substati00", "SUBSTATION", "SubstationID", "ID"))
    name_col = C.pick_column(gdf.columns, ("Substation", "NAME", "Name"))
    gdf["substation_id"] = gdf[id_col].astype(str) if id_col else gdf.index.astype(str)
    gdf["substation_name"] = gdf[name_col].astype(str) if name_col else gdf["substation_id"]
    gdf["source"] = "grip"
    return gdf[["substation_id", "substation_name", "source", "geometry"]].copy()


def load_hifld_substations() -> gpd.GeoDataFrame:
    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        gdf = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG)
    else:
        try:
            gdf = C.download_arcgis_geojson(HIFLD_QUERY_URL, C.HIFLD_SUBSTATIONS_GPKG)
        except Exception as exc:
            print(f"WARNING: CA substations download failed ({exc}); HIFLD set empty.")
            return gpd.GeoDataFrame(
                columns=["substation_id", "substation_name", "source", "geometry"],
                geometry="geometry",
                crs=C.CA_ALBERS_CRS,
            )
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(C.CA_ALBERS_CRS)
    if "OBJECTID" in gdf.columns:
        id_series = gdf["OBJECTID"]
    elif "ID" in gdf.columns:
        id_series = gdf["ID"]
    else:
        id_series = gdf.index.astype(str)
    name_col = C.pick_column(gdf.columns, ("Name", "NAME", "Substation", "SUBSTAT_NA"))
    gdf["substation_id"] = "HIFLD_" + id_series.astype(str)
    gdf["substation_name"] = (
        gdf[name_col].astype(str) if name_col else gdf["substation_id"]
    )
    gdf["source"] = "hifld"
    return gdf[["substation_id", "substation_name", "source", "geometry"]].copy()


def load_utility_territories() -> gpd.GeoDataFrame:
    if C.CEC_LSE_IOU_POU_GPKG.is_file():
        gdf = gpd.read_file(C.CEC_LSE_IOU_POU_GPKG)
    elif C.CEC_UTILITY_GPKG.is_file():
        gdf = gpd.read_file(C.CEC_UTILITY_GPKG)
    else:
        try:
            gdf = C.download_arcgis_geojson(CEC_UTILITY_QUERY_URL, C.CEC_UTILITY_GPKG)
        except Exception as exc:
            print(f"WARNING: CEC utility territories download failed ({exc}).")
            return gpd.GeoDataFrame(
                columns=["utility_name", "parent_ba", "is_pge", "geometry"],
                geometry="geometry",
                crs=C.CA_ALBERS_CRS,
            )
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(C.CA_ALBERS_CRS)
    name_col = C.pick_column(
        gdf.columns,
        ("Utility", "UTILITY", "Name", "NAME", "OWNER", "Company", "UTILITY_NA"),
    )
    gdf["utility_name"] = gdf[name_col].astype(str) if name_col else "unknown"
    gdf["is_pge"] = gdf["utility_name"].map(C.utility_is_pge)
    gdf["parent_ba"] = gdf["utility_name"].map(C.utility_to_ba)
    return gdf[["utility_name", "parent_ba", "is_pge", "geometry"]].copy()


def load_taz_centroids() -> gpd.GeoDataFrame:
    C.require_file(C.TAZ_CENTROID_GPKG, hint="Expected data/shps/TAZ_centroid_sf.gpkg")
    taz = gpd.read_file(C.TAZ_CENTROID_GPKG)
    taz_col = C.pick_column(taz.columns, ("TAZ", "TAZ12", "TAZ_Zone", "taz", "ZONE", "Zone"))
    if taz_col is None:
        raise KeyError(f"No TAZ column in {C.TAZ_CENTROID_GPKG}; cols={list(taz.columns)}")
    if taz_col != "TAZ":
        taz = taz.rename(columns={taz_col: "TAZ"})
    if taz.crs is None:
        taz = taz.set_crs(4326)
    return taz.to_crs(C.CA_ALBERS_CRS)[["TAZ", "geometry"]].copy()


def _nearest(taz: gpd.GeoDataFrame, stations: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if taz.empty or stations.empty:
        out = taz.copy()
        out["substation_id"] = pd.NA
        out["substation_name"] = pd.NA
        out["source"] = pd.NA
        out["distance_m"] = np.nan
        return out
    joined = gpd.sjoin_nearest(taz, stations, how="left", distance_col="distance_m")
    return joined.drop_duplicates(subset=["TAZ"], keep="first")


def _knn_allocation(taz: gpd.GeoDataFrame, stations: gpd.GeoDataFrame) -> pd.DataFrame:
    """Split each TAZ across its KNN_K nearest stations by inverse distance."""
    from scipy.spatial import cKDTree

    cols = ["TAZ", "substation_id", "substation_name", "source", "distance_m", "weight", "rank"]
    if taz.empty or stations.empty:
        return pd.DataFrame(columns=cols)

    k = min(KNN_K, len(stations))
    tree = cKDTree(np.column_stack([stations.geometry.x, stations.geometry.y]))
    dist, idx = tree.query(np.column_stack([taz.geometry.x, taz.geometry.y]), k=k)
    dist = np.asarray(dist, dtype=float).reshape(len(taz), k)
    idx = np.asarray(idx).reshape(len(taz), k)

    floored = np.maximum(dist, KNN_MIN_DISTANCE_M)
    keep = floored <= KNN_MAX_RATIO * floored[:, :1]   # nearest is always kept
    w = np.where(keep, floored ** -KNN_POWER, 0.0)
    w = w / w.sum(axis=1, keepdims=True)

    st = stations.reset_index(drop=True)
    taz_ids = taz["TAZ"].to_numpy()
    r, c = np.nonzero(w > 0)
    return pd.DataFrame(
        {
            "TAZ": taz_ids[r],
            "substation_id": st["substation_id"].to_numpy()[idx[r, c]],
            "substation_name": st["substation_name"].to_numpy()[idx[r, c]],
            "source": st["source"].to_numpy()[idx[r, c]],
            "distance_m": dist[r, c],
            "weight": w[r, c],
            "rank": c + 1,
        },
        columns=cols,
    )


def assign_utility_mask(
    taz: gpd.GeoDataFrame, territories: gpd.GeoDataFrame, grip: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Flag each TAZ as PG&E vs non-PG&E using territory polygons, with distance fallback."""
    out = taz.copy()
    out["is_pge"] = False
    out["utility_name"] = pd.NA
    out["parent_ba"] = pd.NA
    if not territories.empty:
        hit = gpd.sjoin(taz, territories, how="left", predicate="within")
        hit = hit.drop_duplicates(subset=["TAZ"], keep="first")
        out = out.drop(columns=["is_pge", "utility_name", "parent_ba"], errors="ignore")
        out = out.merge(
            hit[["TAZ", "is_pge", "utility_name", "parent_ba"]],
            on="TAZ",
            how="left",
        )
        out["is_pge"] = out["is_pge"].fillna(False).astype(bool)
    # Fallback for TAZs that matched no territory polygon at all: within a
    # buffer of a GRIP station, treat as PG&E. It must not touch TAZs that did
    # match a non-PG&E territory -- selecting on ~is_pge used to flip them, so
    # SMUD (WEC_BANC) TAZs 10-24 km from a GRIP station became PG&E and piled
    # ~470k Sacramento residents each onto WEST SACRAMENTO and DEEPWATER, and
    # SCE TAZs in Tulare onto STONE CORRAL.
    unmatched = out["utility_name"].isna()
    if unmatched.any() and not grip.empty:
        tmp = _nearest(out.loc[unmatched, ["TAZ", "geometry"]], grip)
        near = tmp["distance_m"].fillna(np.inf) <= PGE_DISTANCE_FALLBACK_M
        pge_ids = set(tmp.loc[near, "TAZ"])
        out.loc[out["TAZ"].isin(pge_ids), "is_pge"] = True
        out.loc[out["TAZ"].isin(pge_ids) & out["utility_name"].isna(), "utility_name"] = "PG&E (distance fallback)"
        out.loc[out["TAZ"].isin(pge_ids) & out["parent_ba"].isna(), "parent_ba"] = "WEC_CALN"
    return out


def main() -> None:
    print("Loading GRIP EDSubstations (EPSG:3310)...")
    grip = load_grip_substations()
    print(f"  {len(grip)} GRIP substations")

    print("Loading HIFLD / CEC substations...")
    hifld = load_hifld_substations()
    print(f"  {len(hifld)} HIFLD stations available")

    print("Loading utility service territories...")
    territories = load_utility_territories()
    print(f"  {len(territories)} territory polygons")

    print("Loading TAZ centroids...")
    taz = load_taz_centroids()
    print(f"  {len(taz)} TAZ centroids")

    taz = assign_utility_mask(taz, territories, grip)
    n_pge = int(taz["is_pge"].sum())
    print(f"  PG&E-masked TAZs: {n_pge}; non-PG&E: {len(taz) - n_pge}")

    pge_taz = taz[taz["is_pge"]].copy()
    other_taz = taz[~taz["is_pge"]].copy()

    print("Nearest join: PG&E TAZs -> EDSubstations ...")
    pge_join = _nearest(pge_taz[["TAZ", "geometry"]], grip)

    print("Nearest join: non-PG&E TAZs -> HIFLD substations ...")
    hifld_pool = hifld if not hifld.empty else C.ba_gateway_points_gdf()
    if hifld.empty:
        print("  HIFLD empty — using BA gateway proxies for non-PG&E TAZs")
    other_join = _nearest(other_taz[["TAZ", "geometry"]], hifld_pool)

    joined = pd.concat([pge_join, other_join], ignore_index=True)
    joined = gpd.GeoDataFrame(joined, geometry="geometry", crs=C.CA_ALBERS_CRS)
    joined = joined.drop_duplicates(subset=["TAZ"], keep="first")
    joined = joined.merge(
        taz[["TAZ", "is_pge", "utility_name", "parent_ba"]],
        on="TAZ",
        how="left",
    )

    out = joined[
        [
            "TAZ",
            "substation_id",
            "substation_name",
            "source",
            "distance_m",
            "is_pge",
            "utility_name",
            "parent_ba",
        ]
    ].copy()
    out["TAZ"] = out["TAZ"].astype(int)
    out["substation_id"] = out["substation_id"].astype(str)
    out["utility_mask"] = np.where(out["is_pge"], "pge_edsubstation", "hifld")

    C.ensure_dir(C.MAPPING_DIR)
    out_path = C.TAZ_TO_SUBSTATION_CSV
    keep = ["TAZ", "substation_id", "source", "distance_m", "substation_name", "utility_mask", "parent_ba"]
    out[keep].to_csv(out_path, index=False)
    print(f"Wrote {out_path} ({len(out)} rows)")
    print(out["source"].value_counts().to_string())
    print(f"median distance_m={out['distance_m'].median():.0f}")

    print(f"k-nearest allocation (k={KNN_K}, 1/d^{KNN_POWER:g}) within each utility pool ...")
    knn = pd.concat(
        [
            _knn_allocation(pge_taz[["TAZ", "geometry"]], grip).assign(utility_mask="pge_edsubstation"),
            _knn_allocation(other_taz[["TAZ", "geometry"]], hifld_pool).assign(utility_mask="hifld"),
        ],
        ignore_index=True,
    )
    knn = knn.merge(taz[["TAZ", "parent_ba"]], on="TAZ", how="left")
    knn["TAZ"] = knn["TAZ"].astype(int)
    knn["substation_id"] = knn["substation_id"].astype(str)
    knn.to_csv(C.TAZ_TO_SUBSTATION_KNN_CSV, index=False)
    per_taz = knn.groupby("TAZ")["weight"].agg(["size", "sum"])
    print(
        f"Wrote {C.TAZ_TO_SUBSTATION_KNN_CSV} ({len(knn)} rows, {len(per_taz)} TAZs, "
        f"mean {per_taz['size'].mean():.2f} substations/TAZ, "
        f"weight sums {per_taz['sum'].min():.6f}-{per_taz['sum'].max():.6f})"
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
