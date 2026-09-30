"""
Build TAZ path / distance tables for the Distribution-Grid-EV-CA pipeline.

Reads the CSTDM highway link layer (``CSTDM_sf.gpkg``), snaps TAZ centroids to
network nodes, runs shortest-path routing (Dijkstra), maps links to TAZ12 zones,
and writes chunked CSVs compatible with ``02_04_get_EV_trips.py`` and
``03_02_charging_events_LD_ETM.py``.

Output columns: ``from``, ``to``, ``distance`` (meters, OD total), ``TAZ12,``
(note trailing comma matches the R export).

ETM inbound trips use external zone IDs 1--51 as ``from``; routing starts at a
California entry TAZ inferred from the trip ``Int`` field (CA Rem / SF / Sac / LA / SD).

Example
-------
    python build_taz_path_skims.py --trip-set ld --test
    python build_taz_path_skims.py --trip-set etm
    python build_taz_path_skims.py --trip-set both
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

BASE_DIR = Path(__file__).resolve().parent
NETWORK_GPKG = BASE_DIR / "data" / "shps" / "CSTDM_sf.gpkg"
TAZ_SHP = BASE_DIR / "data" / "shps" / "taz_id.shp"
TAZ_CENTROIDS = BASE_DIR / "data" / "shps" / "TAZ_centroid_sf.gpkg"
LDPTM_TRIPS = BASE_DIR / "data" / "mobility_data" / "CSTDM" / "LDPTM" / "LDPTM_Trips.csv"
ETM_TRIPS = BASE_DIR / "data" / "mobility_data" / "CSTDM" / "ETM" / "trips_Ext.csv"

OUT_LD = BASE_DIR / "data" / "mobility_data" / "TAZ_distance" / "parsed_100000_LDPTM"
OUT_ETM = BASE_DIR / "data" / "mobility_data" / "TAZ_distance" / "parsed_100000_ETM"

METERS_PER_MILE = 1609.344
DEFAULT_CHUNK_ROWS = 100_000
# California internal TAZ IDs in CSTDM start at 100; ETM origins 1--51 are external zones.
CA_TAZ_MIN = 100
# Representative California entry TAZ per ETM ``Int`` region (from inbound-trip J modes).
INT_GATEWAY_TAZ: dict[str, int] = {
    "CA Rem": 5612,
    "CA SF": 1481,
    "CA Sac": 263,
    "CA LA": 5148,
    "CA SD": 6670,
}


def _load_od_pairs(trip_set: str, max_pairs: int | None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if trip_set in ("ld", "both"):
        ld = pd.read_csv(LDPTM_TRIPS, usecols=["I", "J"])
        frames.append(ld.rename(columns={"I": "from", "J": "to"}))
    if trip_set in ("etm", "both"):
        etm = pd.read_csv(ETM_TRIPS, usecols=["I", "J", "DPurp"])
        etm = etm.loc[etm["DPurp"] == "I", ["I", "J"]]
        frames.append(etm.rename(columns={"I": "from", "J": "to"}))
    if not frames:
        raise ValueError(f"Unknown trip_set: {trip_set}")

    pairs = pd.concat(frames, ignore_index=True).drop_duplicates()
    pairs["from"] = pairs["from"].astype(int)
    pairs["to"] = pairs["to"].astype(int)
    if max_pairs is not None and len(pairs) > max_pairs:
        pairs = pairs.iloc[:max_pairs].copy()
    return pairs


def _build_etm_gateways() -> tuple[dict[int, int], dict[tuple[int, int], int]]:
    """Map external zone I (and optionally each I-J pair) to a CA entry TAZ12."""
    etm = pd.read_csv(ETM_TRIPS, usecols=["I", "J", "DPurp", "Int"])
    inbound = etm.loc[etm["DPurp"] == "I", ["I", "J", "Int"]]
    by_i = inbound.groupby("I")["Int"].agg(lambda s: s.mode().iloc[0])
    gateway_i = {
        int(ext): INT_GATEWAY_TAZ[str(region)]
        for ext, region in by_i.items()
        if str(region) in INT_GATEWAY_TAZ
    }
    by_ij = inbound.groupby(["I", "J"])["Int"].agg(lambda s: s.mode().iloc[0])
    gateway_ij = {
        (int(i), int(j)): INT_GATEWAY_TAZ[str(region)]
        for (i, j), region in by_ij.items()
        if str(region) in INT_GATEWAY_TAZ
    }
    return gateway_i, gateway_ij


def _build_graph(links: gpd.GeoDataFrame) -> nx.DiGraph:
    graph = nx.DiGraph()
    for a, b, dist in zip(
        links["A"].astype(int), links["B"].astype(int), links["DISTANCE"].astype(float)
    ):
        if dist <= 0:
            continue
        a, b = int(a), int(b)
        if graph.has_edge(a, b):
            if dist < graph[a][b]["weight"]:
                graph[a][b]["weight"] = dist
        else:
            graph.add_edge(a, b, weight=dist)
    return graph


def _node_coordinates(links: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Return (node_ids, coords) averaged from link endpoint geometries."""
    accum: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for geom, a, b in zip(links.geometry, links["A"], links["B"]):
        x0, y0 = geom.coords[0]
        x1, y1 = geom.coords[-1]
        accum[int(a)].append((x0, y0))
        accum[int(b)].append((x1, y1))
    node_ids = np.array(sorted(accum), dtype=np.int64)
    coords = np.array(
        [(np.mean([p[0] for p in pts]), np.mean([p[1] for p in pts])) for pts in (accum[n] for n in node_ids)],
        dtype=np.float64,
    )
    return node_ids, coords


