"""
08_08_build_asserted_topology.py

Corridors that a published source *asserts*, rather than ones we infer from
how close two drawn shapes happen to be.

Why
---
GRIP and CEC publish line geometry with a voltage and a name, and nothing
about what connects to what. FERC classifies real topology as Critical
Energy/Electric Infrastructure Information, so no authoritative connectivity
model is published -- the California Test System pairs real corridor
locations with deliberately invented topology for exactly this reason.

Two public sources still name their endpoints:

* HIFLD Electric Power Transmission Lines carry ``SUB_1``/``SUB_2``. Over
  California, 1,864 of 2,171 lines name both ends. Nothing below 100 kV.
* OpenStreetMap maps substations as polygons and often encodes both endpoints
  in a line's name ("Summer Lake - Malin 500KV"), and reaches below 100 kV.

Endpoint names are resolved to model nodes through a three-step chain,
because matching names directly against our node set succeeds only 47% of the
time, while HIFLD's own substation layer -- same publisher, same naming --
matches 69%:

  1. direct name match against a model node
  2. name match against the HIFLD substation layer, then nearest model node
  3. the line's own endpoint geometry, then nearest model node

Writes:
  data/meso/asserted_corridors.csv
"""

from __future__ import annotations

import argparse
import re

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

import common as C

OUT_CSV = C.MESO_DIR / "asserted_corridors.csv"
HIFLD_LINES = C.DATA_DIR / "hifld" / "transmission_lines_ca.gpkg"
OSM_LINES = C.DATA_DIR / "osm" / "osm_power_lines_ca.gpkg"
OSM_SUBS = C.DATA_DIR / "osm" / "osm_substations_ca.gpkg"

# A named endpoint must plausibly be at the line's end. Generous, because the
# two datasets are digitised independently, but finite so a name collision
# across the state cannot create a corridor.
MAX_ENDPOINT_DIST_M = 25_000.0
# How close a located substation must be to a model node to stand in for it.
NODE_SNAP_M = 2_000.0


def norm(s) -> str:
    s = str(s or "").upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(SUBSTATION|SUB|STATION|SWITCHING|SWYD|TAP)\b", " ", s)
    s = re.sub(r"\b\d{1,3}\s*/?\s*\d{0,3}\s*KV\b", " ", s)
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def _bad_name(s) -> bool:
    s = str(s or "").strip().upper()
    return (not s) or s in ("NOT AVAILABLE", "NONE") or s.startswith("UNKNOWN")


