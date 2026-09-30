"""
08_16_fetch_sdge_ica.py

Download SDG&E's Integration Capacity Analysis from their public ArcGIS services.

SDG&E turns out to publish the whole ICA dataset as open ArcGIS Online feature
services under the ``SDGE_ICA`` organisation, reachable from their ICM API
Explorer. An earlier read of this concluded there was no bulk export path,
because the Interconnection Map at ``interconnectionmapsdge.extweb.sempra.com``
renders its layers dynamically and the ICA User Guide documents no download. That
was wrong: the Hub dataset pages resolve to plain feature services that answer
ordinary REST queries, no key and no data request needed.

    https://icm-api-explorer.sdge.com/datasets/<item id>/about
      -> https://www.arcgis.com/sharing/rest/content/items/<item id>?f=json
      -> https://services.arcgis.com/S0EUI1eVapjRPS5e/arcgis/rest/services/<name>/FeatureServer

What this completes
-------------------
SDG&E was the last of the three California IOUs with no capacity data in the
model: 167 substation nodes, all HIFLD-sourced, 0% measured ratings and 0%
measured base load, so its transformer limits were entirely
``0.856 x allocated peak``. It is also the one dataset standing between this study
and the scope of Li & Jenn (2024), who covered PG&E, SCE and SDG&E.

The two layers that matter carry the same identity ``capacity = headroom +
baseload`` that 08_12 already applies to PG&E:

* ``Load Capacity Grids`` -- 501,409 line sections, each with ``ICAWOF_UNILOAD``
  (load integration capacity in MW), ``CIRCUIT_NAME`` and ``SUBID``. Structurally
  the same as PG&E's section-level ICA, so the same section -> feeder -> substation
  aggregation applies.
* ``Substations`` -- 107 substations with ``PROJ_LOAD`` (projected load, MW),
  ``SUBSTATIONTYPE`` (the voltage transformation, e.g. "138/12 kV") and polygon
  geometry for joining to the HIFLD nodes.

A cross-check worth keeping: ``PROJ_LOAD`` sums to 4,997 MW against the 5.3 GW
this model already allocates to SDG&E, a 6% agreement that was not fitted.

Two limits to note
------------------
``SUBID`` is a substation *name*, not an id, so joining to our HIFLD-sourced nodes
needs name matching with a spatial fallback, the way 08_06 already does for SCE
(nearest within 2 km). And the hourly profile is *referenced* but not served:
``IMAP_LOAD_PROFILE`` reads "(576 Data Points)" -- 12 months x 24 hours x 2 day
types -- which the map UI fetches per substation. So this download yields
measured capacity and a peak load, not the hourly series. PG&E remains the only
utility with hourly substation load in this model.

Writes data/ica/sdge/*.parquet
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

SDGE_DIR = C.DATA_DIR / "ica" / "sdge"
ORG = "https://services.arcgis.com/S0EUI1eVapjRPS5e/arcgis/rest/services"
UA = {"User-Agent": "UC Davis ITS research (academic, non-commercial)"}

# PROD views only. The parallel ICA_MAP_QA_* services are staging copies and
# should not be used for published results.
LAYERS = {
    "substations": ("ICA_MAP_PROD_Substations_VW", True),
    "load_capacity": ("ICA_MAP_PROD_LoadCapacityGrids_VW", False),
    "generation_capacity": ("ICA_MAP_PROD_GenerationCapacityGrids_VW", False),
    "gna_grids": ("ICA_MAP_PROD_GNAGrids_VW", False),
    "dupr_planned": ("ICA_MAP_PROD_DUPRPlannedGrids_VW", False),
}


def _get(url: str, timeout: int = 180, tries: int = 4) -> dict:
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


def _count(base: str) -> int:
    d = _get(f"{base}/query?where={urllib.parse.quote('1=1')}&returnCountOnly=true&f=json")
    return int(d.get("count", 0))


def fetch_layer(service: str, want_geometry: bool, page: int = 2000) -> pd.DataFrame:
    base = f"{ORG}/{service}/FeatureServer/0"
    total = _count(base)
    print(f"  {service}: {total:,} features")

    rows, offset = [], 0
    while offset < total:
        url = (
            f"{base}/query?where={urllib.parse.quote('1=1')}&outFields=*"
            f"&returnGeometry={'true' if want_geometry else 'false'}"
            f"&resultOffset={offset}&resultRecordCount={page}&f=json"
        )
        d = _get(url)
        feats = d.get("features") or []
        if not feats:
            break
        for f in feats:
            rec = dict(f.get("attributes") or {})
            if want_geometry and f.get("geometry"):
                # Polygon centroid is enough; the substation layer is a service
                # area outline and only its location is needed for the join.
                rings = f["geometry"].get("rings") or []
                pts = [p for ring in rings for p in ring]
                if pts:
                    rec["lon"] = sum(p[0] for p in pts) / len(pts)
                    rec["lat"] = sum(p[1] for p in pts) / len(pts)
            rows.append(rec)
        offset += len(feats)
        if offset % 20000 == 0 or offset >= total:
            print(f"    {offset:,}/{total:,}")

    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", nargs="*", default=["substations", "load_capacity"],
                    help=f"Subset of {sorted(LAYERS)}. Default: the two 08_06 needs.")
    ap.add_argument("--page", type=int, default=2000, help="Service maxRecordCount is 2000.")
    args = ap.parse_args()

    C.ensure_dir(SDGE_DIR)
    for name in args.layers:
        if name not in LAYERS:
            raise SystemExit(f"unknown layer {name!r}; choose from {sorted(LAYERS)}")
        service, geom = LAYERS[name]
        df = fetch_layer(service, geom, args.page)
        out = SDGE_DIR / f"{name}.parquet"
        df.to_parquet(out, index=False)
        print(f"    wrote {out}  ({len(df):,} rows, {len(df.columns)} cols)")

    subs = SDGE_DIR / "substations.parquet"
    if subs.is_file():
        s = pd.read_parquet(subs)
        if "PROJ_LOAD" in s.columns:
            print(f"\n  cross-check: PROJ_LOAD sums to {s.PROJ_LOAD.sum():,.0f} MW "
                  f"(this model allocates ~5,300 MW of peak to SDG&E)")


if __name__ == "__main__":
    main()
