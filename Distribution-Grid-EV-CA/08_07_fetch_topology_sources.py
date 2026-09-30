"""
08_07_fetch_topology_sources.py

Pull the two public sources that carry real transmission connectivity, so
corridors stop being inferred purely from how close two drawn shapes happen
to be.

Why
---
Neither GRIP nor the CEC line layer says what connects to what: both are a
drawn line with a voltage and a name. FERC classifies real topology as
Critical Energy/Electric Infrastructure Information, so no authoritative
connectivity model is published -- the California Test System, for instance,
pairs real corridor locations with deliberately *invented* topology.

Two public sources still assert connections:

1. HIFLD Electric Power Transmission Lines carry ``SUB_1``/``SUB_2``, the
   named terminal substations. Over California, 2,171 lines with 86% of both
   endpoints named. Nothing below 100 kV.
2. OpenStreetMap maps substations as *polygons* (3,635 in California, against
   4,015 objects total), which turns "does this line end at this substation?"
   from a distance guess into a containment test. Its line ways also carry
   voltage, operator, circuits, and names that encode both endpoints
   ("Summer Lake - Malin 500KV"), and reach below 100 kV where HIFLD stops.

Writes:
  data/hifld/transmission_lines_ca.gpkg
  data/osm/osm_substations_ca.gpkg
  data/osm/osm_power_lines_ca.gpkg
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request

import geopandas as gpd
import pandas as pd

import common as C

HIFLD_LINES_URL = (
    "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
    "Electric_Power_Transmission_Lines/FeatureServer/0"
)
HIFLD_OUT = C.DATA_DIR / "hifld" / "transmission_lines_ca.gpkg"

OSM_DIR = C.DATA_DIR / "osm"
OSM_SUBS_OUT = OSM_DIR / "osm_substations_ca.gpkg"
OSM_LINES_OUT = OSM_DIR / "osm_power_lines_ca.gpkg"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

CA_ENVELOPE = {"xmin": -124.6, "ymin": 32.4, "xmax": -114.0, "ymax": 42.2}
UA = {"User-Agent": "Mozilla/5.0 (ASTR2026 research; UC Davis)"}
PAGE = 1000


def _get_json(url: str, timeout: int = 240) -> dict:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout
            ) as fh:
                return json.load(fh)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(4 * (attempt + 1))
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# HIFLD
# --------------------------------------------------------------------------
def fetch_hifld_lines() -> None:
    if HIFLD_OUT.is_file():
        print(f"  already have {HIFLD_OUT}")
        return
    env = dict(CA_ENVELOPE, spatialReference={"wkid": 4326})
    common = (
        "geometry=" + urllib.parse.quote(json.dumps(env))
        + "&geometryType=esriGeometryEnvelope&inSR=4326"
        + "&spatialRel=esriSpatialRelIntersects"
    )
    total = _get_json(f"{HIFLD_LINES_URL}/query?{common}&returnCountOnly=true&f=json")
    total = int(total.get("count", 0))
    print(f"  HIFLD: {total:,} lines intersecting California")

    frames, offset = [], 0
    while offset < total:
        url = (
            f"{HIFLD_LINES_URL}/query?{common}&outFields=*&returnGeometry=true"
            f"&outSR=4326&resultOffset={offset}&resultRecordCount={PAGE}&f=geojson"
        )
        d = _get_json(url, timeout=300)
        feats = d.get("features") or []
        if not feats:
            break
        frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326"))
        offset += len(feats)
        print(f"    {offset:,}/{total:,}", flush=True)

    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326")
    C.ensure_dir(HIFLD_OUT.parent)
    gdf.to_file(HIFLD_OUT, driver="GPKG")

    def named(col):
        s = gdf[col].astype(str).str.strip().str.upper()
        return ~(s.eq("") | s.eq("NOT AVAILABLE") | s.str.startswith("UNKNOWN") | s.eq("NONE"))

    both = (named("SUB_1") & named("SUB_2")).sum()
    print(f"  wrote {HIFLD_OUT}  ({len(gdf):,} lines, {both:,} with both endpoints named)")


# --------------------------------------------------------------------------
# OpenStreetMap
# --------------------------------------------------------------------------
def _overpass(query: str, timeout: int = 300) -> dict:
    last = None
    for ep in OVERPASS_ENDPOINTS:
        for attempt in range(2):
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(ep, data=data, headers=UA)
                with urllib.request.urlopen(req, timeout=timeout) as fh:
                    return json.load(fh)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(5 * (attempt + 1))
        print(f"    endpoint failed ({type(last).__name__}); trying next")
    raise RuntimeError(f"all Overpass endpoints failed: {last}")


def _tiles(step: float = 1.6):
    y = CA_ENVELOPE["ymin"]
    while y < CA_ENVELOPE["ymax"]:
        x = CA_ENVELOPE["xmin"]
        while x < CA_ENVELOPE["xmax"]:
            yield (y, x, min(y + step, CA_ENVELOPE["ymax"]), min(x + step, CA_ENVELOPE["xmax"]))
            x += step
        y += step


def _elements_to_gdf(elements: list[dict], want_polygons: bool) -> gpd.GeoDataFrame:
    from shapely.geometry import LineString, Point, Polygon

    rows = []
    for el in elements:
        tags = el.get("tags") or {}
        geom = None
        if el["type"] == "node" and "lat" in el:
            geom = Point(el["lon"], el["lat"])
        elif el["type"] == "way" and el.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if len(pts) < 2:
                continue
            if want_polygons and len(pts) >= 4 and pts[0] == pts[-1]:
                geom = Polygon(pts)
            else:
                geom = LineString(pts)
        if geom is None:
            continue
        rows.append(
            {
                "osm_id": f"{el['type']}/{el['id']}",
                "name": tags.get("name"),
                "operator": tags.get("operator"),
                "voltage": tags.get("voltage"),
                "power": tags.get("power"),
                "ref": tags.get("ref"),
                "circuits": tags.get("circuits"),
                "cables": tags.get("cables"),
                "geometry": geom,
            }
        )
    if not rows:
        return gpd.GeoDataFrame(columns=["osm_id", "geometry"], geometry="geometry", crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def fetch_osm(kind: str, out_path) -> None:
    if out_path.is_file():
        print(f"  already have {out_path}")
        return
    if kind == "substation":
        body = '(way["power"="substation"]({bbox});relation["power"="substation"]({bbox});node["power"="substation"]({bbox}););'
        want_poly = True
    else:
        body = '(way["power"="line"]({bbox});way["power"="minor_line"]({bbox}););'
        want_poly = False

    frames = []
    tiles = list(_tiles())
    for i, (s, w, n, e) in enumerate(tiles, 1):
        bbox = f"{s},{w},{n},{e}"
        q = f"[out:json][timeout:280];{body.format(bbox=bbox)}out geom tags;"
        try:
            d = _overpass(q)
        except Exception as exc:  # noqa: BLE001
            print(f"    tile {i}/{len(tiles)} FAILED: {type(exc).__name__}")
            continue
        gdf = _elements_to_gdf(d.get("elements", []), want_poly)
        if len(gdf):
            frames.append(gdf)
        print(f"    tile {i}/{len(tiles)}: {len(gdf):,} features", flush=True)

    if not frames:
        print(f"  no OSM {kind} features retrieved")
        return
    out = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326")
    out = out.drop_duplicates("osm_id").reset_index(drop=True)
    C.ensure_dir(out_path.parent)
    out.to_file(out_path, driver="GPKG")
    poly = (out.geometry.geom_type == "Polygon").sum()
    print(f"  wrote {out_path}  ({len(out):,} features, {poly:,} polygons)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-hifld", action="store_true")
    ap.add_argument("--skip-osm", action="store_true")
    args = ap.parse_args()

    if not args.skip_hifld:
        print("HIFLD transmission lines (SUB_1/SUB_2 endpoints)...")
        fetch_hifld_lines()

    if not args.skip_osm:
        print("OSM substations (polygons)...")
        fetch_osm("substation", OSM_SUBS_OUT)
        print("OSM power lines...")
        fetch_osm("line", OSM_LINES_OUT)


if __name__ == "__main__":
    main()
