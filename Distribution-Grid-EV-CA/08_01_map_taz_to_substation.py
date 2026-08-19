"""
08_01_map_taz_to_substation.py

Map each TAZ centroid to the nearest substation:
  - PG&E GRIP EDSubstations inside the PGE convex hull
  - HIFLD Electric Substations (CA) elsewhere

Writes data/mapping/taz_to_substation.csv
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

import common as C

# CEC / HIFLD-compiled California substations (public ArcGIS FeatureServer)
HIFLD_QUERY_URL = (
    "https://services1.arcgis.com/ZIL9uO234SBBPGL7/arcgis/rest/services/"
    "CA_Substations_Final/FeatureServer/0/query"
)


def _download_hifld_ca(out_path: Path) -> gpd.GeoDataFrame:
    """Download CA substations (CEC+HIFLD compile) via ArcGIS REST geojson pages."""
    C.ensure_dir(out_path.parent)
    frames: list[gpd.GeoDataFrame] = []
    offset = 0
    page_size = 2000
    where = "1=1"
    while True:
        params = (
            f"?where={urllib.parse.quote(where)}"
            f"&outFields=*"
            f"&returnGeometry=true&outSR=4326"
            f"&resultOffset={offset}&resultRecordCount={page_size}"
            f"&f=geojson"
        )
        url = HIFLD_QUERY_URL + params
        print(f"  CA substations download offset={offset} ...")
        with urllib.request.urlopen(url, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        feats = payload.get("features") or []
        if not feats:
            break
        gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
        frames.append(gdf)
        if len(feats) < page_size:
            break
        offset += page_size
    if not frames:
        raise RuntimeError("CA substations download returned no features")
    out = pd.concat(frames, ignore_index=True)
    out = gpd.GeoDataFrame(out, geometry="geometry", crs="EPSG:4326")
    out.to_file(out_path, driver="GPKG")
    print(f"  wrote {out_path} ({len(out)} features)")
    return out


def load_grip_substations() -> gpd.GeoDataFrame:
    gdf = gpd.read_file(C.GRIP_ED_SUBSTATIONS)
    gdf = gdf.to_crs(26910)
    gdf["substation_id"] = gdf["Substati00"].astype(str)
    gdf["substation_name"] = gdf["Substation"].astype(str)
    gdf["source"] = "grip"
    return gdf[["substation_id", "substation_name", "source", "geometry"]].copy()


def load_hifld_substations() -> gpd.GeoDataFrame:
    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        gdf = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG)
    else:
        try:
            gdf = _download_hifld_ca(C.HIFLD_SUBSTATIONS_GPKG)
        except Exception as exc:
            print(f"WARNING: CA substations download failed ({exc}); using GRIP-only map.")
            return gpd.GeoDataFrame(
                columns=["substation_id", "substation_name", "source", "geometry"],
                geometry="geometry",
                crs="EPSG:26910",
            )
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(26910)
    # Prefer stable id fields from CEC layer
    if "OBJECTID" in gdf.columns:
        id_series = gdf["OBJECTID"]
    elif "ID" in gdf.columns:
        id_series = gdf["ID"]
    else:
        id_series = gdf.index.astype(str)
    name_col = next(
        (c for c in ("Name", "NAME", "Substation", "SUBSTAT_NA") if c in gdf.columns),
        None,
    )
    gdf["substation_id"] = "CEC_" + id_series.astype(str)
    gdf["substation_name"] = (
        gdf[name_col].astype(str) if name_col else gdf["substation_id"]
    )
    gdf["source"] = "hifld"
    return gdf[["substation_id", "substation_name", "source", "geometry"]].copy()


def load_taz_centroids() -> gpd.GeoDataFrame:
    taz = gpd.read_file(C.TAZ_CENTROID_GPKG)
    # normalize TAZ id column
    for cand in ("TAZ", "TAZ12", "TAZ_Zone", "taz", "ZONE", "Zone"):
        if cand in taz.columns:
            taz = taz.rename(columns={cand: "TAZ"})
            break
    if "TAZ" not in taz.columns:
        raise KeyError(f"No TAZ column in {C.TAZ_CENTROID_GPKG}; cols={list(taz.columns)}")
    if taz.crs is None:
        taz = taz.set_crs(4326)
    return taz.to_crs(26910)[["TAZ", "geometry"]].copy()


def build_station_universe(
    grip: gpd.GeoDataFrame, hifld: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Use GRIP for PGE footprint; add HIFLD/CEC stations outside a buffer of GRIP.

    If external CA stations are unavailable, add synthetic BA-gateway points so
    southern TAZs are not forced onto distant PGE substations.
    """
    if not hifld.empty:
        try:
            hull = grip.union_all().convex_hull.buffer(5000)
        except Exception:
            hull = grip.unary_union.convex_hull.buffer(5000)
        outside = hifld[~hifld.intersects(hull)].copy()
        print(f"  GRIP stations: {len(grip)}; external outside PGE hull: {len(outside)}")
        return pd.concat([grip, outside], ignore_index=True)

    # Synthetic gateways at approximate CA BA load centers (EPSG:4326 → 26910)
    ba_ll = {
        "WEC_CALN": (-122.0, 38.0),
        "WEC_BANC": (-121.5, 38.6),
        "WECC_SCE": (-117.5, 34.0),
        "WEC_LADW": (-118.3, 34.1),
        "WEC_SDGE": (-117.1, 32.8),
        "WECC_IID": (-115.5, 33.0),
    }
    from shapely.geometry import Point

    rows = []
    for ba, (lon, lat) in ba_ll.items():
        rows.append(
            {
                "substation_id": f"BA_GW_{ba}",
                "substation_name": f"Gateway {ba}",
                "source": "ba_gateway",
                "geometry": Point(lon, lat),
            }
        )
    gw = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(26910)
    print(f"  GRIP stations: {len(grip)}; added {len(gw)} BA gateway proxies (no CEC/HIFLD)")
    return pd.concat([grip, gw], ignore_index=True)


def main() -> None:
    print("Loading GRIP EDSubstations...")
    grip = load_grip_substations()
    print(f"  {len(grip)} GRIP substations")

    print("Loading HIFLD CA substations...")
    hifld = load_hifld_substations()
    print(f"  {len(hifld)} HIFLD stations available")

    stations = build_station_universe(grip, hifld)
    stations = gpd.GeoDataFrame(stations, geometry="geometry", crs="EPSG:26910")

    print("Loading TAZ centroids...")
    taz = load_taz_centroids()
    print(f"  {len(taz)} TAZ centroids")

    print("Nearest-substation join...")
    joined = gpd.sjoin_nearest(
        taz, stations, how="left", distance_col="distance_m"
    )
    # sjoin_nearest can duplicate if ties; keep first
    joined = joined.drop_duplicates(subset=["TAZ"], keep="first")

    out = joined[
        ["TAZ", "substation_id", "substation_name", "source", "distance_m"]
    ].copy()
    out["TAZ"] = out["TAZ"].astype(int)

    C.ensure_dir(C.MAPPING_DIR)
    out_path = C.MAPPING_DIR / "taz_to_substation.csv"
    out.to_csv(out_path, index=False)
    print(f"Wrote {out_path} ({len(out)} rows)")
    print(out["source"].value_counts().to_string())
    print(f"median distance_m={out['distance_m'].median():.0f}")


if __name__ == "__main__":
    # ensure we can import common when run from repo root or this folder
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