class Resolver:
    """Endpoint name + endpoint location -> model node id."""

    def __init__(self, hubs: gpd.GeoDataFrame):
        self.ids = hubs["hub_id"].to_numpy()
        self.x = hubs.geometry.x.to_numpy()
        self.y = hubs.geometry.y.to_numpy()
        self.tree = cKDTree(np.column_stack([self.x, self.y]))
        self.site_kv = (
            pd.to_numeric(hubs.get("site_kv"), errors="coerce").to_numpy(dtype=float)
            if "site_kv" in hubs.columns
            else np.full(len(hubs), np.nan)
        )

        self.by_name: dict[str, list[int]] = {}
        for i, n in enumerate(hubs["substation_name"].map(norm)):
            if n:
                self.by_name.setdefault(n, []).append(i)

        # Substation gazetteers: name -> location, from sources that name the
        # same yards our line layers refer to.
        self.gazetteer: dict[str, list[tuple[float, float]]] = {}
        if C.HIFLD_SUBSTATIONS_GPKG.is_file():
            hs = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG).to_crs(C.CA_ALBERS_CRS)
            for _, r in hs.iterrows():
                n = norm(r.get("Name"))
                if n:
                    self.gazetteer.setdefault(n, []).append((r.geometry.x, r.geometry.y))

        # OSM substation footprints. A polygon turns "does this line end at
        # this substation?" from a distance guess into a containment test,
        # which is the single biggest weakness of point-based snapping: 240 of
        # our isolated nodes sit within 250 m of a line that we could not
        # attach because the nearest *point* belonged to something else.
        self.osm_polys = None
        if OSM_SUBS.is_file():
            o = gpd.read_file(OSM_SUBS).to_crs(C.CA_ALBERS_CRS)
            o = o[o.geometry.notna() & ~o.geometry.is_empty]
            for _, r in o.iterrows():
                n = norm(r.get("name"))
                if n:
                    c = r.geometry.centroid
                    self.gazetteer.setdefault(n, []).append((c.x, c.y))
            poly = o[o.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
            if len(poly):
                poly["node_idx"] = [
                    self._nearest((g.centroid.x, g.centroid.y), limit=NODE_SNAP_M)
                    for g in poly.geometry
                ]
                self.osm_polys = poly[poly["node_idx"].notna()].reset_index(drop=True)
                self.osm_sindex = self.osm_polys.sindex
                print(f"  OSM: {len(o):,} substations, {len(poly):,} with footprints, "
                      f"{len(self.osm_polys):,} matched to a model node")

        self.stats = {"direct": 0, "gazetteer": 0, "contains": 0,
                      "geometry": 0, "unresolved": 0}

    def _nearest(self, xy, kv=np.nan, limit=NODE_SNAP_M):
        d, i = self.tree.query(xy, k=8)
        d = np.atleast_1d(d)
        i = np.atleast_1d(i)
        for dd, ii in zip(d, i):
            if dd > limit:
                break
            here = self.site_kv[ii]
            if np.isnan(kv) or np.isnan(here) or here + 1e-9 >= kv:
                return int(ii)
        return None

    def resolve(self, name, xy, kv=np.nan):
        key = norm(name)

        cands = self.by_name.get(key)
        if cands:
            d = [np.hypot(self.x[i] - xy[0], self.y[i] - xy[1]) for i in cands]
            j = int(np.argmin(d))
            if d[j] <= MAX_ENDPOINT_DIST_M:
                self.stats["direct"] += 1
                return cands[j]

        # Containment: is the line endpoint inside a mapped substation?
        if self.osm_polys is not None:
            from shapely.geometry import Point

            pt = Point(xy)
            for j in self.osm_sindex.query(pt, predicate="intersects"):
                idx = self.osm_polys.iloc[int(j)]["node_idx"]
                if idx is not None:
                    self.stats["contains"] += 1
                    return int(idx)

        pts = self.gazetteer.get(key)
        if pts:
            d = [np.hypot(p[0] - xy[0], p[1] - xy[1]) for p in pts]
            j = int(np.argmin(d))
            if d[j] <= MAX_ENDPOINT_DIST_M:
                hit = self._nearest(pts[j], kv)
                if hit is not None:
                    self.stats["gazetteer"] += 1
                    return hit

        hit = self._nearest(xy, kv, limit=NODE_SNAP_M)
        if hit is not None:
            self.stats["geometry"] += 1
            return hit

        self.stats["unresolved"] += 1
        return None


def _endpoints(geom):
    parts = [geom] if geom.geom_type == "LineString" else list(geom.geoms)
    parts = [p for p in parts if len(p.coords) >= 2]
    if not parts:
        return None
    return parts[0].coords[0][:2], parts[-1].coords[-1][:2]


def corridors_from(lines, res: Resolver, name_a: str, name_b: str,
                   kv_col: str, kv_scale: float, source: str) -> list[dict]:
    rows = []
    for _, ln in lines.iterrows():
        if _bad_name(ln.get(name_a)) or _bad_name(ln.get(name_b)):
            continue
        ends = _endpoints(ln.geometry)
        if ends is None:
            continue
        kv = pd.to_numeric(pd.Series([ln.get(kv_col)]), errors="coerce").iloc[0]
        kv = float(kv) * kv_scale if pd.notna(kv) and kv > 0 else np.nan

        best = None
        for p1, p2 in (ends, ends[::-1]):
            i1 = res.resolve(ln.get(name_a), p1, kv)
            i2 = res.resolve(ln.get(name_b), p2, kv)
            score = (i1 is not None) + (i2 is not None)
            if best is None or score > best[0]:
                best = (score, i1, i2)
        score, i1, i2 = best
        if score != 2 or i1 == i2:
            continue
        rows.append(
            {
                "source": res.ids[i1],
                "target": res.ids[i2],
                "rated_kv": kv,
                "asserted_by": source,
                "end_a": ln.get(name_a),
                "end_b": ln.get(name_b),
                "length_km": ln.geometry.length / 1000.0,
            }
        )
    return rows


def corridors_from_osm_geometry(lines, res: Resolver) -> list[dict]:
    """OSM corridors from containment alone, with no reliance on names.

    This is the part HIFLD cannot do. OSM maps substations as footprints, so a
    line whose endpoint falls inside a substation polygon is asserting a
    connection directly -- no name matching, no snap radius. It also reaches
    below 100 kV, where HIFLD's line layer stops and where 2,442 of our
    corridors are inference-only.
    """
    if res.osm_polys is None:
        return []
    from shapely.geometry import Point

    rows = []
    for _, ln in lines.iterrows():
        ends = _endpoints(ln.geometry)
        if ends is None:
            continue
        hits = []
        for xy in ends:
            hit = None
            for j in res.osm_sindex.query(Point(xy), predicate="intersects"):
                idx = res.osm_polys.iloc[int(j)]["node_idx"]
                if idx is not None:
                    hit = int(idx)
                    break
            hits.append(hit)
        if hits[0] is None or hits[1] is None or hits[0] == hits[1]:
            continue
        kv = pd.to_numeric(pd.Series([ln.get("voltage")]), errors="coerce").iloc[0]
        kv = float(kv) / 1000.0 if pd.notna(kv) and kv > 0 else np.nan
        rows.append(
            {
                "source": res.ids[hits[0]],
                "target": res.ids[hits[1]],
                "rated_kv": kv,
                "asserted_by": "osm_geom",
                "end_a": ln.get("name"),
                "end_b": None,
                "length_km": ln.geometry.length / 1000.0,
            }
        )
    return rows


def _split_osm_name(name):
    """OSM line names often encode both endpoints: 'A - B 500KV'."""
    s = str(name or "")
    s = re.sub(r"\b\d{2,3}\s*KV\b", " ", s, flags=re.I)
    for sep in (" - ", " -- ", "–", "—", " to "):
        if sep in s:
            a, b = s.split(sep, 1)
            if norm(a) and norm(b):
                return a.strip(), b.strip()
    return None, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.parse_args()

    hubs = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg").to_crs(C.CA_ALBERS_CRS)
    res = Resolver(hubs)
    print(f"{len(hubs):,} model nodes")

    rows: list[dict] = []

    if HIFLD_LINES.is_file():
        hl = gpd.read_file(HIFLD_LINES).to_crs(C.CA_ALBERS_CRS)
        got = corridors_from(hl, res, "SUB_1", "SUB_2", "VOLTAGE", 1.0, "hifld")
        rows += got
        print(f"  HIFLD: {len(hl):,} lines -> {len(got):,} asserted corridors")
    else:
        print(f"  {HIFLD_LINES} missing; run 08_07 first")

    if OSM_LINES.is_file():
        ol = gpd.read_file(OSM_LINES).to_crs(C.CA_ALBERS_CRS)
        ol[["end_a", "end_b"]] = ol["name"].map(
            lambda n: pd.Series(_split_osm_name(n))
        ).apply(pd.Series) if len(ol) else None
        ol = ol[ol["end_a"].notna()]
        # OSM voltage is in volts
        got = corridors_from(ol, res, "end_a", "end_b", "voltage", 1e-3, "osm")
        rows += got
        print(f"  OSM names: {len(ol):,} named lines -> {len(got):,} asserted corridors")

        all_lines = gpd.read_file(OSM_LINES).to_crs(C.CA_ALBERS_CRS)
        geo = corridors_from_osm_geometry(all_lines, res)
        rows += geo
        print(f"  OSM geometry: {len(all_lines):,} lines -> {len(geo):,} asserted corridors")
    else:
        print(f"  {OSM_LINES} missing (Overpass pull not finished); HIFLD only")

    if not rows:
        print("no asserted corridors produced")
        return

    df = pd.DataFrame(rows)
    df["pair"] = [tuple(sorted(p)) for p in zip(df["source"], df["target"])]
    agg = (
        df.groupby("pair", as_index=False)
        .agg(rated_kv=("rated_kv", "max"), n_circuits=("pair", "size"),
             asserted_by=("asserted_by", lambda s: ",".join(sorted(set(s)))),
             length_km=("length_km", "median"))
    )
    agg["source"] = [p[0] for p in agg["pair"]]
    agg["target"] = [p[1] for p in agg["pair"]]
    agg = agg.drop(columns=["pair"])

    C.ensure_dir(OUT_CSV.parent)
    agg.to_csv(OUT_CSV, index=False)

    print(f"\n  endpoint resolution: {res.stats}")
    print(f"  wrote {OUT_CSV}  ({len(agg):,} distinct asserted corridors)")

    edges_csv = C.MESO_DIR / "meso_edges.csv"
    if edges_csv.is_file():
        ex = pd.read_csv(edges_csv)
        have = {tuple(sorted((s, t))) for s, t in zip(ex["source"], ex["target"])}
        pairs = {tuple(sorted((s, t))) for s, t in zip(agg["source"], agg["target"])}
        print(f"  confirm an inferred corridor : {len(pairs & have):,}")
        print(f"  NEW, not inferred            : {len(pairs - have):,}")
        print(f"  inferred but not asserted    : {len(have - pairs):,} "
              f"(mostly sub-100 kV, which HIFLD does not cover)")


if __name__ == "__main__":
    main()
