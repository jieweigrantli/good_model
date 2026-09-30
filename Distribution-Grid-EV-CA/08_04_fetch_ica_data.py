"""
08_04_fetch_ica_data.py

Pull the public distribution-planning data the IOUs publish under the CPUC
Distribution Resources Plan, so substation baseline demand and capacity come
from measurement rather than from voltage heuristics.

PG&E already ships as a bulk shapefile download (data/GRIP_SHP), so this
script covers SCE's DRPEP, which is only served through ArcGIS REST.

Layers pulled (substation level; circuit level is opt-in via --circuits
because it is ~1.2M rows):
  ICA Substation Load Profile   288 month-hour bins of min/max load per substation
  ICA Substations               substation voltage, projected load, remaining capacity
  GNA Substations               rating and 10-year capacity forecast
  GNA Planning Assumptions      per substation-year: base_demand, ev, pv, es, capacity

Writes parquet under data/ica/sce/.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

import common as C

OUT_DIR = C.DATA_DIR / "ica" / "sce"

ICA_TABLES = "https://services5.arcgis.com/z6hI6KRjKHvhNO0r/arcgis/rest/services/ICA_Tables/FeatureServer"
ICA_LAYER = "https://services5.arcgis.com/z6hI6KRjKHvhNO0r/arcgis/rest/services/ICA_Layer/FeatureServer"
GNA_LAYER = "https://drpep.sce.com/arcgis_server/rest/services/Hosted/GNA_Layer/FeatureServer"

LAYERS = {
    "substation_load_profile": f"{ICA_TABLES}/2",
    "substations": f"{ICA_LAYER}/0",
    "gna_substations": f"{GNA_LAYER}/1",
    "gna_planning_assumptions": f"{GNA_LAYER}/5",
}
CIRCUIT_LAYERS = {
    "circuit_load_profile": f"{ICA_TABLES}/1",
    "ica_circuit_segments": f"{ICA_LAYER}/2",
}

# Circuit geometry carrying its parent substation (`circt_nam` -> `sub_name`),
# the SCE analogue of PG&E's FeederDetail.shp. Needed for the block-to-feeder
# assignment, so it is fetched with geometry rather than as a flat table.
CIRCUIT_GEOM = {
    "distribution_circuits": "https://drpep.sce.com/arcgis_server/rest/services/Hosted/Distribution_circuits/FeatureServer/0",
    # Substation points, so a feeder's parent substation can be matched to a
    # model node by location instead of by name. Name matching left 29,889 SCE
    # blocks unresolved because SCE and HIFLD spell the same yard differently
    # ("Universal 69/12 kV" vs "Universal City").
    "ica_substations_geom": f"{ICA_LAYER}/0",
}

UA = {"User-Agent": "Mozilla/5.0 (research; ASTR2026 UC Davis)"}
PAGE = 2000


def _get(url: str, timeout: int = 180) -> dict:
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return json.load(fh)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def layer_count(base: str) -> int:
    d = _get(f"{base}/query?where={urllib.parse.quote('1=1')}&returnCountOnly=true&f=json")
    return int(d.get("count", 0))


def fetch_layer(base: str, name: str) -> pd.DataFrame:
    total = layer_count(base)
    print(f"  {name}: {total:,} records")
    rows: list[dict] = []
    offset = 0
    while offset < total:
        url = (
            f"{base}/query?where={urllib.parse.quote('1=1')}&outFields=*"
            f"&returnGeometry=false&resultOffset={offset}&resultRecordCount={PAGE}&f=json"
        )
        d = _get(url)
        feats = d.get("features") or []
        if not feats:
            break
        rows.extend(f["attributes"] for f in feats)
        offset += len(feats)
        if offset % 20000 < PAGE:
            print(f"    {offset:,}/{total:,}", flush=True)
    return pd.DataFrame(rows)


def fetch_geometry(base: str, name: str, out: "Path") -> None:
    """Page a polyline/point layer down as GeoJSON and write a GeoPackage."""
    import geopandas as gpd

    total = layer_count(base)
    print(f"  {name}: {total:,} features (with geometry)")
    frames = []
    offset = 0
    while offset < total:
        url = (
            f"{base}/query?where={urllib.parse.quote('1=1')}&outFields=*"
            f"&returnGeometry=true&outSR=4326&resultOffset={offset}"
            f"&resultRecordCount={PAGE}&f=geojson"
        )
        d = _get(url, timeout=300)
        feats = d.get("features") or []
        if not feats:
            break
        frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326"))
        offset += len(feats)
        if offset % 10000 < PAGE:
            print(f"    {offset:,}/{total:,}", flush=True)
    if not frames:
        print(f"  {name}: no features returned")
        return
    import pandas as pd_

    gdf = gpd.GeoDataFrame(pd_.concat(frames, ignore_index=True), crs="EPSG:4326")
    gdf.to_file(out, driver="GPKG")
    print(f"  wrote {out}  ({len(gdf):,} features)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--circuits", action="store_true",
                    help="also pull circuit-level tables (~1.2M rows, slow)")
    args = ap.parse_args()

    C.ensure_dir(OUT_DIR)
    targets = dict(LAYERS)
    if args.circuits:
        targets.update(CIRCUIT_LAYERS)

    for name, base in targets.items():
        out = OUT_DIR / f"{name}.parquet"
        if out.is_file():
            print(f"  {name}: already downloaded ({out})")
            continue
        try:
            df = fetch_layer(base, name)
        except Exception as exc:
            print(f"  {name}: FAILED - {type(exc).__name__}: {exc}")
            continue
        if df.empty:
            print(f"  {name}: no rows returned")
            continue
        df.to_parquet(out, index=False)
        print(f"  wrote {out}  ({len(df):,} rows x {df.shape[1]} cols)")

    for name, base in CIRCUIT_GEOM.items():
        out = OUT_DIR / f"{name}.gpkg"
        if out.is_file():
            print(f"  {name}: already downloaded ({out})")
            continue
        try:
            fetch_geometry(base, name, out)
        except Exception as exc:
            print(f"  {name}: FAILED - {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