def _snap_taz_to_nodes(centroids: gpd.GeoDataFrame, node_ids: np.ndarray, coords: np.ndarray) -> dict[int, int]:
    tree = cKDTree(coords)
    xy = np.column_stack([centroids.geometry.x.to_numpy(), centroids.geometry.y.to_numpy()])
    _, idx = tree.query(xy)
    return {int(taz): int(node_ids[i]) for taz, i in zip(centroids["TAZ12"].astype(int), idx)}


def _assign_link_taz(links: gpd.GeoDataFrame, taz: gpd.GeoDataFrame) -> dict[tuple[int, int], int]:
    mid = links.copy()
    mid["geometry"] = links.geometry.interpolate(0.5, normalized=True)
    joined = gpd.sjoin(
        mid[["A", "B", "geometry"]],
        taz[["TAZ12", "geometry"]],
        how="left",
        predicate="within",
    )
    missing = joined["TAZ12"].isna()
    if missing.any():
        nearest = gpd.sjoin_nearest(
            mid.loc[missing, ["A", "B", "geometry"]],
            taz[["TAZ12", "geometry"]],
            how="left",
        )
        joined.loc[missing, "TAZ12"] = nearest["TAZ12"].to_numpy()

    edge_taz: dict[tuple[int, int], int] = {}
    for row in joined.itertuples(index=False):
        edge_taz[(int(row.A), int(row.B))] = int(row.TAZ12)
    return edge_taz


def _path_from_predecessors(
    pred: dict[int, list[int]], source: int, target: int
) -> list[int] | None:
    if source == target:
        return [source]
    path: list[int] = []
    node = target
    while node != source:
        path.append(node)
        preds = pred.get(node)
        if not preds:
            return None
        node = preds[0]
    path.append(source)
    path.reverse()
    return path


def _taz_sequence_along_path(
    node_path: list[int],
    edge_taz: dict[tuple[int, int], int],
    origin_taz: int,
    dest_taz: int,
) -> list[int]:
    tazs: list[int] = []
    for u, v in zip(node_path[:-1], node_path[1:]):
        tz = edge_taz.get((u, v))
        if tz is None:
            continue
        if not tazs or tazs[-1] != tz:
            tazs.append(tz)
    if not tazs or tazs[0] != origin_taz:
        tazs.insert(0, origin_taz)
    if tazs[-1] != dest_taz:
        tazs.append(dest_taz)
    else:
        # ensure destination appears even if duplicate interior
        if dest_taz not in tazs:
            tazs.append(dest_taz)
    return tazs


