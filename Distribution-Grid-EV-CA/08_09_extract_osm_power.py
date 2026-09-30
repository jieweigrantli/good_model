"""
08_09_extract_osm_power.py

Extract California power infrastructure from the OpenStreetMap bulk extract.

Why bulk rather than Overpass
-----------------------------
The Overpass API returned zero features and timed out across three mirrors
when this was first attempted. The Geofabrik extract is a single 1.3 GB
download that parses offline and reproducibly, and GDAL reads .osm.pbf
natively through its OSM driver, so no compiled OSM library is needed.

What OSM adds that HIFLD does not
---------------------------------
* **Substation polygons.** HIFLD and CEC publish substations as points, so
  "does this line end here?" is a distance guess with a 1,500 m radius. OSM
  maps ~3,600 California substations as footprints, turning that into a
  containment test.
* **Coverage below 100 kV.** HIFLD's transmission line layer stops at 100 kV,
  which is precisely the tier where our corridors are inference-only (2,442 of
  3,030). OSM tags lines at 92 kV, 66 kV and below, plus ~22,000
  ``power=minor_line`` ways.
* **Endpoint names in line names.** Many OSM lines are named for both ends,
  e.g. "Summer Lake - Malin 500KV", giving a second independent assertion of
  connectivity.

Writes:
  data/osm/osm_substations_ca.gpkg   polygons + points, power=substation
  data/osm/osm_power_lines_ca.gpkg   power=line and power=minor_line
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import osmium
import pandas as pd
import shapely.wkb
from shapely.geometry import Polygon

import common as C

OSM_DIR = C.DATA_DIR / "osm"
PBF = OSM_DIR / "california-latest.osm.pbf"
SUBS_OUT = OSM_DIR / "osm_substations_ca.gpkg"
LINES_OUT = OSM_DIR / "osm_power_lines_ca.gpkg"

POWER_LINE = {"line", "minor_line"}
POWER_SUB = {"substation"}
KEEP_TAGS = ("name", "voltage", "operator", "cables", "circuits", "ref", "substation")


class PowerHandler(osmium.SimpleHandler):
    """Stream the extract once, keeping only power ways and substation nodes.

    GDAL can read .osm.pbf, but filtering `other_tags` with LIKE forces a full
    materialisation: a single Bay Area bounding box took 83 s, which does not
    scale to the state. osmium streams the whole 1.3 GB file in ~26 s.
    """

    def __init__(self):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.lines: list[dict] = []
        self.subs: list[dict] = []
        self.skipped = 0

    @staticmethod
    def _tags(o) -> dict:
        return {k: o.tags.get(k) for k in KEEP_TAGS if o.tags.get(k)}

    def way(self, w):
        power = w.tags.get("power")
        if power not in POWER_LINE and power not in POWER_SUB:
            return
        try:
            geom = shapely.wkb.loads(self.wkb.create_linestring(w), hex=True)
        except Exception:  # noqa: BLE001 - ways with missing nodes
            self.skipped += 1
            return
        rec = {"osm_id": f"way/{w.id}", "power": power, **self._tags(w)}
        if power in POWER_SUB:
            coords = list(geom.coords)
            if len(coords) >= 4 and coords[0] == coords[-1]:
                geom = Polygon(coords)
            self.subs.append({**rec, "geometry": geom})
        else:
            self.lines.append({**rec, "geometry": geom})

    def node(self, n):
        if n.tags.get("power") in POWER_SUB:
            self.subs.append(
                {
                    "osm_id": f"node/{n.id}",
                    "power": "substation",
                    **self._tags(n),
                    "geometry": shapely.wkb.loads(self.wkb.create_point(n), hex=True),
                }
            )


def _frame(rows: list[dict]) -> gpd.GeoDataFrame:
    cols = ["osm_id", "power", *KEEP_TAGS, "geometry"]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return gpd.GeoDataFrame(df[cols], geometry="geometry", crs="EPSG:4326")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    C.require_file(PBF, hint="Download california-latest.osm.pbf from Geofabrik")

    print("Streaming the California extract for power features...")
    h = PowerHandler()
    h.apply_file(str(PBF), locations=True, idx="flex_mem")
    print(f"  {len(h.lines):,} power lines, {len(h.subs):,} substations "
          f"({h.skipped:,} ways skipped for incomplete geometry)")

    C.ensure_dir(OSM_DIR)

    subs = _frame(h.subs)
    subs = subs[subs.geometry.notna() & ~subs.geometry.is_empty]
    subs.to_file(SUBS_OUT, driver="GPKG")
    poly = int(subs.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).sum())
    print(f"  wrote {SUBS_OUT}  ({len(subs):,} substations, {poly:,} with a footprint, "
          f"{int(subs['name'].notna().sum()):,} named)")

    lines = _frame(h.lines)
    lines = lines[lines.geometry.notna() & ~lines.geometry.is_empty]
    lines.to_file(LINES_OUT, driver="GPKG")
    kv = pd.to_numeric(lines["voltage"], errors="coerce") / 1000.0
    print(f"  wrote {LINES_OUT}  ({len(lines):,} lines, "
          f"{int(lines['name'].notna().sum()):,} named, {int(kv.notna().sum()):,} with voltage)")
    if kv.notna().any():
        bands = pd.cut(kv, [0, 50, 100, 230, 1000],
                       labels=["<50kV", "50-99kV", "100-229kV", ">=230kV"])
        print("  voltage coverage:", bands.value_counts().to_dict())
    print("  power tag mix:", lines["power"].value_counts().to_dict())


if __name__ == "__main__":
    main()
