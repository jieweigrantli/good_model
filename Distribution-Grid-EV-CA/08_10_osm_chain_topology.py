"""
08_10_osm_chain_topology.py

Corridors from OpenStreetMap's own node graph, for the sub-100 kV tier.

Why this is different from everything before it
-----------------------------------------------
Every corridor we have built so far below 100 kV is inferred: we snap a drawn
line's endpoint to whichever substation dot is nearest and hope. HIFLD fixes
that above 100 kV by naming both endpoints, but its line layer stops there,
leaving 2,391 of our sub-100 kV corridors (96%) with no published backing.

OSM is topological at source. A way references node *ids*, and two ways that
share a node id are connected -- that is an assertion by the mapper, not a
distance guess. Using only the exported geometry throws this away, which is
why containment alone recovered just 145 corridors from 43,841 lines: OSM
splits circuits into many short segments that end at towers, not substations,
so both ends of a single segment rarely land in a yard.

Chaining restores it. Ways are linked through shared node ids into connected
runs, substations are located by polygon containment, and each run is reduced
to corridors between the substations it actually reaches -- by minimum
spanning tree over routed distance, so a run touching many substations
produces a path rather than a clique.

Writes (merged into) :
  data/meso/asserted_corridors.csv
"""

from __future__ import annotations

import argparse
from collections import defaultdict

import geopandas as gpd
import networkx as nx
import numpy as np
import osmium
import pandas as pd
import shapely.wkb
from shapely.geometry import Point

import common as C

PBF = C.DATA_DIR / "osm" / "california-latest.osm.pbf"
OSM_SUBS = C.DATA_DIR / "osm" / "osm_substations_ca.gpkg"
OUT_CSV = C.MESO_DIR / "asserted_corridors.csv"

POWER_LINE = {"line", "minor_line"}
# A run touching more substations than this is a shared junction rather than a
# corridor; the spanning tree keeps it a path, but cap the work regardless.
MAX_TERMINALS = 24
NODE_SNAP_M = 2_000.0


