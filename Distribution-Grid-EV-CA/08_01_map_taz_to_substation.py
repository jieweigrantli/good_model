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
from shapely.geometry import Point

import common as C

HIFLD_QUERY_URL = (
    "https://services1.arcgis.com/ZIL9uO234SBBPGL7/arcgis/rest/services/"
    "CA_Substations_Final/FeatureServer/0/query"
)
CEC_UTILITY_QUERY_URL = (
    "https://services3.arcgis.com/bWPjFyq029ChCGur/arcgis/rest/services/"
    "California_Electric_Utility_Service_Areas/FeatureServer/0/query"
)

PGE_DISTANCE_FALLBACK_M = 25_000.0


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
    if C.CEC_UTILITY_GPKG.is_file():
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


def _ba_gateway_points() -> gpd.GeoDataFrame:
    rows = []
    ba_ll = {
        "WEC_CALN": (-122.0, 38.0),
        "WEC_BANC": (-121.5, 38.6),
        "WECC_SCE": (-117.5, 34.0),
        "WEC_LADW": (-118.3, 34.1),
        "WEC_SDGE": (-117.1, 32.8),
        "WECC_IID": (-115.5, 33.0),
    }
    for ba, (lon, lat) in ba_ll.items():
        rows.append(
            {
                "substation_id": f"BA_GW_{ba}",
                "substation_name": f"Gateway {ba}",
                "source": "ba_gateway",
                "geometry": Point(lon, lat),
            }
        )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(C.CA_ALBERS_CRS)


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
        out = out.drop(columns=["is_pge"], errors="ignore")
        out = out.merge(
            hit[["TAZ", "is_pge", "utility_name", "parent_ba"]],
            on="TAZ",
            how="left",
        )
        out["is_pge"] = out["is_pge"].fillna(False).astype(bool)
    # Fallback: TAZ within buffer of a GRIP station is treated as PG&E
    unmatched = ~out["is_pge"]
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

    print("Nearest join: PG&E TAZs → EDSubstations ...")
    pge_join = _nearest(pge_taz[["TAZ", "geometry"]], grip)

    print("Nearest join: non-PG&E TAZs → HIFLD substations ...")
    hifld_pool = hifld if not hifld.empty else _ba_gateway_points()
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


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
