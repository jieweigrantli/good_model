"""
09_01_build_ca_meso_grid.py

Build the California meso delivery layer from GRIP substations (+ gateways),
attach TransmissionLines for transfer-capacity heuristics, assign parent CA
BA regions, and attach seasonal EV load profiles at each delivery node.

Default: one GOOD node per substation (abstract-aligned, no k-means).
Optional aggregation: --aggregate N (e.g. 80) for screening.

Outputs under data/meso/:
  meso_nodes.csv, meso_edges.csv, meso_hubs.gpkg
  seasonal/<week>/meso_hourly_kW.npy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

import common as C

# Approximate MW→W capacity from rated kV (screening heuristic)
KV_TO_MW = {500: 1500, 230: 400, 115: 150, 70: 80, 60: 60}


def _kv_capacity_w(rated_kv: float) -> float:
    kv = float(rated_kv) if pd.notna(rated_kv) else 115.0
    # nearest key
    keys = np.array(list(KV_TO_MW.keys()))
    nearest = int(keys[np.argmin(np.abs(keys - kv))])
    return KV_TO_MW[nearest] * 1e6  # W


def _ba_centroids() -> dict[str, Point]:
    """Rough CA BA centroids (lon, lat) for parent assignment."""
    # approximate population/load centers
    coords = {
        "WEC_CALN": (-122.0, 38.0),   # Bay Area / north
        "WEC_BANC": (-121.5, 38.6),   # Sacramento
        "WECC_SCE": (-117.5, 34.0),   # LA basin
        "WEC_LADW": (-118.3, 34.1),
        "WEC_SDGE": (-117.1, 32.8),
        "WECC_IID": (-115.5, 33.0),
    }
    return {k: Point(xy) for k, xy in coords.items()}


def load_substations_with_load() -> gpd.GeoDataFrame:
    grip = gpd.read_file(C.GRIP_ED_SUBSTATIONS).to_crs(26910)
    grip["substation_id"] = grip["Substati00"].astype(str)
    grip["substation_name"] = grip["Substation"].astype(str)
    grip["source"] = "grip"

    rank = pd.read_csv(C.MESO_DIR / "substation_ev_rank_mean.csv")
    rank["substation_id"] = rank["substation_id"].astype(str)
    gdf = grip.merge(
        rank[["substation_id", "mean_week_kwh", "mean_peak_kW"]],
        on="substation_id",
        how="left",
    )
    gdf["mean_week_kwh"] = gdf["mean_week_kwh"].fillna(0.0)
    gdf["mean_peak_kW"] = gdf["mean_peak_kW"].fillna(0.0)
    keep = ["substation_id", "substation_name", "source", "mean_week_kwh", "mean_peak_kW", "geometry"]
    gdf = gdf[keep].copy()

    # Include BA gateway / external stations present in the TAZ map but not in GRIP
    mapping = pd.read_csv(C.MAPPING_DIR / "taz_to_substation.csv")
    mapping["substation_id"] = mapping["substation_id"].astype(str)
    extra_ids = set(mapping["substation_id"]) - set(gdf["substation_id"])
    if extra_ids:
        from shapely.geometry import Point

        meta = (
            mapping[mapping["substation_id"].isin(extra_ids)]
            [["substation_id", "substation_name", "source"]]
            .drop_duplicates("substation_id")
        )
        # place gateways at BA centroids; others at (0,0) fallback then overwritten
        ba_ll = {
            "BA_GW_WEC_CALN": (-122.0, 38.0),
            "BA_GW_WEC_BANC": (-121.5, 38.6),
            "BA_GW_WECC_SCE": (-117.5, 34.0),
            "BA_GW_WEC_LADW": (-118.3, 34.1),
            "BA_GW_WEC_SDGE": (-117.1, 32.8),
            "BA_GW_WECC_IID": (-115.5, 33.0),
        }
        rows = []
        en = rank.set_index("substation_id")
        for _, r in meta.iterrows():
            sid = r["substation_id"]
            lon, lat = ba_ll.get(sid, (-120.0, 37.0))
            rows.append(
                {
                    "substation_id": sid,
                    "substation_name": r["substation_name"],
                    "source": r["source"],
                    "mean_week_kwh": float(en["mean_week_kwh"].get(sid, 0.0))
                    if sid in en.index
                    else 0.0,
                    "mean_peak_kW": float(en["mean_peak_kW"].get(sid, 0.0))
                    if sid in en.index
                    else 0.0,
                    "geometry": Point(lon, lat),
                }
            )
        extra = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(26910)
        gdf = pd.concat([gdf, extra[keep]], ignore_index=True)
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs="EPSG:26910")
        print(f"  added {len(extra)} non-GRIP stations (gateways/external)")

    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        hifld = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG).to_crs(26910)
        id_col = "OBJECTID" if "OBJECTID" in hifld.columns else (
            "ID" if "ID" in hifld.columns else None
        )
        if id_col is not None:
            hifld["substation_id"] = "CEC_" + hifld[id_col].astype(str)
            hifld = hifld[hifld["substation_id"].isin(extra_ids)].copy()
            if len(hifld):
                hifld["substation_name"] = hifld.get("Name", hifld["substation_id"]).astype(str)
                hifld["source"] = "hifld"
                hifld["mean_week_kwh"] = 0.0
                hifld["mean_peak_kW"] = 0.0
                gdf = pd.concat([gdf, hifld[keep]], ignore_index=True)
                gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs="EPSG:26910")
    return gdf


def substations_as_nodes(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """1:1 — each substation is its own meso delivery node (abstract default)."""
    out = gdf.copy()
    out["hub_id"] = out["substation_id"].map(lambda s: f"SUB_{s}")
    hub_rows = []
    for _, r in out.iterrows():
        hub_rows.append(
            {
                "hub_id": r["hub_id"],
                "substation_id": r["substation_id"],
                "substation_name": r.get("substation_name", ""),
                "n_substations": 1,
                "mean_week_kwh": float(r["mean_week_kwh"]),
                "geometry": r.geometry,
            }
        )
    hubs = gpd.GeoDataFrame(hub_rows, geometry="geometry", crs=gdf.crs)
    return out, hubs


def cluster_hubs(gdf: gpd.GeoDataFrame, n_hubs: int) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Optional k-means aggregation on projected coordinates."""
    from sklearn.cluster import KMeans

    xy = np.column_stack([gdf.geometry.x, gdf.geometry.y])
    n_hubs = min(n_hubs, len(gdf))
    km = KMeans(n_clusters=n_hubs, random_state=42, n_init=10)
    labels = km.fit_predict(xy)
    out = gdf.copy()
    out["hub_id"] = [f"MESO_{i:03d}" for i in labels]
    centers = km.cluster_centers_
    hub_rows = []
    for i in range(n_hubs):
        members = out[out["hub_id"] == f"MESO_{i:03d}"]
        hub_rows.append(
            {
                "hub_id": f"MESO_{i:03d}",
                "n_substations": len(members),
                "mean_week_kwh": float(members["mean_week_kwh"].sum()),
                "geometry": Point(centers[i, 0], centers[i, 1]),
            }
        )
    hubs = gpd.GeoDataFrame(hub_rows, geometry="geometry", crs=gdf.crs)
    return out, hubs