def _route_pairs(
    pairs: pd.DataFrame,
    graph: nx.DiGraph,
    taz_to_node: dict[int, int],
    edge_taz: dict[tuple[int, int], int],
    writer: _ChunkWriter,
    show_progress: bool,
) -> dict[str, int]:
    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover
        tqdm = None

    by_origin: dict[int, list[int]] = defaultdict(list)
    for origin, dest in pairs[["from", "to"]].itertuples(index=False, name=None):
        by_origin[int(origin)].append(int(dest))

    stats = {"ok": 0, "same_zone": 0, "missing_connector": 0, "unreachable": 0}

    origin_iter = sorted(by_origin.items())
    if show_progress and tqdm is not None:
        origin_iter = tqdm(origin_iter, desc="Origins (Dijkstra)", unit="TAZ")

    for origin_taz, dest_list in origin_iter:
        dest_unique = sorted(set(dest_list))
        source_node = taz_to_node.get(origin_taz)
        if source_node is None:
            stats["missing_connector"] += len(dest_unique)
            continue

        pred, dist = nx.dijkstra_predecessor_and_distance(
            graph, source_node, weight="weight"
        )

        for dest_taz in dest_unique:
            if origin_taz == dest_taz:
                writer.add(
                    {
                        "from": origin_taz,
                        "to": dest_taz,
                        "distance": 0.0,
                        "TAZ12,": origin_taz,
                    }
                )
                stats["same_zone"] += 1
                continue

            target_node = taz_to_node.get(dest_taz)
            if target_node is None:
                stats["missing_connector"] += 1
                continue

            if target_node not in dist:
                stats["unreachable"] += 1
                continue

            node_path = _path_from_predecessors(pred, source_node, target_node)
            if not node_path:
                stats["unreachable"] += 1
                continue

            distance_m = float(dist[target_node]) * METERS_PER_MILE
            taz_path = _taz_sequence_along_path(
                node_path, edge_taz, origin_taz, dest_taz
            )
            for taz in taz_path:
                writer.add(
                    {
                        "from": origin_taz,
                        "to": dest_taz,
                        "distance": distance_m,
                        "TAZ12,": taz,
                    }
                )
            stats["ok"] += 1

    return stats


def _route_etm_pairs(
    pairs: pd.DataFrame,
    graph: nx.DiGraph,
    taz_to_node: dict[int, int],
    edge_taz: dict[tuple[int, int], int],
    gateway_i: dict[int, int],
    gateway_ij: dict[tuple[int, int], int],
    writer: _ChunkWriter,
    show_progress: bool,
) -> dict[str, int]:
    """Route inbound ETM trips from CA entry TAZ (by Int region) to destination J."""
    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover
        tqdm = None

    stats = {"ok": 0, "same_zone": 0, "missing_connector": 0, "unreachable": 0}
    by_entry: dict[int, list[tuple[int, int]]] = defaultdict(list)

    for origin, dest in pairs[["from", "to"]].itertuples(index=False, name=None):
        ext_i, dest_j = int(origin), int(dest)
        entry_taz = gateway_ij.get((ext_i, dest_j), gateway_i.get(ext_i))
        if entry_taz is None or entry_taz not in taz_to_node:
            stats["missing_connector"] += 1
            continue
        by_entry[entry_taz].append((ext_i, dest_j))

    entry_iter = sorted(by_entry.items())
    if show_progress and tqdm is not None:
        entry_iter = tqdm(entry_iter, desc="CA entry TAZ (Dijkstra)", unit="entry")

    for entry_taz, od_list in entry_iter:
        source_node = taz_to_node[entry_taz]
        pred, dist = nx.dijkstra_predecessor_and_distance(
            graph, source_node, weight="weight"
        )

        for ext_i, dest_j in od_list:
            if entry_taz == dest_j:
                writer.add(
                    {
                        "from": ext_i,
                        "to": dest_j,
                        "distance": 0.0,
                        "TAZ12,": dest_j,
                    }
                )
                stats["same_zone"] += 1
                continue

            target_node = taz_to_node.get(dest_j)
            if target_node is None:
                stats["missing_connector"] += 1
                continue
            if target_node not in dist:
                stats["unreachable"] += 1
                continue

            node_path = _path_from_predecessors(pred, source_node, target_node)
            if not node_path:
                stats["unreachable"] += 1
                continue

            distance_m = float(dist[target_node]) * METERS_PER_MILE
            taz_path = _taz_sequence_along_path(
                node_path, edge_taz, entry_taz, dest_j
            )
            for taz in taz_path:
                writer.add(
                    {
                        "from": ext_i,
                        "to": dest_j,
                        "distance": distance_m,
                        "TAZ12,": taz,
                    }
                )
            stats["ok"] += 1

    return stats


