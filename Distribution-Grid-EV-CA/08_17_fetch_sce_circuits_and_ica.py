"""
08_17_fetch_sce_circuits_and_ica.py

Pull the two SCE DRPEP layers 08_04 does not: the transmission circuit inventory,
and the section-level ICA.

Why these two
-------------
**Transmission Circuits (layer 5, 1,030 polylines).** SCE's corridors in this model
are 64.3% *inferred* -- 08_08/08_10 connected two substations because the geometry
suggested it, not because any inventory says a line runs there. Only 244 of 1,959
SCE corridors are confirmed. The consequence is measurable: on the PG&E+SCE run,
120 SCE substations end up with a mean corridor degree of 2.65, a mean of 0.74
confirmed corridors and a BA interface on only 0.8% of them, and they shed
**376 GWh** in every hour of the horizon. That is not congestion, it is a
reconstruction gap, and because a stranded node is short in S1 and rescued by a
10x corridor relaxation it would bias `P_cong` upward if SCE were added as-is.

This layer is SCE's equivalent of PG&E's GRIP ``TransmissionLines``, which is why
PG&E reaches 41.4% documented corridors against SCE's 30.6%. Feeding it to 08_08
lets those corridors be asserted from an inventory instead of guessed.

**ICA - Circuit Segments (layer 2, 524,522 polylines).** Carries
``ica_overall_load`` per section together with ``substation_name`` and
``circuit_name``, which is the same section -> feeder -> substation shape as PG&E's
and SDG&E's ICA. 08_06 currently rates SCE from ``GNA Substations``, a single
capacity value with no hourly detail -- the weakest of the three utilities, and the
same limitation Li & Jenn (2024) had for SCE. This layer would let SCE use the
``capacity = headroom + baseload`` identity that 08_12 already applies to PG&E,
putting SCE *ahead* of the published treatment rather than level with it.

Outputs
-------
* ``data/ica/sce/transmission_circuits.gpkg`` -- geometry kept, for 08_08 to assert
  corridors from. Written as a GeoPackage rather than parquet because the line
  geometry is the point of it.
* ``data/ica/sce/ica_circuit_segments.parquet`` -- attributes only. The geometry of
  half a million distribution sections is not needed to aggregate headroom to a
  substation, and dropping it keeps the file in the tens of megabytes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

SCE_DIR = C.DATA_DIR / "ica" / "sce"
BASE = "https://services5.arcgis.com/z6hI6KRjKHvhNO0r/arcgis/rest/services/ICA_Layer/FeatureServer"
UA = {"User-Agent": "UC Davis ITS research (academic, non-commercial)"}

LAYERS = {
    "transmission_circuits": (5, True),
    "ica_circuit_segments": (2, False),
    "substations": (0, True),
}


def _get(url: str, timeout: int = 240, tries: int = 4) -> dict:
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return json.loads(fh.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - transient network, retry
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed after {tries} tries: {url}\n  {last}")


def fetch(layer: int, geometry: bool, page: int = 1000):
    base = f"{BASE}/{layer}"
    total = int(_get(f"{base}/query?where={urllib.parse.quote('1=1')}"
                     f"&returnCountOnly=true&f=json").get("count", 0))
    print(f"  layer {layer}: {total:,} features")

    rows, geoms, offset = [], [], 0
    while offset < total:
        url = (
            f"{base}/query?where={urllib.parse.quote('1=1')}&outFields=*"
            f"&returnGeometry={'true' if geometry else 'false'}&outSR=4326"
            f"&resultOffset={offset}&resultRecordCount={page}&f=json"
        )
        d = _get(url)
        feats = d.get("features") or []
        if not feats:
            break
        for f in feats:
            rows.append(dict(f.get("attributes") or {}))
            if geometry:
                geoms.append(f.get("geometry"))
        offset += len(feats)
        if offset % 25000 == 0 or offset >= total:
            print(f"    {offset:,}/{total:,}")

    return pd.DataFrame(rows), geoms


def _to_geodataframe(df: pd.DataFrame, geoms: list):
    import geopandas as gpd
    from shapely.geometry import LineString, MultiLineString, Point

    def conv(g):
        if not g:
            return None
        if "paths" in g:
            parts = [LineString(p) for p in g["paths"] if len(p) >= 2]
            if not parts:
                return None
            return parts[0] if len(parts) == 1 else MultiLineString(parts)
        if "rings" in g:
            pts = [p for ring in g["rings"] for p in ring]
            if not pts:
                return None
            return Point(sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        if "x" in g and "y" in g:
            return Point(g["x"], g["y"])
        return None

    gs = [conv(g) for g in geoms]
    keep = [i for i, g in enumerate(gs) if g is not None]
    if len(keep) < len(gs):
        print(f"    dropped {len(gs) - len(keep):,} features with unusable geometry")
    return gpd.GeoDataFrame(df.iloc[keep].reset_index(drop=True),
                            geometry=[gs[i] for i in keep], crs="EPSG:4326")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", nargs="*",
                    default=["transmission_circuits", "ica_circuit_segments"],
                    help=f"Subset of {sorted(LAYERS)}.")
    ap.add_argument("--page", type=int, default=1000)
    args = ap.parse_args()

    C.ensure_dir(SCE_DIR)
    for name in args.layers:
        if name not in LAYERS:
            raise SystemExit(f"unknown layer {name!r}; choose from {sorted(LAYERS)}")
        layer, geometry = LAYERS[name]
        df, geoms = fetch(layer, geometry, args.page)
        if geometry:
            g = _to_geodataframe(df, geoms)
            out = SCE_DIR / f"{name}.gpkg"
            g.to_file(out, driver="GPKG")
            print(f"    wrote {out}  ({len(g):,} features)")
        else:
            out = SCE_DIR / f"{name}.parquet"
            df.to_parquet(out, index=False)
            print(f"    wrote {out}  ({len(df):,} rows, {len(df.columns)} cols)")


if __name__ == "__main__":
    main()