class WayHandler(osmium.SimpleHandler):
    """Keep each power way's node ids, endpoints and voltage."""

    def __init__(self):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.ways: list[dict] = []

    def way(self, w):
        if w.tags.get("power") not in POWER_LINE:
            return
        refs = [n.ref for n in w.nodes]
        if len(refs) < 2:
            return
        try:
            geom = shapely.wkb.loads(self.wkb.create_linestring(w), hex=True)
        except Exception:  # noqa: BLE001
            return
        kv = pd.to_numeric(pd.Series([w.tags.get("voltage")]), errors="coerce").iloc[0]
        self.ways.append(
            {
                "refs": refs,
                "coords": list(geom.coords),
                "kv": float(kv) / 1000.0 if pd.notna(kv) and kv > 0 else np.nan,
                "length_m": geom.length,
            }
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    C.require_file(PBF, hint="Run 08_07/08_09 to download the extract first")
    C.require_file(OSM_SUBS, hint="Run 08_09_extract_osm_power.py first")

    print("Streaming power ways with their node references...")
    h = WayHandler()
    h.apply_file(str(PBF), locations=True, idx="flex_mem")
    print(f"  {len(h.ways):,} power ways")

    # --- substations -> model nodes ---------------------------------------
    hubs = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg").to_crs(C.CA_ALBERS_CRS)
    subs = gpd.read_file(OSM_SUBS).to_crs(C.CA_ALBERS_CRS)
    subs = subs[subs.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()

    from scipy.spatial import cKDTree

    tree = cKDTree(np.column_stack([hubs.geometry.x, hubs.geometry.y]))
    cent = subs.geometry.centroid
    d, idx = tree.query(np.column_stack([cent.x, cent.y]), k=1)
    subs["node_idx"] = np.where(d <= NODE_SNAP_M, idx, -1)
    subs = subs[subs["node_idx"] >= 0].reset_index(drop=True)
    sindex = subs.sindex
    hub_ids = hubs["hub_id"].to_numpy()
    print(f"  {len(subs):,} substation footprints matched to a model node")

    # --- graph over OSM node ids ------------------------------------------
    # Reproject way coordinates once (they arrive in WGS84).
    all_pts = gpd.GeoSeries(
        [Point(c[0], c[1]) for w in h.ways for c in (w["coords"][0], w["coords"][-1])],
        crs="EPSG:4326",
    ).to_crs(C.CA_ALBERS_CRS)

    g = nx.Graph()
    ends = []
    for i, w in enumerate(h.ways):
        a, b = w["refs"][0], w["refs"][-1]
        if a == b:
            continue
        if g.has_edge(a, b):
            if w["length_m"] < g[a][b]["length_m"]:
                g[a][b].update(length_m=w["length_m"], kv=w["kv"])
        else:
            g.add_edge(a, b, length_m=w["length_m"], kv=w["kv"])
        ends.append((a, all_pts.iloc[2 * i]))
        ends.append((b, all_pts.iloc[2 * i + 1]))
    print(f"  node graph: {g.number_of_nodes():,} nodes, {g.number_of_edges():,} edges")

    # --- which OSM node ids sit inside a substation? -----------------------
    terminal: dict[int, int] = {}
    for ref, pt in ends:
        if ref in terminal:
            continue
        for j in sindex.query(pt, predicate="intersects"):
            terminal[ref] = int(subs.iloc[int(j)]["node_idx"])
            break
    print(f"  {len(terminal):,} way endpoints fall inside a mapped substation")

    # --- reduce each connected run to corridors between its substations ----
    rows = []
    for comp in nx.connected_components(g):
        terms = [n for n in comp if n in terminal]
        # Distinct model nodes only; several OSM refs can share a yard.
        seen, uniq = set(), []
        for n in terms:
            if terminal[n] not in seen:
                seen.add(terminal[n])
                uniq.append(n)
        if len(uniq) < 2:
            continue
        if len(uniq) > MAX_TERMINALS:
            uniq = uniq[:MAX_TERMINALS]

        sub = g.subgraph(comp)
        tg = nx.Graph()
        tg.add_nodes_from(uniq)
        for n in uniq:
            dist = nx.single_source_dijkstra_path_length(sub, n, weight="length_m")
            for other in uniq:
                if other == n or other not in dist:
                    continue
                w = float(dist[other])
                if not tg.has_edge(n, other) or w < tg[n][other]["weight"]:
                    tg.add_edge(n, other, weight=w)
        for a, b in nx.minimum_spanning_tree(tg).edges():
            ia, ib = terminal[a], terminal[b]
            if ia == ib:
                continue
            path = nx.shortest_path(sub, a, b, weight="length_m")
            kvs = [
                sub[u][v]["kv"]
                for u, v in zip(path[:-1], path[1:])
                if not np.isnan(sub[u][v]["kv"])
            ]
            rows.append(
                {
                    "source": hub_ids[ia],
                    "target": hub_ids[ib],
                    "rated_kv": min(kvs) if kvs else np.nan,
                    "asserted_by": "osm_chain",
                    "end_a": None,
                    "end_b": None,
                    "length_km": tg[a][b]["weight"] / 1000.0,
                }
            )

    new = pd.DataFrame(rows)
    print(f"  {len(new):,} chained corridors")
    if new.empty:
        return

    if OUT_CSV.is_file():
        old = pd.read_csv(OUT_CSV)
    else:
        old = pd.DataFrame(columns=new.columns)

    both = pd.concat([old, new], ignore_index=True)
    both["pair"] = [tuple(sorted((s, t))) for s, t in zip(both["source"], both["target"])]
    agg = (
        both.groupby("pair", as_index=False)
        .agg(
            rated_kv=("rated_kv", "max"),
            n_circuits=("pair", "size"),
            asserted_by=("asserted_by", lambda s: ",".join(sorted(set(map(str, s))))),
            length_km=("length_km", "median"),
        )
    )
    agg["source"] = [p[0] for p in agg["pair"]]
    agg["target"] = [p[1] for p in agg["pair"]]
    agg.drop(columns=["pair"]).to_csv(OUT_CSV, index=False)

    print(f"  wrote {OUT_CSV}  ({len(agg):,} distinct asserted corridors total)")
    kv = pd.to_numeric(agg["rated_kv"], errors="coerce")
    bands = pd.cut(kv, [0, 100, 230, 1000], labels=["<100kV", "100-229kV", ">=230kV"])
    print("  by voltage:", bands.value_counts(dropna=False).to_dict())


if __name__ == "__main__":
    main()