class _ChunkWriter:
    def __init__(self, out_dir: Path, chunk_size: int) -> None:
        self.out_dir = out_dir
        self.chunk_size = chunk_size
        self.buffer: list[dict[str, object]] = []
        self.part = 0
        self.total_rows = 0
        out_dir.mkdir(parents=True, exist_ok=True)

    def add(self, row: dict[str, object]) -> None:
        self.buffer.append(row)
        if len(self.buffer) >= self.chunk_size:
            self.flush()

    def extend(self, rows: list[dict[str, object]]) -> None:
        for row in rows:
            self.add(row)

    def flush(self) -> None:
        if not self.buffer:
            return
        self.part += 1
        path = self.out_dir / f"part_{self.part:05d}.csv"
        pd.DataFrame(self.buffer).to_csv(path, index=False)
        self.total_rows += len(self.buffer)
        self.buffer.clear()

    def close(self) -> None:
        self.flush()


def build_for_trip_set(
    trip_set: str,
    out_dir: Path,
    *,
    max_pairs: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_ROWS,
    show_progress: bool = True,
) -> dict[str, int]:
    if not NETWORK_GPKG.is_file():
        raise SystemExit(f"Missing network: {NETWORK_GPKG}")
    if not TAZ_SHP.is_file():
        raise SystemExit(f"Missing TAZ polygons: {TAZ_SHP}")

    centroids_path = TAZ_CENTROIDS if TAZ_CENTROIDS.is_file() else TAZ_SHP
    pairs = _load_od_pairs(trip_set, max_pairs)
    print(f"[{trip_set}] unique OD pairs: {len(pairs):,}")

    links = gpd.read_file(NETWORK_GPKG)
    taz = gpd.read_file(TAZ_SHP)[["TAZ12", "geometry"]]
    links = links.to_crs(taz.crs)

    print("Building graph and lookups...")
    graph = _build_graph(links)
    node_ids, coords = _node_coordinates(links)
    centroids = gpd.read_file(centroids_path)[["TAZ12", "geometry"]].to_crs(taz.crs)
    taz_to_node = _snap_taz_to_nodes(centroids, node_ids, coords)
    edge_taz = _assign_link_taz(links, taz)
    print(
        f"  nodes={graph.number_of_nodes():,} edges={graph.number_of_edges():,} "
        f"link_to_TAZ={len(edge_taz):,}"
    )

    writer = _ChunkWriter(out_dir, chunk_size)
    if trip_set == "etm":
        gateway_i, gateway_ij = _build_etm_gateways()
        print(
            f"  ETM external zones: {len(gateway_i)} with CA entry TAZ "
            f"({len(gateway_ij):,} I-J overrides)"
        )
        stats = _route_etm_pairs(
            pairs,
            graph,
            taz_to_node,
            edge_taz,
            gateway_i,
            gateway_ij,
            writer,
            show_progress=show_progress,
        )
    else:
        stats = _route_pairs(
            pairs, graph, taz_to_node, edge_taz, writer, show_progress=show_progress
        )
    writer.close()
    n_written = writer.total_rows
    print(
        f"Wrote {n_written:,} rows to {out_dir} "
        f"(OD ok={stats['ok']:,}, same_zone={stats['same_zone']:,}, "
        f"unreachable={stats['unreachable']:,}, missing_connector={stats['missing_connector']:,})"
    )
    return stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build parsed TAZ path CSVs from CSTDM_sf.gpkg + TAZ polygons."
    )
    parser.add_argument(
        "--trip-set",
        choices=("ld", "etm", "both"),
        default="both",
        help="Which trip tables define the OD pair list (default: both).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_ROWS,
        help=f"Rows per output CSV (default: {DEFAULT_CHUNK_ROWS}).",
    )
    parser.add_argument(
        "--max-pairs",
        type=int,
        default=None,
        help="Limit OD pairs (debug / smoke test).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Shortcut for --max-pairs 500 --trip-set ld.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bar.",
    )
    args = parser.parse_args(argv)

    max_pairs = 500 if args.test else args.max_pairs
    show_progress = not args.no_progress

    if args.trip_set in ("ld", "both"):
        build_for_trip_set(
            "ld",
            OUT_LD,
            max_pairs=max_pairs,
            chunk_size=args.chunk_size,
            show_progress=show_progress,
        )
    if args.trip_set in ("etm", "both"):
        build_for_trip_set(
            "etm",
            OUT_ETM,
            max_pairs=max_pairs,
            chunk_size=args.chunk_size,
            show_progress=show_progress,
        )


if __name__ == "__main__":
    main()
