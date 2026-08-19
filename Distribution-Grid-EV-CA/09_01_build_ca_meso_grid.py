"""
09_01_build_ca_meso_grid.py

Unclustered California substation graph:
  nodes  = GRIP EDSubstations + HIFLD non-PG&E substations used in the TAZ map
  edges  = TransmissionLines.shp (plus optional HIFLD lines) with NTC limits
  assets = WECC CA generators, candidate BESS, named interties snapped to nodes

Default: one GOOD node per substation (no clustering).
Optional screening only: --aggregate N

Writes:
  data/meso/ca_substation_network.json
  data/meso/meso_nodes.csv, meso_edges.csv, meso_hubs.gpkg
  data/meso/meso_ba_interfaces.csv
  seasonal/<week>/meso_hourly_kW.npy  (EV) and meso_hourly_total_kW.npy
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

import common as C

HIFLD_LINES_QUERY = (
    "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
    "Electric_Power_Transmission_Lines/FeatureServer/0/query"
)


def _to_albers(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf.to_crs(C.CA_ALBERS_CRS)


def load_substation_nodes() -> gpd.GeoDataFrame:
    rank_path = C.MESO_DIR / "substation_ev_rank_mean.csv"
    rank = (
        pd.read_csv(rank_path)
        if rank_path.is_file()
        else pd.DataFrame(columns=["substation_id", "mean_week_kwh", "mean_peak_kW"])
    )
    rank["substation_id"] = rank["substation_id"].astype(str)
    mapping = pd.read_csv(C.TAZ_TO_SUBSTATION_CSV)
    mapping["substation_id"] = mapping["substation_id"].astype(str)
    needed = set(mapping["substation_id"])

    frames = []
    C.require_file(C.GRIP_ED_SUBSTATIONS, hint="Need unzipped GRIP EDSubstations.shp")
    grip = _to_albers(gpd.read_file(C.GRIP_ED_SUBSTATIONS))
    id_col = C.pick_column(grip.columns, ("Substati00", "SUBSTATION", "SubstationID"))
    name_col = C.pick_column(grip.columns, ("Substation", "NAME", "Name"))
    grip["substation_id"] = grip[id_col].astype(str) if id_col else grip.index.astype(str)
    grip["substation_name"] = grip[name_col].astype(str) if name_col else grip["substation_id"]
    grip["source"] = "grip"
    frames.append(grip[["substation_id", "substation_name", "source", "geometry"]])

    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        hifld = _to_albers(gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG))
        hid = "OBJECTID" if "OBJECTID" in hifld.columns else ("ID" if "ID" in hifld.columns else None)
        if hid is not None:
            hifld["substation_id"] = "HIFLD_" + hifld[hid].astype(str)
            name_col = C.pick_column(hifld.columns, ("Name", "NAME", "Substation"))
            hifld["substation_name"] = (
                hifld[name_col].astype(str) if name_col else hifld["substation_id"]
            )
            hifld["source"] = "hifld"
            frames.append(hifld[["substation_id", "substation_name", "source", "geometry"]])

    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=C.CA_ALBERS_CRS)
    gdf = gdf.drop_duplicates("substation_id")

    missing = needed - set(gdf["substation_id"])
    if missing:
        extra = (
            mapping[mapping["substation_id"].isin(missing)]
            [["substation_id", "substation_name", "source"]]
            .drop_duplicates("substation_id")
        )
        rows = []
        for _, r in extra.iterrows():
            rows.append(
                {
                    "substation_id": r["substation_id"],
                    "substation_name": r.get("substation_name", r["substation_id"]),
                    "source": r.get("source", "unknown"),
                    "geometry": Point(0, 0),
                }
            )
        gdf = pd.concat(
            [gdf, gpd.GeoDataFrame(rows, geometry="geometry", crs=C.CA_ALBERS_CRS)],
            ignore_index=True,
        )
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=C.CA_ALBERS_CRS)

    # Keep stations that appear in the TAZ map plus all GRIP stations (full PGE backbone)
    keep = set(gdf.loc[gdf["source"] == "grip", "substation_id"]) | needed
    gdf = gdf[gdf["substation_id"].isin(keep)].copy()
    gdf = gdf.merge(rank, on="substation_id", how="left")
    gdf["mean_week_kwh"] = gdf["mean_week_kwh"].fillna(0.0)
    gdf["mean_peak_kW"] = gdf["mean_peak_kW"].fillna(0.0)
    gdf["hub_id"] = gdf["substation_id"].map(lambda s: f"SUB_{s}")
    return gdf


def assign_parent_ba(hubs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    wpath = C.MESO_DIR / "substation_weights.csv"
    if wpath.is_file():
        wdf = pd.read_csv(wpath)
        wdf["substation_id"] = wdf["substation_id"].astype(str)
        hubs = hubs.merge(wdf[["substation_id", "parent_ba", "w_s"]], on="substation_id", how="left")
    ba_ll = {
        "WEC_CALN": (-122.0, 38.0),
        "WEC_BANC": (-121.5, 38.6),
        "WECC_SCE": (-117.5, 34.0),
        "WEC_LADW": (-118.3, 34.1),
        "WEC_SDGE": (-117.1, 32.8),
        "WECC_IID": (-115.5, 33.0),
    }
    ba_gdf = gpd.GeoDataFrame(
        {"ba": list(ba_ll.keys()), "geometry": [Point(xy) for xy in ba_ll.values()]},
        crs="EPSG:4326",
    ).to_crs(hubs.crs)
    miss = hubs["parent_ba"].isna() if "parent_ba" in hubs.columns else pd.Series(True, index=hubs.index)
    if miss.any():
        joined = gpd.sjoin_nearest(hubs.loc[miss], ba_gdf, how="left")
        joined = joined.drop_duplicates("hub_id")
        if "parent_ba" not in hubs.columns:
            hubs["parent_ba"] = pd.NA
        hubs.loc[miss, "parent_ba"] = joined["ba"].to_numpy()
    hubs["parent_ba"] = hubs["parent_ba"].fillna("WEC_CALN")
    if "w_s" not in hubs.columns:
        hubs["w_s"] = 0.0
    hubs["w_s"] = hubs["w_s"].fillna(0.0)
    return hubs


def _endpoint_points(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "LineString":
        coords = list(geom.coords)
    elif geom.geom_type == "MultiLineString":
        coords = list(geom.geoms[0].coords)
    else:
        return None
    if len(coords) < 2:
        return None
    return Point(coords[0]), Point(coords[-1])


def build_edges(hubs: gpd.GeoDataFrame, lines: gpd.GeoDataFrame) -> pd.DataFrame:
    """Snap each line's endpoints to nearest substations; aggregate parallel capacity."""
    hx = hubs.geometry.x.to_numpy()
    hy = hubs.geometry.y.to_numpy()
    ids = hubs["hub_id"].to_numpy()
    rows = []
    for _, ln in lines.iterrows():
        ends = _endpoint_points(ln.geometry)
        if ends is None:
            continue
        a, b = ends
        da = (hx - a.x) ** 2 + (hy - a.y) ** 2
        db = (hx - b.x) ** 2 + (hy - b.y) ** 2
        ia, ib = int(np.argmin(da)), int(np.argmin(db))
        if ia == ib:
            continue
        cap = C.line_limit_w(ln)
        u, v = sorted([ids[ia], ids[ib]])
        rows.append(
            {
                "source": u,
                "target": v,
                "installed_capacity_W": cap,
                "rated_mva": cap / 1e6,
                "rated_kv": ln.get("RATEDKV", ln.get("VOLTAGE", ln.get("KV"))),
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=["source", "target", "installed_capacity_W", "n_lines", "rated_mva"]
        )
    ed = pd.DataFrame(rows)
    return (
        ed.groupby(["source", "target"], as_index=False)
        .agg(
            installed_capacity_W=("installed_capacity_W", "sum"),
            n_lines=("rated_mva", "count"),
            rated_mva=("rated_mva", "sum"),
        )
    )


def load_transmission_lines(hubs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    C.require_file(C.GRIP_TRANSMISSION_LINES, hint="Need unzipped GRIP TransmissionLines.shp")
    grip_lines = _to_albers(gpd.read_file(C.GRIP_TRANSMISSION_LINES))
    grip_lines["line_source"] = "grip"
    frames = [grip_lines]
    if C.HIFLD_TX_LINES_GPKG.is_file():
        extra = _to_albers(gpd.read_file(C.HIFLD_TX_LINES_GPKG))
        extra["line_source"] = "hifld"
        frames.append(extra)
    else:
        print("  HIFLD transmission lines cache missing; using GRIP lines only.")
    return pd.concat(frames, ignore_index=True)


def map_wecc_generators(hubs: gpd.GeoDataFrame) -> list[dict]:
    path = C.resolve_wec_json()
    with open(path, encoding="utf-8") as fh:
        graph = json.load(fh)
    recs = []
    for node in graph["nodes"]:
        ba = node.get("id")
        if ba not in C.CALIFORNIA_REGIONS:
            continue
        for handle, asset in (node.get("assets") or {}).items():
            cls = asset.get("_class")
            if cls not in ("Producer", "Load", "Store"):
                continue
            if cls == "Load" and str(asset.get("type", "")).lower() == "load":
                continue  # demand handled in 08_03
            x, y = asset.get("x"), asset.get("y")
            recs.append(
                {
                    "handle": handle,
                    "parent_ba": ba,
                    "class": cls,
                    "type": asset.get("type"),
                    "fuel": asset.get("fuel"),
                    "installed_capacity": asset.get("installed_capacity"),
                    "capex_capacity": asset.get("capex_capacity", 0),
                    "lon": x,
                    "lat": y,
                    "optional": str(handle).startswith("optional_"),
                    "profile_key": asset.get("profile") if isinstance(asset.get("profile"), str) else None,
                }
            )
    if not recs:
        return []
    df = pd.DataFrame(recs)
    has_xy = df["lon"].notna() & df["lat"].notna()
    mapped = []
    if has_xy.any():
        gdf = gpd.GeoDataFrame(
            df.loc[has_xy].copy(),
            geometry=gpd.points_from_xy(df.loc[has_xy, "lon"], df.loc[has_xy, "lat"]),
            crs="EPSG:4326",
        ).to_crs(hubs.crs)
        joined = gpd.sjoin_nearest(
            gdf, hubs[["hub_id", "substation_id", "parent_ba", "geometry"]], how="left", distance_col="snap_m"
        )
        joined = joined.drop_duplicates(subset=["handle", "parent_ba"], keep="first")
        for _, r in joined.iterrows():
            mapped.append(
                {
                    "handle": r["handle"],
                    "hub_id": r["hub_id"],
                    "substation_id": r["substation_id"],
                    "parent_ba": r["parent_ba_left"] if "parent_ba_left" in joined.columns else r["parent_ba"],
                    "class": r["class"],
                    "type": r["type"],
                    "fuel": r["fuel"],
                    "installed_capacity": r["installed_capacity"],
                    "capex_capacity": r["capex_capacity"],
                    "optional": bool(r["optional"]),
                    "profile_key": r["profile_key"],
                    "snap_m": float(r["snap_m"]) if pd.notna(r.get("snap_m")) else None,
                }
            )
    # Assets without coordinates stay on the parent BA (optional solar/wind CAPEX)
    for _, r in df.loc[~has_xy].iterrows():
        mapped.append(
            {
                "handle": r["handle"],
                "hub_id": None,
                "substation_id": None,
                "parent_ba": r["parent_ba"],
                "class": r["class"],
                "type": r["type"],
                "fuel": r["fuel"],
                "installed_capacity": r["installed_capacity"],
                "capex_capacity": r["capex_capacity"],
                "optional": bool(r["optional"]),
                "profile_key": r["profile_key"],
                "snap_m": None,
            }
        )
    print(f"  mapped {sum(1 for m in mapped if m['hub_id'])} CA assets to substations; "
          f"{sum(1 for m in mapped if m['hub_id'] is None)} remain on parent BA")
    return mapped


def map_interties(hubs: gpd.GeoDataFrame) -> list[dict]:
    rows = []
    pts = gpd.GeoDataFrame(
        [
            {"intertie_id": k, **v, "geometry": Point(v["lon"], v["lat"])}
            for k, v in C.INTERTIE_POINTS.items()
        ],
        geometry="geometry",
        crs="EPSG:4326",
    ).to_crs(hubs.crs)
    joined = gpd.sjoin_nearest(
        pts, hubs[["hub_id", "substation_id", "geometry"]], how="left", distance_col="snap_m"
    )
    joined = joined.drop_duplicates("intertie_id")
    for _, r in joined.iterrows():
        rows.append(
            {
                "intertie_id": r["intertie_id"],
                "path": r["path"],
                "parent_ba": r["parent_ba"],
                "remote_ba": r["remote_ba"],
                "hub_id": r["hub_id"],
                "substation_id": r["substation_id"],
                "snap_m": float(r["snap_m"]) if pd.notna(r.get("snap_m")) else None,
            }
        )
    return rows


def candidate_bess(hubs: gpd.GeoDataFrame) -> list[dict]:
    """Every substation with EV peak > 0 is a BESS candidate (S2)."""
    out = []
    for _, r in hubs.iterrows():
        peak_kw = float(r.get("mean_peak_kW") or 0.0)
        if peak_kw <= 0:
            continue
        out.append(
            {
                "hub_id": r["hub_id"],
                "substation_id": r["substation_id"],
                "capex_capacity_W": max(peak_kw * 1000.0 * 0.5, 1e6),
                "duration_h": 4.0,
            }
        )
    return out


def connected_component_gateways(hubs: gpd.GeoDataFrame, edges: pd.DataFrame) -> pd.DataFrame:
    """One BA↔substation interface per connected component (plus isolates)."""
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(hubs["hub_id"].tolist())
    for _, e in edges.iterrows():
        g.add_edge(e["source"], e["target"])
    hubs_ix = hubs.set_index("hub_id")
    rows = []
    for comp in nx.connected_components(g):
        members = hubs_ix.loc[list(comp)]
        # pick the bus with largest EV energy in each component as the gateway
        gw = members["mean_week_kwh"].idxmax() if "mean_week_kwh" in members else members.index[0]
        ba = members.loc[gw, "parent_ba"]
        # interface capacity: 2× component mean kW, floor 50 MW
        mean_kw = float(members["mean_week_kwh"].sum()) / (7 * 24) if "mean_week_kwh" in members else 50e3
        cap_w = max(50e6, mean_kw * 1e3 * 2.0)
        rows.append(
            {
                "hub_id": gw,
                "parent_ba": ba,
                "interface_capacity_W": cap_w,
                "n_substations": len(members),
                "role": "component_gateway",
            }
        )
    # Isolates already included. Also attach named intertie hubs if missing.
    return pd.DataFrame(rows)


def write_seasonal_hub_loads(hubs: gpd.GeoDataFrame) -> None:
    hub_ids = hubs["hub_id"].tolist()
    sub_to_hub = dict(zip(hubs["substation_id"].astype(str), hubs["hub_id"]))
    for week in C.SEASONAL_WEEKS:
        name = week["name"]
        wdir = C.MESO_DIR / "seasonal" / name
        sid_path = wdir / "substation_ids.npy"
        ev_path = wdir / "substation_hourly_kW.npy"
        tot_path = wdir / "substation_hourly_total_kW.npy"
        if not (sid_path.is_file() and ev_path.is_file()):
            print(f"  skip seasonal load {name}: missing 08_03 outputs")
            continue
        sids = np.load(sid_path, allow_pickle=True).astype(str)
        ev = np.load(ev_path)
        tot = np.load(tot_path) if tot_path.is_file() else ev
        meso_ev = np.zeros((len(hub_ids), ev.shape[1]), dtype=np.float32)
        meso_tot = np.zeros_like(meso_ev)
        hub_index = {h: i for i, h in enumerate(hub_ids)}
        for i, sid in enumerate(sids):
            hid = sub_to_hub.get(sid)
            if hid is None:
                continue
            meso_ev[hub_index[hid]] += ev[i]
            meso_tot[hub_index[hid]] += tot[i]
        np.save(wdir / "meso_hub_ids.npy", np.array(hub_ids))
        np.save(wdir / "meso_hourly_kW.npy", meso_ev)
        np.save(wdir / "meso_hourly_total_kW.npy", meso_tot)
        print(f"  {name}: EV peak={meso_ev.sum(0).max()/1e6:.2f} GW  total peak={meso_tot.sum(0).max()/1e6:.2f} GW")


def cluster_hubs(gdf: gpd.GeoDataFrame, n_hubs: int) -> gpd.GeoDataFrame:
    from sklearn.cluster import KMeans

    xy = np.column_stack([gdf.geometry.x, gdf.geometry.y])
    n_hubs = min(n_hubs, len(gdf))
    labels = KMeans(n_clusters=n_hubs, random_state=42, n_init=10).fit_predict(xy)
    out = gdf.copy()
    out["hub_id"] = [f"MESO_{i:03d}" for i in labels]
    return out


def main(aggregate: int | None = None) -> None:
    print("Loading unclustered substation nodes...")
    stations = load_substation_nodes()
    if aggregate and 0 < aggregate < len(stations):
        print(f"WARNING: --aggregate {aggregate} is a screening option; ASTR2026 default is unclustered.")
        try:
            stations = cluster_hubs(stations, aggregate)
        except ImportError:
            print("sklearn missing; ignoring --aggregate")
    else:
        print(f"Using substation-level nodes (n={len(stations)}; no clustering)")

    hubs = assign_parent_ba(stations)
    hubs = hubs.reset_index(drop=True)
    print(hubs["parent_ba"].value_counts().to_string())

    print("Building edges from TransmissionLines.shp (voltage / MVA heuristics)...")
    lines = load_transmission_lines(hubs)
    edges = build_edges(hubs, lines)
    print(f"  {len(edges)} aggregated corridors")

    print("Spatial join: WECC CA generators / storage → nearest substation...")
    generators = map_wecc_generators(hubs)
    print("Snapping named interties (Path 15 / 26 / 66 / Palo Verde)...")
    interties = map_interties(hubs)
    bess = candidate_bess(hubs)
    interfaces = connected_component_gateways(hubs, edges)
    # ensure named intertie hubs also have BA interfaces
    have = set(zip(interfaces["hub_id"], interfaces["parent_ba"])) if len(interfaces) else set()
    extra_iface = []
    for it in interties:
        key = (it["hub_id"], it["parent_ba"])
        if key not in have:
            extra_iface.append(
                {
                    "hub_id": it["hub_id"],
                    "parent_ba": it["parent_ba"],
                    "interface_capacity_W": 500e6,
                    "n_substations": 1,
                    "role": f"intertie:{it['intertie_id']}",
                }
            )
            have.add(key)
    if extra_iface:
        interfaces = pd.concat([interfaces, pd.DataFrame(extra_iface)], ignore_index=True)

    C.ensure_dir(C.MESO_DIR)
    hubs.drop(columns=["geometry"]).to_csv(C.MESO_DIR / "meso_nodes.csv", index=False)
    edges.to_csv(C.MESO_DIR / "meso_edges.csv", index=False)
    hubs.to_file(C.MESO_DIR / "meso_hubs.gpkg", driver="GPKG")
    interfaces.to_csv(C.MESO_DIR / "meso_ba_interfaces.csv", index=False)
    stations[["substation_id", "hub_id"]].to_csv(C.MESO_DIR / "substation_to_hub.csv", index=False)

    ll = hubs.to_crs(4326)
    network = {
        "crs": C.CA_ALBERS_CRS,
        "resolution": "substation" if hubs["hub_id"].str.startswith("SUB_").all() else "aggregated",
        "n_nodes": int(len(hubs)),
        "n_edges": int(len(edges)),
        "voltage_heuristics_mva": C.KV_TO_MVA,
        "nodes": [
            {
                "hub_id": r.hub_id,
                "substation_id": r.substation_id,
                "substation_name": r.substation_name,
                "source": r.source,
                "parent_ba": r.parent_ba,
                "w_s": float(r.w_s),
                "lon": float(ll.geometry.iloc[i].x),
                "lat": float(ll.geometry.iloc[i].y),
            }
            for i, r in enumerate(hubs.itertuples())
        ],
        "edges": edges.to_dict(orient="records"),
        "generators": generators,
        "bess_candidates": bess,
        "interties": interties,
        "ba_interfaces": interfaces.to_dict(orient="records"),
    }
    with open(C.CA_NETWORK_JSON, "w", encoding="utf-8") as fh:
        json.dump(network, fh)
    (C.MESO_DIR / "meso_resolution.json").write_text(
        json.dumps({"resolution": network["resolution"], "n_nodes": network["n_nodes"]}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {C.CA_NETWORK_JSON} (nodes={network['n_nodes']}, edges={network['n_edges']})")

    print("Aggregating seasonal loads onto substation nodes...")
    write_seasonal_hub_loads(hubs)
    print(f"Wrote meso artifacts under {C.MESO_DIR}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    parser = argparse.ArgumentParser(description="Build unclustered CA substation network")
    parser.add_argument(
        "--aggregate",
        type=int,
        default=None,
        help="Screening only. ASTR2026 default is one node per substation.",
    )
    main(aggregate=parser.parse_args().aggregate)