def assign_parent_ba(hubs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    ba_pts = _ba_centroids()
    ba_gdf = gpd.GeoDataFrame(
        {"parent_ba": list(ba_pts.keys()), "geometry": list(ba_pts.values())},
        crs="EPSG:4326",
    ).to_crs(hubs.crs)
    joined = gpd.sjoin_nearest(hubs, ba_gdf[["parent_ba", "geometry"]], how="left")
    joined = joined.drop(columns=[c for c in joined.columns if c.startswith("index_")])
    # drop duplicate geometry cols if any
    if "parent_ba" not in joined.columns:
        raise RuntimeError("parent_ba assignment failed")
    return joined.drop_duplicates(subset=["hub_id"])


def build_edges(hubs: gpd.GeoDataFrame, lines: gpd.GeoDataFrame) -> pd.DataFrame:
    """Snap each transmission line endpoints to nearest hubs; aggregate capacity."""
    hubs = hubs.set_index("hub_id", drop=False)
    # line endpoints
    rows = []
    for _, ln in lines.iterrows():
        geom = ln.geometry
        if geom is None or geom.is_empty:
            continue
        # use boundary coords
        coords = list(geom.coords) if geom.geom_type == "LineString" else list(geom.geoms[0].coords)
        if len(coords) < 2:
            continue
        a = Point(coords[0])
        b = Point(coords[-1])
        # nearest hubs
        # quick: compute distances to all hubs (n_lines*n_hubs ~ 200k ok)
        hx = hubs.geometry.x.to_numpy()
        hy = hubs.geometry.y.to_numpy()
        ids = hubs["hub_id"].to_numpy()
        da = (hx - a.x) ** 2 + (hy - a.y) ** 2
        db = (hx - b.x) ** 2 + (hy - b.y) ** 2
        ia, ib = int(np.argmin(da)), int(np.argmin(db))
        if ia == ib:
            continue
        cap = _kv_capacity_w(ln.get("RATEDKV", 115))
        u, v = sorted([ids[ia], ids[ib]])
        rows.append({"source": u, "target": v, "installed_capacity_W": cap, "rated_kv": ln.get("RATEDKV")})
    if not rows:
        return pd.DataFrame(columns=["source", "target", "installed_capacity_W", "n_lines"])
    ed = pd.DataFrame(rows)
    agg = (
        ed.groupby(["source", "target"], as_index=False)
        .agg(installed_capacity_W=("installed_capacity_W", "sum"), n_lines=("rated_kv", "count"))
    )
    return agg


def aggregate_seasonal_loads(station_hub: gpd.GeoDataFrame) -> None:
    mapping = pd.read_csv(C.MAPPING_DIR / "taz_to_substation.csv")
    mapping["TAZ"] = mapping["TAZ"].astype(int)
    mapping["substation_id"] = mapping["substation_id"].astype(str)
    hub_map = station_hub[["substation_id", "hub_id"]].copy()
    hub_map["substation_id"] = hub_map["substation_id"].astype(str)
    mapping = mapping.merge(hub_map, on="substation_id", how="left")
    taz_ids = np.load(C.MESO_DIR / "taz_ids.npy")
    hubs_order = sorted(station_hub["hub_id"].unique())
    hub_index = {h: i for i, h in enumerate(hubs_order)}

    for week in C.SEASONAL_WEEKS:
        name = week["name"]
        taz_hourly = np.load(C.MESO_DIR / "seasonal" / name / "taz_hourly_kW.npy")
        sub_for_taz = (
            mapping.set_index("TAZ").loc[taz_ids, "hub_id"].to_numpy()
        )
        meso = np.zeros((len(hubs_order), taz_hourly.shape[1]), dtype=np.float64)
        for ti, hid in enumerate(sub_for_taz):
            if pd.isna(hid):
                continue
            meso[hub_index[hid]] += taz_hourly[ti]
        wdir = C.ensure_dir(C.MESO_DIR / "seasonal" / name)
        np.save(wdir / "meso_hub_ids.npy", np.array(hubs_order))
        np.save(wdir / "meso_hourly_kW.npy", meso.astype(np.float32))
        print(f"  {name}: meso load peak={meso.sum(0).max()/1e6:.2f} GW across {len(hubs_order)} hubs")


def main(aggregate: int | None = None) -> None:
    print("Loading substations + EV ranking...")
    stations = load_substations_with_load()
    print(f"  {len(stations)} stations")

    if aggregate is None or aggregate <= 0 or aggregate >= len(stations):
        print(f"Using substation-level nodes (n={len(stations)}; no clustering)...")
        station_hub, hubs = substations_as_nodes(stations)
    else:
        print(f"Aggregating into {aggregate} hubs (k-means)...")
        try:
            station_hub, hubs = cluster_hubs(stations, aggregate)
        except ImportError:
            print("sklearn not available — using grid quantile bins instead")
            n = int(np.sqrt(aggregate))
            xs = pd.qcut(stations.geometry.x, q=n, labels=False, duplicates="drop")
            ys = pd.qcut(stations.geometry.y, q=n, labels=False, duplicates="drop")
            station_hub = stations.copy()
            station_hub["hub_id"] = [f"MESO_{int(a):02d}{int(b):02d}" for a, b in zip(xs, ys)]
            hub_rows = []
            for hid, g in station_hub.groupby("hub_id"):
                hub_rows.append(
                    {
                        "hub_id": hid,
                        "n_substations": len(g),
                        "mean_week_kwh": float(g["mean_week_kwh"].sum()),
                        "geometry": Point(g.geometry.x.mean(), g.geometry.y.mean()),
                    }
                )
            hubs = gpd.GeoDataFrame(hub_rows, geometry="geometry", crs=stations.crs)

    hubs = assign_parent_ba(hubs)
    print(hubs["parent_ba"].value_counts().to_string())

    print("Building edges from GRIP TransmissionLines...")
    lines = gpd.read_file(C.GRIP_TRANSMISSION_LINES).to_crs(hubs.crs)
    edges = build_edges(hubs, lines)
    print(f"  {len(edges)} aggregated hub-hub corridors")

    C.ensure_dir(C.MESO_DIR)
    hubs.drop(columns=["geometry"]).to_csv(C.MESO_DIR / "meso_nodes.csv", index=False)
    edges.to_csv(C.MESO_DIR / "meso_edges.csv", index=False)
    hubs.to_file(C.MESO_DIR / "meso_hubs.gpkg", driver="GPKG")
    station_hub.drop(columns=["geometry"]).to_csv(
        C.MESO_DIR / "substation_to_hub.csv", index=False
    )
    meta = {
        "resolution": "substation" if hubs["n_substations"].max() == 1 else "aggregated",
        "n_nodes": int(len(hubs)),
        "aggregate_requested": aggregate,
    }
    (C.MESO_DIR / "meso_resolution.json").write_text(
        __import__("json").dumps(meta, indent=2), encoding="utf-8"
    )

    print("Aggregating seasonal EV loads to hubs...")
    # need hub_id on stations used in mapping — remap via substation
    aggregate_seasonal_loads(station_hub)

    # interface capacity parent BA ↔ hub: proportional to hub peak EV
    iface = hubs[["hub_id", "parent_ba", "mean_week_kwh"]].copy()
    # default interface MW: max(50, peak-related)
    iface["interface_capacity_W"] = np.maximum(
        50e6, iface["mean_week_kwh"] / (7 * 24) * 1e3 * 2.0
    )  # rough: 2x mean kW → W
    iface.to_csv(C.MESO_DIR / "meso_ba_interfaces.csv", index=False)
    print(f"Wrote meso artifacts under {C.MESO_DIR} ({meta['resolution']}, n={meta['n_nodes']})")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    parser = argparse.ArgumentParser(description="Build CA meso / substation delivery layer")
    parser.add_argument(
        "--aggregate",
        type=int,
        default=None,
        help="If set (e.g. 80), k-means-aggregate substations into N hubs; "
        "default is one node per substation",
    )
    args = parser.parse_args()
    main(aggregate=args.aggregate)
