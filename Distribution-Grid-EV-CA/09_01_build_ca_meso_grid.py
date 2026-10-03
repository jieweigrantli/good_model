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
from shapely.geometry import LineString, Point

import common as C

# Threshold above which a substation is treated as a bulk-transmission
# boundary point eligible to gateway directly to its parent WECC BA node.
GATEWAY_KV_THRESHOLD = 230.0
GATEWAY_CAPACITY_FLOOR_W = 20e6


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
    if C.TAZ_TO_SUBSTATION_KNN_CSV.is_file():
        # Every substation carrying a share of some TAZ's load must be a node:
        # write_seasonal_hub_loads skips substations with no hub, which would
        # silently drop the load 08_03 allocated to them.
        knn = pd.read_csv(C.TAZ_TO_SUBSTATION_KNN_CSV)
        knn["substation_id"] = knn["substation_id"].astype(str)
        mapping = pd.concat(
            [mapping, knn[["TAZ", "substation_id", "substation_name", "source"]]],
            ignore_index=True,
        )
    needed = set(mapping["substation_id"])

    frames = []
    C.require_file(C.GRIP_ED_SUBSTATIONS, hint="Need unzipped GRIP EDSubstations.shp")
    grip = _to_albers(gpd.read_file(C.GRIP_ED_SUBSTATIONS))
    id_col = C.pick_column(grip.columns, ("Substati00", "SUBSTATION", "SubstationID"))
    name_col = C.pick_column(grip.columns, ("Substation", "NAME", "Name"))
    grip["substation_id"] = grip[id_col].astype(str) if id_col else grip.index.astype(str)
    grip["substation_name"] = grip[name_col].astype(str) if name_col else grip["substation_id"]
    grip["source"] = "grip"
    kv_col = C.pick_column(grip.columns, ("Voltage_kV", "VOLTAGE_KV", "VOLTAGE"))
    grip["max_kv"] = pd.to_numeric(grip[kv_col], errors="coerce") if kv_col else np.nan
    frames.append(grip[["substation_id", "substation_name", "source", "max_kv", "geometry"]])

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
            kv_col = C.pick_column(hifld.columns, ("Max_Voltag", "MAX_VOLT", "Voltage"))
            hifld["max_kv"] = pd.to_numeric(hifld[kv_col], errors="coerce") if kv_col else np.nan
            frames.append(hifld[["substation_id", "substation_name", "source", "max_kv", "geometry"]])

    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=C.CA_ALBERS_CRS)
    gdf = gdf.drop_duplicates("substation_id")
    # Every known substation record, before filtering to model nodes; used to
    # pool co-located records into one site voltage.
    all_records = gdf.copy()
    all_records["max_kv"] = pd.to_numeric(all_records["max_kv"], errors="coerce").replace(0.0, np.nan)

    missing = needed - set(gdf["substation_id"])
    if missing:
        extra = (
            mapping[mapping["substation_id"].isin(missing)]
            [["substation_id", "substation_name", "source"]]
            .drop_duplicates("substation_id")
        )
        ba_ll = {f"BA_GW_{ba}": xy for ba, xy in C.BA_CENTROIDS_LL.items()}
        rows = []
        for _, r in extra.iterrows():
            lon, lat = ba_ll.get(str(r["substation_id"]), (-120.0, 37.0))
            rows.append(
                {
                    "substation_id": r["substation_id"],
                    "substation_name": r.get("substation_name", r["substation_id"]),
                    "source": r.get("source", "unknown"),
                    "max_kv": np.nan,
                    "geometry": Point(lon, lat),
                }
            )
        extra_gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(C.CA_ALBERS_CRS)
        gdf = pd.concat([gdf, extra_gdf], ignore_index=True)
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=C.CA_ALBERS_CRS)

    # Keep stations that appear in the TAZ map, all GRIP stations (full PGE
    # backbone), and the switchyards of large power plants. The last group
    # serves no load, so the TAZ test drops them -- see generation_switchyards.
    keep = (
        set(gdf.loc[gdf["source"] == "grip", "substation_id"])
        | needed
        | generation_switchyards(all_records)
    )
    gdf = gdf[gdf["substation_id"].isin(keep)].copy()
    gdf = gdf.merge(
        rank[["substation_id", "mean_week_kwh", "mean_peak_kW"]].drop_duplicates("substation_id"),
        on="substation_id",
        how="left",
    )
    gdf["mean_week_kwh"] = gdf["mean_week_kwh"].fillna(0.0)
    gdf["mean_peak_kW"] = gdf["mean_peak_kW"].fillna(0.0)
    gdf["hub_id"] = gdf["substation_id"].map(lambda s: f"SUB_{s}")

    # Real assigned peak demand (base + EV), used to size fallback gateway
    # interfaces to what a substation actually needs rather than a flat
    # floor (previously 20 MW regardless of assigned load -- e.g. one
    # substation was assigned a 228 MW base-load share behind a 20 MW cap).
    if C.SUB_HOURLY_LOADS_8760.is_file():
        peaks = pd.read_parquet(C.SUB_HOURLY_LOADS_8760, columns=["substation_id", "total_peak_W"])
        peaks["substation_id"] = peaks["substation_id"].astype(str)
        gdf = gdf.merge(peaks.drop_duplicates("substation_id"), on="substation_id", how="left")
    if "total_peak_W" not in gdf.columns:
        gdf["total_peak_W"] = 0.0
    gdf["total_peak_W"] = gdf["total_peak_W"].fillna(0.0)
    # HIFLD encodes "unknown" as 0; keep that as NaN so it reads as unknown
    # rather than as a 0 kV substation that no line may terminate at.
    if "max_kv" not in gdf.columns:
        gdf["max_kv"] = np.nan
    gdf["max_kv"] = pd.to_numeric(gdf["max_kv"], errors="coerce").replace(0.0, np.nan)
    gdf["site_kv"] = _site_voltage(gdf, all_records)
    return gdf


# Records at the same physical site are treated as one yard when deciding what
# voltage may terminate there.
SITE_RADIUS_M = 400.0


def _site_voltage(gdf: gpd.GeoDataFrame, all_records: gpd.GeoDataFrame) -> pd.Series:
    """Highest voltage present at each substation's physical site.

    GRIP EDSubstations report the *secondary* (distribution) voltage, so Vaca
    Dixon -- a 500/230 kV PG&E yard -- reads as 12 kV. Gating on that alone
    would refuse real transmission terminations. A substation yard is one
    site, and the voltage that can terminate there is the highest voltage of
    any record at that site, so co-located GRIP and HIFLD records are pooled.
    """
    from scipy.spatial import cKDTree

    src = all_records[all_records["max_kv"].notna()]
    if src.empty:
        return gdf["max_kv"]
    tree = cKDTree(np.column_stack([src.geometry.x, src.geometry.y]))
    kv = src["max_kv"].to_numpy(dtype=float)
    out = gdf["max_kv"].to_numpy(dtype=float).copy()
    for i, (x, y) in enumerate(zip(gdf.geometry.x, gdf.geometry.y)):
        near = tree.query_ball_point((x, y), r=SITE_RADIUS_M)
        if near:
            best = np.nanmax(kv[near])
            out[i] = best if np.isnan(out[i]) else max(out[i], best)
    return pd.Series(out, index=gdf.index)


def assign_parent_ba(hubs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    # Balancing area comes from ownership and service territory
    # (08_05_assign_substation_ba.py), not from a majority vote of the TAZs
    # that load allocation happened to attach to a substation. The vote fails
    # for bulk hubs, which serve no local load: Tesla, a 500 kV PG&E switching
    # hub, was assigned to WEC_BANC by a single TAZ 5.4 km away that ranked it
    # 4th of 4. Measured against the HIFLD Owner field, PG&E substations were
    # wrong 66% of the time, and 13 voltage gateways carrying 16.2 GW attached
    # to the wrong BA bus.
    bapath = C.MESO_DIR / "substation_ba.csv"
    wpath = C.MESO_DIR / "substation_weights.csv"

    if wpath.is_file():
        wdf = pd.read_csv(wpath)
        wdf["substation_id"] = wdf["substation_id"].astype(str)
        cols = ["substation_id", "w_s"]
        if not bapath.is_file():
            cols.append("parent_ba")
        hubs = hubs.merge(wdf[cols], on="substation_id", how="left")

    if bapath.is_file():
        bdf = pd.read_csv(bapath)
        bdf["substation_id"] = bdf["substation_id"].astype(str)
        hubs = hubs.merge(
            bdf[["substation_id", "parent_ba", "ba_source"]], on="substation_id", how="left"
        )
        got = hubs["parent_ba"].notna().sum()
        print(f"  parent_ba from ownership/territory for {got:,}/{len(hubs):,} substations")
    ba_gdf = gpd.GeoDataFrame(
        {"ba": list(C.BA_CENTROIDS_LL.keys()), "geometry": [Point(xy) for xy in C.BA_CENTROIDS_LL.values()]},
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


# Adjacent digitized spans meant to represent one continuous corridor rarely
# share exact coordinates (independent digitization / different vintages).
# 10m left the graph fragmented into ~5,800 mostly-isolated 2-3 node pieces;
# rounding tolerance and anchor threshold are coupled -- coarser rounding
# shifts a coordinate's snapped position by up to ~0.7x the tolerance
# (diagonal), which can push it past a *fixed* anchor threshold and silently
# lose substations that were previously anchored (measured: raising rounding
# alone from 750m to 1000m dropped anchored substations from 1,672 to 1,296).
# Scanning coupled (round, anchor) pairs up to ~3km showed steady,
# well-behaved connectivity gains with no sudden over-merging cliff (the
# largest connected component grows gradually, not explosively), but beyond
# ~1-1.5km the risk shifts from "recovering real imprecise-digitization
# gaps" to "fabricating connections between genuinely separate corridors" --
# rural bulk-transmission spacing is legitimately multi-km. 750m/900m
# recovers 1,563 of 1,881 substations into real multi-substation
# connectivity (83%, vs 66% at the previous 100m/500m setting) while
# staying inside plausible cross-dataset digitization error.
CORRIDOR_COORD_ROUND_M = 750.0
CORRIDOR_ANCHOR_THRESHOLD_M = 900.0

# How close a line endpoint must be to a substation to count as terminating
# there. Endpoints at a real substation sit within tens of metres (measured
# 3.6-35 m around MABURY), so in principle this only needs to absorb
# digitisation error. In practice our substation points and our line geometry
# come from different vintages and different publishers (HIFLD/CEC points vs
# GRIP/CEC line geometry), and the offset between them is much larger than
# within-source digitisation error.
#
# Published open-data transmission pipelines that join HIFLD line geometry to
# separately-sourced substation points use ~1,500 m snapping with a 250 m
# substation buffer (Bor, Oughton et al. 2024, arXiv:2412.17685). Our own
# audit agreed: 77% of endpoints fall within 400 m of a real substation but
# only 15% dangle beyond 1 km, so the band between 400 m and 1.5 km holds a
# large share of genuine terminations that a 400 m threshold discards.
CORRIDOR_SUBSTATION_SNAP_M = 1500.0

# Within this radius a model node wins an endpoint outright, even if some
# other real substation is nearer. Beyond it, nearest-wins between the two.
#
# Model-node precedence exists because junction substations are ~2x denser, so
# a plain nearest-neighbour query lets a junction 10 m away steal an endpoint
# from the model node 30 m away that the corridor actually terminates at.
# That precedence must stay *tight*, though: applied across the full 1.5 km
# snap radius it would let a model node 1,400 m away beat a junction 5 m away,
# which is the same bug with the sign flipped.
CORRIDOR_HUB_PRECEDENCE_M = 400.0

# How close a substation must be to line geometry for that line to be cut at
# it, turning a pass-through into a pair of spans that terminate there.
# Tighter than the snap radius on purpose: snapping decides which substation
# an existing endpoint belongs to, whereas this asserts that a line
# terminates somewhere the data never said it did.
CORRIDOR_SPLIT_M = 250.0

# Radius used when counting how much circuit capacity lands on a substation
# busbar (gateway sizing). Deliberately independent of the corridor snap
# radius: snapping may reach out 1.5 km to decide which substation an endpoint
# belongs to, but "capacity terminating on this busbar" is a tight, physical
# question, and widening it inflates every gateway.
GATEWAY_INCIDENCE_SNAP_M = 400.0

# A run of pass-through geometry that touches many substations is a shared
# junction, not a point-to-point corridor: connecting every pair through it
# would credit each pair with capacity all the others are also drawing on.
# Keep the highest-capacity terminals and cap the fan-out so one large meshed
# run cannot turn into a dense clique of overstated corridors.
CORRIDOR_MAX_JUNCTION_TERMINALS = 24


def _line_parts(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    return []


# Generation switchyards
# ---------------------------------------------------------------------------
# A substation becomes a model node only if a TAZ mapped load to it (plus the
# whole GRIP set). That is the right test for a load-serving substation and the
# wrong one for a generation switchyard, which serves no load but is the
# injection point for a power plant. The consequence was severe: HIFLD carries
# "Diablo Canyon" as a 500 kV substation 911 m from the plant, but with no load
# mapped to it, it was demoted to a line junction. `map_wecc_generators` then
# snapped Diablo Canyon's 2,240 MW by nearest-neighbour to FOOTHILL -- a 12 kV
# distribution substation 15.9 km away with two 115 kV corridors totalling
# 300 MW. The must-run constraint forces 2,240 MW in every hour, 300 MW can
# leave, and the model spilled 1,301 GWh over four weeks. The same happened to
# The Geysers: 926 MW onto MIDDLETOWN, 12 kV, via a single 13.6 MW corridor,
# spilling 608 GWh. Together those two nodes were 92% of all curtailment.
#
# Checked against the real interconnections: Diablo Canyon has its own 230 kV
# and 500 kV switchyards and five lines -- Morro Bay and Mesa at 230 kV, Midway
# (x2) and Gates at 500 kV -- and HIFLD's own line layer records exactly those
# five with DIABLO CANYON as a named endpoint. The Geysers reaches Fulton and
# Lakeville at 230 kV and Eagle Rock at 115 kV. None of it involves Foothill or
# Middletown. The topology was in the data; the node-selection rule discarded
# the terminals.
GEN_SWITCHYARD_MIN_MW = 100.0
GEN_SWITCHYARD_RADIUS_M = 2000.0

# Snapping a plant to the nearest substation, with no regard for whether that
# substation could carry it, put 1,681 MW of Tehachapi wind onto two unnamed
# HIFLD points at up to 4.7 km, inside a pocket whose only egress is two 80 MW
# corridors. SCE's own circuit inventory confirms the pocket: every circuit that
# touches those nodes is 66 kV subtransmission, and the nearest 220 kV
# transmission circuit is 2.9 km away. So the corridors were right and the
# assignment was wrong -- the real farms collect at Arbwind (230 kV) and
# Highwind (220 kV), both of which the model already holds as nodes.
#
# The discriminator is distance, not voltage. A plant within GEN_SNAP_OWN_YARD_M
# of a substation is standing at its own switchyard, and that assignment is sound
# whatever HIFLD says the voltage is -- Otay Mesa at 883 m, GWF Tracy at 68 m and
# Devil Canyon at 82 m are all the plant's own yard, and a voltage test alone
# wrongly relocates every one of them. Past that radius the nearest node is not
# the plant's yard but whatever HIFLD happened to place closest, because HIFLD
# does not contain the collector yard at all; there, voltage decides.
#
# Applied to plants at or above GEN_SWITCHYARD_MIN_MW this moves 20 plant-sites
# and 3,409 MW, and leaves the 109 large sites already sitting on an adequate
# substation untouched.
GEN_SNAP_OWN_YARD_M = 1000.0
GEN_SNAP_MAX_M = 15000.0
# Tolerance for attributing an SCE circuit's published voltage to a substation
# its geometry passes. 250 m sits on the flat part of the measured snap
# distribution: 150 m resolves 205 of the 541 unknown-voltage substations, 250 m
# resolves 219 and 500 m only 237, so the gain past 250 m is small and bought with
# twice the radius.
SCE_VOLTAGE_FILL_M = 250.0


def generation_switchyards(all_records: gpd.GeoDataFrame) -> set[str]:
    """substation_ids that are the switchyard of a large power plant.

    Picks, within GEN_SWITCHYARD_RADIUS_M of each CA generator at or above
    GEN_SWITCHYARD_MIN_MW, the record with the HIGHEST voltage rather than the
    nearest one. Nearest is wrong here: the closest record to Diablo Canyon is
    "Pecho Valley" at 623 m with an unknown voltage, while the 500 kV "Diablo
    Canyon" switchyard is 911 m away. Voltage is what decides whether a
    substation can accept a GW-scale plant, so it orders the choice and distance
    only breaks ties.
    """
    path = C.resolve_wec_json()
    with open(path, encoding="utf-8") as fh:
        graph = json.load(fh)
    # Threshold on the PLANT, not the unit. WEC.json carries one asset per
    # generating unit, and a multi-unit plant can be large while every unit is
    # small: The Geysers is 926 MW across units of 40-68 MW, so a per-unit test
    # at 100 MW misses the second-largest curtailment source in the model
    # entirely. Units at identical coordinates are the same plant.
    by_site: dict[tuple, float] = {}
    for node in graph["nodes"]:
        if node.get("id") not in C.CALIFORNIA_REGIONS:
            continue
        for handle, asset in (node.get("assets") or {}).items():
            if asset.get("_class") != "Producer":
                continue
            x, y = asset.get("x"), asset.get("y")
            if not (x and y):
                continue
            mw = float(asset.get("installed_capacity") or 0.0) / 1e6
            by_site[(round(float(x), 5), round(float(y), 5))] = (
                by_site.get((round(float(x), 5), round(float(y), 5)), 0.0) + mw
            )
    pts = [
        {"mw": mw, "geometry": Point(x, y)}
        for (x, y), mw in by_site.items()
        if mw >= GEN_SWITCHYARD_MIN_MW
    ]
    if not pts:
        return set()
    gens = gpd.GeoDataFrame(pts, geometry="geometry", crs="EPSG:4326").to_crs(C.CA_ALBERS_CRS)

    recs = all_records.copy()
    recs["_kv"] = pd.to_numeric(recs.get("max_kv"), errors="coerce").fillna(-1.0)
    sindex = recs.sindex
    chosen: set[str] = set()
    for _, gen in gens.iterrows():
        idx = list(sindex.query(gen.geometry.buffer(GEN_SWITCHYARD_RADIUS_M)))
        if not idx:
            continue
        near = recs.iloc[idx].copy()
        near["_d"] = near.geometry.distance(gen.geometry)
        near = near[near["_d"] <= GEN_SWITCHYARD_RADIUS_M]
        if near.empty:
            continue
        # highest voltage first, nearest as the tie-break
        near = near.sort_values(["_kv", "_d"], ascending=[False, True])
        chosen.add(str(near.iloc[0]["substation_id"]))
    print(f"  {len(chosen)} generation switchyards kept as nodes "
          f"(plants >= {GEN_SWITCHYARD_MIN_MW:.0f} MW, within "
          f"{GEN_SWITCHYARD_RADIUS_M:.0f} m, highest voltage wins)")
    return chosen


def load_junction_points(hubs: gpd.GeoDataFrame) -> np.ndarray:
    """Coordinates of real substations that are not themselves model nodes.

    `load_substation_nodes` keeps a substation only if a TAZ mapped to it (plus
    the whole GRIP set), which discards 3,265 of 4,442 HIFLD substations. Those
    are real substations and, more to the point, real line terminals: measured
    against the kept 1,881 nodes only 42% of span endpoints land within 400 m
    of a substation and 50% dangle more than 1 km, but against the full 5,146
    the same endpoints are 77% within 400 m and only 15% dangle. The corridors
    are in the data; the terminals were being thrown away.

    They are returned as junctions rather than nodes: they let spans chain
    through a real substation instead of dead-ending, without adding ~3,300
    zero-load nodes (and their shortfall/wastage variables) to an LP that is
    already at this machine's memory ceiling.
    """
    have = set(hubs["substation_id"].astype(str))
    frames = []
    if C.GRIP_ED_SUBSTATIONS.is_file():
        grip = _to_albers(gpd.read_file(C.GRIP_ED_SUBSTATIONS))
        id_col = C.pick_column(grip.columns, ("Substati00", "SUBSTATION", "SubstationID"))
        grip["substation_id"] = grip[id_col].astype(str) if id_col else grip.index.astype(str)
        frames.append(grip[["substation_id", "geometry"]])
    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        hifld = _to_albers(gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG))
        hid = "OBJECTID" if "OBJECTID" in hifld.columns else ("ID" if "ID" in hifld.columns else None)
        if hid is not None:
            hifld["substation_id"] = "HIFLD_" + hifld[hid].astype(str)
            frames.append(hifld[["substation_id", "geometry"]])
    if not frames:
        return np.empty((0, 2))
    allsub = pd.concat(frames, ignore_index=True).drop_duplicates("substation_id")
    extra = allsub[~allsub["substation_id"].isin(have)]
    print(f"  {len(extra)} additional substations used as line junctions")
    return np.column_stack([extra.geometry.x.to_numpy(), extra.geometry.y.to_numpy()])


def _reconstruct_corridors(
    hubs: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    junction_xy: np.ndarray | None = None,
) -> list[dict]:
    """Reconstruct substation-to-substation corridors from per-span line geometry.

    The source line layers (GRIP/HIFLD/CEC) digitize transmission circuits as
    many short spans rather than single substation-to-substation lines (91%
    of CEC rows have no circuit-grouping field at all). Naively snapping each
    row's own two endpoints to the nearest substation treats mid-corridor
    waypoints as if they were destinations: when a span's far end
    coincidentally snaps to the SAME substation as its near end (because the
    corridor continues past it, invisible to that one row), the old logic
    silently dropped the whole span as a false self-loop -- measured at 52%
    of all loaded lines, fully isolating 58 substations that do have real
    nearby lines (e.g. a line whose true endpoint is 7.5 m from the
    substation).

    This instead builds a coordinate-level graph of every span, takes
    connected components (each one a physically-continuous corridor
    network), anchors substations to component coordinates within
    ``CORRIDOR_ANCHOR_THRESHOLD_M``, and reduces each component to a minimum
    spanning tree over just its anchored substations -- each MST edge's
    capacity is the bottleneck (minimum) capacity along its shortest
    physical path, matching how a chain of segments in series is actually
    capacity-limited by its weakest link.
    """
    import networkx as nx
    from scipy.spatial import cKDTree

    # Collect every span with its exact endpoints. Endpoints are clustered
    # below rather than snapped to a coordinate grid: grid-rounding maps a
    # position to a cell, so BOTH ends of any span shorter than the cell size
    # land on the same node and the span is discarded as a self-loop. At the
    # 750 m tolerance needed to chain rural corridors that silently deleted
    # 3,114 of 9,394 spans (33%), and disproportionately the short urban spans
    # that interconnect dense substation clusters -- which is why MABURY came
    # out with one 60 MW tie despite 14 x 115 kV lines terminating within 3 km.
    # Split line geometry where it passes through a substation.
    #
    # Snapping only ever looks at span *endpoints*, so a substation that a
    # line runs straight through is invisible to it: measured over the model
    # nodes that reached no line at all, the median perpendicular distance to
    # line geometry is 44 m while the median distance to the nearest line
    # endpoint is 898 m. They are sitting on the wire and were still dropped.
    # Cutting the part at those substations turns one pass-through span into
    # two spans that terminate there, which is what the geometry means.
    #
    # The cut is voltage-gated. A 500 kV line flying over a 12 kV
    # distribution yard does not terminate there, and splitting it there
    # would fabricate a connection -- and, worse, a plausible-looking one.
    # A substation may take the cut only if it is rated at or above the
    # line's voltage, or if its rating is unknown.
    hub_pts = np.column_stack([hubs.geometry.x.to_numpy(), hubs.geometry.y.to_numpy()])
    kv_col_for_split = "site_kv" if "site_kv" in hubs.columns else "max_kv"
    hub_kv = (
        pd.to_numeric(hubs[kv_col_for_split], errors="coerce").to_numpy(dtype=float)
        if kv_col_for_split in hubs.columns
        else np.full(len(hubs), np.nan)
    )
    split_tree = cKDTree(hub_pts)

    def _cut_positions(part, kv):
        """Distances along ``part`` at which a compatible substation sits."""
        idx = split_tree.query_ball_point(
            np.asarray(part.coords, dtype=float)[:, :2], r=CORRIDOR_SPLIT_M
        )
        near = {i for sub in idx for i in sub}
        if not near:
            return []
        out = []
        for i in near:
            k = hub_kv[i]
            if not np.isnan(k) and k + 1e-9 < kv:
                continue  # substation cannot terminate a line at this voltage
            p = Point(hub_pts[i])
            if part.distance(p) > CORRIDOR_SPLIT_M:
                continue
            d = part.project(p)
            if d > CORRIDOR_SPLIT_M and d < part.length - CORRIDOR_SPLIT_M:
                out.append(d)
        return sorted(out)

    spans = []
    for _, ln in lines.iterrows():
        cap = C.line_limit_w(ln)
        kv = C.line_rated_kv(ln)
        for whole in _line_parts(ln.geometry):
            cuts = _cut_positions(whole, kv)
            if cuts:
                bounds = [0.0] + cuts + [whole.length]
                pieces = []
                for lo, hi in zip(bounds[:-1], bounds[1:]):
                    if hi - lo < 1.0:
                        continue
                    pieces.append(
                        LineString([whole.interpolate(lo), whole.interpolate(hi)])
                    )
            else:
                pieces = [whole]

            for part in pieces:
                coords = list(part.coords)
                if len(coords) < 2:
                    continue
                a, b = coords[0][:2], coords[-1][:2]
                if a == b:
                    continue  # genuinely degenerate geometry
                spans.append((a, b, cap, kv, max(part.length, 1.0)))

    if not spans:
        return []

    pts = np.array([xy for s in spans for xy in (s[0], s[1])], dtype=float)

    hx = hubs.geometry.x.to_numpy()
    hy = hubs.geometry.y.to_numpy()
    ids = hubs["hub_id"].to_numpy()

    # Snap endpoints onto substations FIRST, then cluster only what is left.
    #
    # Clustering everything together and anchoring afterwards fails at exactly
    # the places that matter. A substation busbar is where many lines
    # genuinely do interconnect, so a general "never merge endpoints of the
    # same span" rule -- needed to stop dense pass-through geometry collapsing
    # short spans -- also refuses the legitimate merges at substations, and
    # corridors end up as dead-end stubs. Measured on MABURY: its three
    # 150 MW / 115 kV edges each ran into a pass-through run whose only anchor
    # terminal was MABURY itself, so none of them produced a corridor and the
    # node was left with a single 60 MW tie against 155 MW of load.
    # Snap targets are the model nodes plus every other real substation. Only
    # the model nodes become corridor terminals; the rest act as junctions so
    # spans chain through them instead of dead-ending.
    #
    # Model nodes take precedence: junctions are ~2x denser, so a single
    # nearest-neighbour query over the union lets a junction 10 m away steal an
    # endpoint from the model node 30 m away that the corridor actually
    # terminates at, which silently deletes that substation's connection.
    # Voltage-gate the endpoint snap, not just the mid-span split.
    #
    # Snapping took the nearest model node regardless of what that node is
    # rated for, so a 230 kV line could "terminate" at a 21 kV distribution
    # yard simply because it was the closest node within the snap radius.
    # That is how NEWARK 21KV acquired a 230 kV tie to Vincent 483 km away:
    # the corridor never terminated where it physically does, so contraction
    # ran it to the next real terminal and emitted one enormous edge. 36 of
    # the 63 corridors over 100 km had an endpoint rated below their own line
    # voltage. Restricting each endpoint to a site that can actually take that
    # voltage forces the corridor to break at the substations it really runs
    # through, which is the segmentation we want.
    site_kv = (
        pd.to_numeric(hubs["site_kv"], errors="coerce").to_numpy(dtype=float)
        if "site_kv" in hubs.columns
        else np.full(len(hubs), np.nan)
    )
    span_kv = np.repeat(np.array([s[3] for s in spans], dtype=float), 2)

    hub_tree = cKDTree(np.column_stack([hx, hy]))
    k_probe = int(min(len(hubs), 12))
    cand_d, cand_i = hub_tree.query(pts, k=k_probe)
    if k_probe == 1:
        cand_d, cand_i = cand_d[:, None], cand_i[:, None]

    hub_dist = np.full(len(pts), np.inf)
    hub_idx = np.zeros(len(pts), dtype=int)
    for n in range(len(pts)):
        kv_needed = span_kv[n]
        chosen = None
        for d, i in zip(cand_d[n], cand_i[n]):
            if not np.isfinite(d):
                break
            kv_here = site_kv[i]
            if np.isnan(kv_here) or kv_here + 1e-9 >= kv_needed:
                chosen = (d, i)
                break
        if chosen is None:
            # No site near this endpoint can take the line's voltage. Leave it
            # unsnapped so the span continues as pass-through geometry to a
            # substation that can, instead of terminating somewhere it cannot.
            hub_dist[n], hub_idx[n] = np.inf, cand_i[n][0]
        else:
            hub_dist[n], hub_idx[n] = chosen

    if junction_xy is not None and len(junction_xy):
        jx = np.asarray(junction_xy, dtype=float)
        j_dist, j_idx = cKDTree(jx).query(pts, k=1)
    else:
        j_idx = np.zeros(len(pts), dtype=int)
        j_dist = np.full(len(pts), np.inf)

    # Model nodes win outright inside the precedence radius; past it the
    # nearer of the two wins, and either may reach out to the full snap
    # radius. Ties go to the model node, which is the terminal we can
    # actually attach load and corridors to.
    hub_ok = hub_dist <= CORRIDOR_SUBSTATION_SNAP_M
    j_ok = j_dist <= CORRIDOR_SUBSTATION_SNAP_M
    at_hub = hub_ok & (
        (hub_dist <= CORRIDOR_HUB_PRECEDENCE_M) | (~j_ok) | (hub_dist <= j_dist)
    )
    at_junction = j_ok & ~at_hub

    at_sub = at_hub | at_junction

    # Cluster the remaining (pass-through) endpoints, refusing merges that
    # would short-circuit a span. Away from substations that rule is right:
    # there is no busbar there, so both ends of a line landing in one cluster
    # means the line has been erased.
    free = np.flatnonzero(~at_sub)
    pos = {int(i): k for k, i in enumerate(free)}
    parent = np.arange(len(free))

    def _find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    spans_in = [{int(i) // 2} for i in free]

    if len(free) > 1:
        tree = cKDTree(pts[free])
        candidates = tree.query_pairs(r=CORRIDOR_COORD_ROUND_M, output_type="ndarray")
        if len(candidates):
            d = np.hypot(*(pts[free][candidates[:, 0]] - pts[free][candidates[:, 1]]).T)
            candidates = candidates[np.argsort(d)]
        for a_k, b_k in candidates:
            ra, rb = _find(int(a_k)), _find(int(b_k))
            if ra == rb:
                continue
            if not spans_in[ra].isdisjoint(spans_in[rb]):
                continue  # this merge would short-circuit a line
            if len(spans_in[ra]) < len(spans_in[rb]):
                ra, rb = rb, ra
            parent[rb] = ra
            spans_in[ra] |= spans_in[rb]
            spans_in[rb] = set()

    def _node_of(i: int):
        if at_hub[i]:
            return ("S", str(ids[int(hub_idx[i])]))   # model node: a terminal
        if at_junction[i]:
            return ("J", int(j_idx[i]))               # real substation: junction
        return ("P", int(_find(pos[i])))

    node_of = [_node_of(i) for i in range(len(pts))]

    g = nx.Graph()
    for s, (a, b, cap, kv, length_m) in enumerate(spans):
        u, v = node_of[2 * s], node_of[2 * s + 1]
        if u == v:
            continue  # a genuine loop within one substation or cluster
        if g.has_edge(u, v):
            if cap > g[u][v]["capacity_W"]:
                g[u][v]["capacity_W"] = cap
                g[u][v]["rated_kv"] = kv
        else:
            g.add_edge(u, v, length_m=length_m, capacity_W=cap, rated_kv=kv)

    if g.number_of_nodes() == 0:
        return []

    # Diagnostic hook: the pre-contraction coordinate graph, so tooling can
    # tell apart "endpoint never snapped", "snapped but the line geometry
    # around it is isolated", and "connected in geometry but lost during
    # contraction".
    globals()["_LAST_CORRIDOR_GRAPH"] = g
    globals()["_LAST_NODE_OF"] = node_of

    node_to_hubs: dict[tuple, list[str]] = {}
    for node in g.nodes:
        if node[0] == "S":
            node_to_hubs.setdefault(node, []).append(node[1])

    # Reduce the coordinate graph to substation-to-substation corridors by
    # contracting the runs of pass-through geometry between substations.
    #
    # The previous approach ran a shortest-path search per anchor pair and
    # kept a pair only if no other anchor lay on the path, giving each
    # corridor the *minimum* capacity along the *length-shortest* route. Both
    # halves of that were wrong, and they under-connected the network badly:
    #   * The "no other anchor on the path" filter is far too aggressive in
    #     dense areas, where almost every pair has some third substation near
    #     its straight-line route, so real links were discarded wholesale.
    #   * Taking the bottleneck of the length-shortest route lets one short
    #     low-voltage span cap a corridor that also has a parallel
    #     high-voltage route, and collapses genuinely parallel circuits into
    #     a single edge instead of adding their capacity.
    # Measured on SUB_08219 (MABURY, San Jose): 14 x 115 kV lines terminate
    # within 3 km (nearest 6.7 m) with endpoints on five distinct
    # substations, yet the node came out with a single 60 MW corridor against
    # 155 MW of load -- an entirely manufactured deficit that showed up as
    # 54% of all system shortfall.
    #
    # Instead: delete the anchor nodes, take the connected components of what
    # remains (each one a run of pass-through geometry), and connect the
    # anchors that touch the same run. A substation's capacity into a run is
    # the sum of its own incident edges into it, so parallel circuits add;
    # a pair's capacity is limited by the weaker of the two ends. Corridors
    # for the same pair arising from different runs are summed downstream by
    # build_edges' groupby, so parallel routes add there too.
    anchor_nodes = set(node_to_hubs)
    passthrough = g.copy()
    passthrough.remove_nodes_from(anchor_nodes)

    rows = []

    def _emit(hid_a, hid_b, cap, kv):
        if hid_a == hid_b or cap <= 0:
            return
        src, tgt = sorted([hid_a, hid_b])
        rows.append(
            {
                "source": src,
                "target": tgt,
                "installed_capacity_W": float(cap),
                "rated_mva": float(cap) / 1e6,
                "rated_kv": float(kv),
            }
        )

    # Anchors joined directly by a single span, with no pass-through geometry.
    for u, v, ed in g.edges(data=True):
        if u in anchor_nodes and v in anchor_nodes:
            for hid_a in node_to_hubs[u]:
                for hid_b in node_to_hubs[v]:
                    _emit(hid_a, hid_b, ed["capacity_W"], ed["rated_kv"])

    # Anchors joined through a run of pass-through geometry.
    #
    # Every iteration order here is made explicit, because this block decided
    # the topology from the hash order of a set. `nx.connected_components`
    # yields sets of node keys, and a node key for an anchor is
    # ``("S", "SUB_25454")`` -- a tuple containing a string, whose hash is
    # randomised per process. Iterating the run in that order set the insertion
    # order of `terminals`, which broke ties in the capacity sort below, which
    # chose which terminals survived the fan-out cap, which changed the MST.
    # Two builds of the same inputs therefore emitted different corridors: 4,947
    # edges against 4,946, with 16 links moving between near-coincident
    # substations that tie on capacity -- SUB_25454 against SUB_25457,
    # SUB_HIFLD_3994 against SUB_HIFLD_3995. Capacities never differed, only
    # which node held the connection.
    #
    # That is a reproducibility defect on its own, and it also made any
    # before-and-after scenario comparison unattributable, because rebuilding
    # the network to change one thing silently changed sixteen others.
    for run in sorted(nx.connected_components(passthrough), key=min):
        terminals: dict[tuple, dict] = {}
        for node in sorted(run):
            for nbr in g.neighbors(node):
                if nbr not in anchor_nodes:
                    continue
                ed = g[node][nbr]
                rec = terminals.setdefault(nbr, {"cap": 0.0, "kv": 0.0})
                rec["cap"] += ed["capacity_W"]          # parallel circuits add
                rec["kv"] = max(rec["kv"], ed["rated_kv"])
        if len(terminals) < 2:
            continue
        # A run touching many substations is a shared junction rather than a
        # point-to-point corridor; connecting every pair through it would
        # credit each pair with capacity the others are also using. Cap the
        # fan-out so a single large meshed run cannot generate a dense clique.
        # Tie-break on the node key so equal-capacity terminals order the same
        # way in every process; `reverse=True` on capacity alone left the tie to
        # dict insertion order.
        items = sorted(terminals.items(), key=lambda kv: (-kv[1]["cap"], kv[0]))
        if len(items) > CORRIDOR_MAX_JUNCTION_TERMINALS:
            items = items[:CORRIDOR_MAX_JUNCTION_TERMINALS]

        # Connect the terminals along the run, not to each other wholesale.
        #
        # Emitting every pair treats a shared corridor as a clique: a run that
        # touches San Ramon, Newark, Vincent and Antelope produced a direct
        # San Ramon-Vincent edge 499 km long, which is not a circuit that
        # exists -- the real path runs through the substations in between.
        # Taking a minimum spanning tree over the terminals, weighted by the
        # actual routed distance through the pass-through geometry, keeps each
        # terminal joined to its neighbours along the corridor and drops the
        # long chords.
        sub = g.subgraph(set(run) | {nd for nd, _ in items})
        term_nodes = [nd for nd, _ in items]
        tgraph = nx.Graph()
        tgraph.add_nodes_from(term_nodes)
        for nd in term_nodes:
            try:
                dist = nx.single_source_dijkstra_path_length(sub, nd, weight="length_m")
            except (nx.NodeNotFound, nx.NetworkXError):
                continue
            for other in term_nodes:
                if other == nd or other not in dist:
                    continue
                w = float(dist[other])
                if not tgraph.has_edge(nd, other) or w < tgraph[nd][other]["weight"]:
                    tgraph.add_edge(nd, other, weight=w)
        keep_pairs = {
            tuple(sorted(pair)) for pair in nx.minimum_spanning_tree(tgraph).edges()
        }

        for i in range(len(items)):
            node_a, rec_a = items[i]
            for j in range(i + 1, len(items)):
                node_b, rec_b = items[j]
                if tuple(sorted((node_a, node_b))) not in keep_pairs:
                    continue
                cap = min(rec_a["cap"], rec_b["cap"])
                kv = min(rec_a["kv"], rec_b["kv"])
                for hid_a in node_to_hubs[node_a]:
                    for hid_b in node_to_hubs[node_b]:
                        _emit(hid_a, hid_b, cap, kv)
    return rows


def build_edges(hubs: gpd.GeoDataFrame, lines: gpd.GeoDataFrame, rows: list[dict] | None = None) -> pd.DataFrame:
    """Aggregate parallel line capacity into one edge per substation corridor."""
    if rows is None:
        rows = _reconstruct_corridors(hubs, lines)
    if not rows:
        return pd.DataFrame(
            columns=["source", "target", "installed_capacity_W", "n_lines", "rated_mva", "rated_kv"]
        )
    ed = pd.DataFrame(rows)
    return (
        ed.groupby(["source", "target"], as_index=False)
        .agg(
            installed_capacity_W=("installed_capacity_W", "sum"),
            n_lines=("rated_mva", "count"),
            rated_mva=("rated_mva", "sum"),
            # Capacity sums across parallel circuits, so rated_mva cannot be
            # inverted back to a voltage. Carry the corridor's highest circuit
            # voltage through explicitly for diagnostics and mapping.
            rated_kv=("rated_kv", "max"),
        )
    )


def merge_asserted_corridors(edges: pd.DataFrame, hubs: gpd.GeoDataFrame) -> pd.DataFrame:
    """Add corridors a published source names, and tag every edge's provenance.

    Our inferred corridors come from how close two drawn shapes are. HIFLD's
    line layer instead *names* both terminal substations, so where it agrees
    the corridor is confirmed by an independent source, and where it names a
    pair we never inferred the corridor is asserted rather than guessed.
    HIFLD covers nothing below 100 kV, so the sub-transmission tier stays
    inference-only and is labelled as such.
    """
    path = C.MESO_DIR / "asserted_corridors.csv"
    if "provenance" not in edges.columns:
        edges = edges.assign(provenance="inferred")
    if not path.is_file():
        return edges

    a = pd.read_csv(path)
    known = set(hubs["hub_id"])
    a = a[a["source"].isin(known) & a["target"].isin(known)]
    if a.empty:
        return edges

    pair = lambda df: [tuple(sorted(p)) for p in zip(df["source"], df["target"])]
    have = set(pair(edges))
    a["pair"] = pair(a)
    confirmed = a[a["pair"].isin(have)]
    new = a[~a["pair"].isin(have)].copy()

    edges.loc[[p in set(confirmed["pair"]) for p in pair(edges)], "provenance"] = "confirmed"

    if len(new):
        kv = pd.to_numeric(new["rated_kv"], errors="coerce").fillna(115.0)
        mva = kv.map(lambda v: C.kv_to_mva(v) if hasattr(C, "kv_to_mva") else np.nan)
        if mva.isna().all():
            keys = np.array(list(C.KV_TO_MVA.keys()), dtype=float)
            mva = kv.map(
                lambda v: float(C.KV_TO_MVA[int(keys[np.argmin(np.abs(keys - v))])])
            )
        n_circ = pd.to_numeric(new.get("n_circuits"), errors="coerce").fillna(1.0)
        new_edges = pd.DataFrame(
            {
                "source": new["source"].to_numpy(),
                "target": new["target"].to_numpy(),
                "installed_capacity_W": (mva.to_numpy() * n_circ.to_numpy()) * 1e6,
                "n_lines": n_circ.to_numpy(),
                "rated_mva": mva.to_numpy() * n_circ.to_numpy(),
                "rated_kv": kv.to_numpy(),
                "provenance": "asserted",
            }
        )
        edges = pd.concat([edges, new_edges], ignore_index=True)

    counts = edges["provenance"].value_counts().to_dict()
    print(f"  corridor provenance: {counts}")
    return edges


# A synthetic radial feed is sized to the larger of the load it serves and the
# generation it evacuates, so it cannot become a bulk bypass. Floor keeps a node
# with neither usable.
#
# Sizing on load alone was wrong for generation switchyards, which serve no load
# by definition -- that is why `generation_switchyards` had to keep them as nodes
# in the first place. Every such component fell to the floor, so a switchyard
# with hundreds of MW of wind could export 5 MW and spilled the rest. At
# three-IOU scope this stranded 5,855 MW behind 24 feeds and manufactured 647 GWh
# of curtailment in four weeks -- 65% of all substation spill -- which inflated
# the storage opportunity M_BESS is measured from, because 08_14 sizes the battery
# fleet against spill. The two worst cases were SUB_HIFLD_3316 (870 MW of
# generation, 5 MW feed) and SUB_HIFLD_2620 (811 MW, 5 MW), both Tehachapi-area
# wind switchyards that in reality export over SCE's Tehachapi Renewable
# Transmission Project.
#
# The feed stays radial -- one edge from an islanded component to the main one --
# so it carries no through-flow whatever its rating, and the load and generation
# limits are taken as a maximum rather than a sum because a node exporting its
# generation is not simultaneously importing its peak.
SYNTHETIC_FEED_FLOOR_W = 5e6


def add_synthetic_feeds(edges: pd.DataFrame, hubs: gpd.GeoDataFrame,
                        generators: list[dict] | None = None) -> pd.DataFrame:
    """Give every disconnected node or island an explicit, tagged radial feed.

    A node with no path to the rest of the network cannot import, so its
    demand becomes shortfall or forces local generation -- which is exactly
    the signature of a binding transmission corridor. That matters because
    P_cong is a difference between constrained and relaxed cases: a data gap
    that manufactures scarcity reads as congestion, biasing the headline
    result in the direction of the finding.

    These feeds are an admission that we do not know the wire, not a claim
    that we do. Each is tagged ``synthetic_feed`` so every result can be
    reported with and without them, and each is sized to the larger of the
    load it serves and the generation it evacuates.
    """
    import networkx as nx
    from scipy.spatial import cKDTree

    g = nx.Graph()
    g.add_nodes_from(hubs["hub_id"])
    g.add_edges_from(zip(edges["source"], edges["target"]))
    comps = sorted(nx.connected_components(g), key=len, reverse=True)
    if len(comps) < 2:
        return edges
    main = comps[0]

    ix = hubs.set_index("hub_id")
    kv = pd.to_numeric(ix.get("site_kv"), errors="coerce")
    peak = pd.to_numeric(ix.get("total_peak_W"), errors="coerce").fillna(0.0)

    gen_W = C.injection_capacity_by_hub(generators)

    main_ids = [h for h in ix.index if h in main]
    main_xy = np.column_stack([ix.loc[main_ids].geometry.x, ix.loc[main_ids].geometry.y])
    main_kv = kv.loc[main_ids].to_numpy(dtype=float)
    tree = cKDTree(main_xy)

    rows = []
    gen_led: list[tuple[str, float]] = []
    for comp in comps[1:]:
        members = [h for h in ix.index if h in comp]
        # Attach at the member with the most to move, load or generation;
        # that is where a utility would actually bring the feed in. Ranking on
        # load alone put the feed on an arbitrary member of any component that
        # serves no load, which is every generation switchyard.
        anchor = max(members, key=lambda h: (max(float(peak.get(h, 0.0)),
                                                 gen_W.get(h, 0.0)), h))
        axy = (ix.loc[anchor].geometry.x, ix.loc[anchor].geometry.y)
        a_kv = float(kv.get(anchor, np.nan))

        # Feed from a site at or above this one's voltage where possible: a
        # distribution yard is fed from sub-transmission, not the reverse.
        k = int(min(len(main_ids), 25))
        d, idx = tree.query(axy, k=k)
        d, idx = np.atleast_1d(d), np.atleast_1d(idx)
        pick = None
        for dd, ii in zip(d, idx):
            if np.isnan(a_kv) or np.isnan(main_kv[ii]) or main_kv[ii] + 1e-9 >= a_kv:
                pick = int(ii)
                break
        if pick is None:
            pick = int(idx[0])

        comp_peak = float(sum(float(peak.get(h, 0.0)) for h in members))
        comp_gen = float(sum(gen_W.get(h, 0.0) for h in members))
        cap = max(comp_peak, comp_gen, SYNTHETIC_FEED_FLOOR_W)
        if comp_gen > max(comp_peak, SYNTHETIC_FEED_FLOOR_W):
            gen_led.append((anchor, comp_gen))
        rows.append(
            {
                "source": anchor,
                "target": main_ids[pick],
                "installed_capacity_W": cap,
                "n_lines": 1,
                "rated_mva": cap / 1e6,
                "rated_kv": a_kv if not np.isnan(a_kv) else np.nan,
                "provenance": "synthetic_feed",
            }
        )

    out = pd.concat([edges, pd.DataFrame(rows)], ignore_index=True)
    served = sum(r["installed_capacity_W"] for r in rows)
    print(f"  {len(rows):,} synthetic radial feeds for disconnected components "
          f"({served / 1e9:.1f} GW reconnected)")
    print(f"    {len(gen_led):,} sized by generation rather than load "
          f"({sum(c for _, c in gen_led) / 1e9:.2f} GW of injection that would "
          f"otherwise sit behind the {SYNTHETIC_FEED_FLOOR_W / 1e6:.0f} MW floor)")
    for hid, c in sorted(gen_led, key=lambda t: -t[1])[:8]:
        print(f"      {hid:18} {c / 1e6:8,.0f} MW")
    return out


def load_transmission_lines(hubs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    C.require_file(C.GRIP_TRANSMISSION_LINES, hint="Need unzipped GRIP TransmissionLines.shp")
    grip_lines = _to_albers(gpd.read_file(C.GRIP_TRANSMISSION_LINES))
    grip_lines["line_source"] = "grip"
    frames = [grip_lines]
    if C.HIFLD_TX_LINES_GPKG.is_file():
        extra = _to_albers(gpd.read_file(C.HIFLD_TX_LINES_GPKG))
        extra["line_source"] = "hifld"
        frames.append(extra)
    # Statewide CEC transmission lines cover SCE/SDG&E/LADWP/IID territory
    # that GRIP (PG&E-only) never included.
    if C.CEC_TRANSMISSION_GPKG.is_file():
        cec = _to_albers(gpd.read_file(C.CEC_TRANSMISSION_GPKG))
        cec["line_source"] = "cec"
        frames.append(cec)
    else:
        print("  CEC statewide transmission lines cache missing; non-PG&E lines unavailable.")
    return pd.concat(frames, ignore_index=True)


def _hub_voltage_with_sce_fill(hubs: gpd.GeoDataFrame) -> np.ndarray:
    """Per-hub voltage in kV, HIFLD first, SCE's circuit inventory second.

    HIFLD leaves 541 of 3,104 substations with no voltage at all, and the
    Tehachapi collector yards are among them, so a voltage test on HIFLD alone
    cannot see the very nodes it needs to judge. SCE publishes a voltage on every
    transmission and subtransmission circuit, so a circuit whose geometry passes
    within ``SCE_VOLTAGE_FILL_M`` of a substation attributes its voltage to it --
    the highest, where several pass. That resolves 219 of the 541, taking coverage
    from 82.6% to 89.6%.

    This fill is deliberately local to the generator snap and is NOT written back
    onto ``hubs``. ``site_kv`` also gates mid-span line splitting and corridor
    endpoint snapping in ``_reconstruct_corridors``, where a substation of unknown
    voltage is treated permissively; giving 219 substations a voltage would change
    which lines may terminate there and so rebuild the topology. That may well be
    an improvement, but it is a separate change with its own evidence to weigh,
    and folding it in here would make this one unattributable.
    """
    kv = pd.to_numeric(hubs.get("site_kv"), errors="coerce")
    if "max_kv" in hubs.columns:
        kv = kv.fillna(pd.to_numeric(hubs["max_kv"], errors="coerce"))
    kv = kv.to_numpy(dtype=float)
    circuits = C.DATA_DIR / "ica" / "sce" / "transmission_circuits.gpkg"
    miss = np.isnan(kv)
    if not miss.any() or not circuits.is_file():
        return kv
    sc = gpd.read_file(circuits).to_crs(hubs.crs)
    sc["_kv"] = pd.to_numeric(sc["CIRCUIT_VOLTAGE"], errors="coerce")
    sc = sc[sc["_kv"].notna()]
    if sc.empty:
        return kv
    probe = hubs.loc[miss, ["hub_id", "geometry"]].copy()
    j = gpd.sjoin_nearest(probe, sc[["_kv", "geometry"]], how="left",
                          distance_col="_d", max_distance=SCE_VOLTAGE_FILL_M)
    got = j.dropna(subset=["_kv"]).groupby("hub_id")["_kv"].max()
    if len(got):
        pos = {h: i for i, h in enumerate(hubs["hub_id"].to_numpy())}
        for h, v in got.items():
            kv[pos[h]] = float(v)
        print(f"  substation voltage: {int(np.isfinite(kv).sum())}/{len(kv)} known "
              f"({len(got)} filled from SCE circuit voltages within "
              f"{SCE_VOLTAGE_FILL_M:.0f} m)")
    return kv


def _collector_kv_for(mw: float) -> float:
    """Lowest voltage in C.KV_TO_MVA whose single circuit could carry ``mw``."""
    for kv, mva in sorted(C.KV_TO_MVA.items()):
        if mva >= mw:
            return float(kv)
    return float(max(C.KV_TO_MVA))


def _resnap_large_plants_by_voltage(joined: pd.DataFrame,
                                    hubs: gpd.GeoDataFrame) -> pd.DataFrame:
    """Move large plants off substations that could not collect them.

    Works on plant *sites* -- all units at one coordinate are one plant, the same
    grouping ``generation_switchyards`` uses -- so a multi-unit plant moves whole
    rather than splitting across substations. A site stays where it is if it is
    within ``GEN_SNAP_OWN_YARD_M`` (its own switchyard), if it is below
    ``GEN_SWITCHYARD_MIN_MW``, or if no substation within ``GEN_SNAP_MAX_M`` has a
    voltage able to collect it. The last case is reported rather than forced: 30
    sites and 7,939 MW have no adequate substation in reach, and inventing one
    would be worse than recording that the data does not place them.
    """
    from scipy.spatial import cKDTree

    if not {"lon", "lat"} <= set(joined.columns):
        return joined
    mw = pd.to_numeric(joined["installed_capacity"], errors="coerce").fillna(0.0) / 1e6
    firm = ~joined["optional"].astype(bool)
    site = list(zip(joined["lon"].round(5), joined["lat"].round(5)))
    joined = joined.assign(_site=site)
    site_mw = mw.where(firm, 0.0).groupby(joined["_site"]).sum()

    kv = _hub_voltage_with_sce_fill(hubs)
    hx, hy = hubs.geometry.x.to_numpy(), hubs.geometry.y.to_numpy()
    hid = hubs["hub_id"].to_numpy()
    sid = hubs["substation_id"].to_numpy()
    tree = cKDTree(np.column_stack([hx, hy]))

    geom = joined.geometry
    moves: dict[tuple, tuple[int, float]] = {}
    unreachable: list[tuple[tuple, float]] = []
    for s, total in site_mw.items():
        if total < GEN_SWITCHYARD_MIN_MW:
            continue
        g = geom[joined["_site"] == s].iloc[0]
        if not (np.isfinite(g.x) and np.isfinite(g.y)):
            continue
        need = _collector_kv_for(float(total))
        d, i = tree.query((g.x, g.y), k=int(min(len(hubs), 40)))
        d, i = np.atleast_1d(d), np.atleast_1d(i)
        if float(d[0]) <= GEN_SNAP_OWN_YARD_M:
            continue  # the plant's own switchyard
        for dd, ii in zip(d, i):
            if dd > GEN_SNAP_MAX_M:
                unreachable.append((s, float(total)))
                break
            k = kv[int(ii)]
            if np.isfinite(k) and k + 1e-9 >= need:
                if hid[int(ii)] != joined.loc[joined["_site"] == s, "hub_id"].iloc[0]:
                    moves[s] = (int(ii), float(dd))
                break

    if moves:
        sel = joined["_site"].isin(moves)
        tgt = joined.loc[sel, "_site"].map(lambda s: moves[s][0])
        joined.loc[sel, "hub_id"] = hid[tgt.to_numpy()]
        joined.loc[sel, "substation_id"] = sid[tgt.to_numpy()]
        joined.loc[sel, "snap_m"] = joined.loc[sel, "_site"].map(lambda s: moves[s][1]).to_numpy()
        moved_mw = sum(site_mw[s] for s in moves)
        dists = [v[1] for v in moves.values()]
        print(f"  {len(moves)} plant-sites ({moved_mw:,.0f} MW) re-snapped to a substation "
              f"that can collect them (median {np.median(dists):,.0f} m, "
              f"max {max(dists):,.0f} m)")
    if unreachable:
        tot = sum(v for _, v in unreachable)
        print(f"  {len(unreachable)} plant-sites ({tot:,.0f} MW) have no substation within "
              f"{GEN_SNAP_MAX_M / 1000:.0f} km rated to collect them; left on the nearest node")
    return joined.drop(columns=["_site"])


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
        hub_cols = hubs[["hub_id", "substation_id", "geometry"]].copy()
        if "parent_ba" in hubs.columns:
            hub_cols["hub_parent_ba"] = hubs["parent_ba"].values
        joined = gpd.sjoin_nearest(gdf, hub_cols, how="left", distance_col="snap_m")
        subset = [c for c in ("handle", "parent_ba") if c in joined.columns]
        if subset:
            joined = joined.drop_duplicates(subset=subset, keep="first")
        else:
            joined = joined.drop_duplicates(subset=["handle"], keep="first")
        joined = _resnap_large_plants_by_voltage(joined, hubs)
        for _, r in joined.iterrows():
            mapped.append(
                {
                    "handle": r["handle"],
                    "hub_id": r["hub_id"],
                    "substation_id": r.get("substation_id"),
                    "parent_ba": r["parent_ba"] if "parent_ba" in joined.columns else r.get("hub_parent_ba"),
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


def hub_incident_line_capacity(
    hubs: gpd.GeoDataFrame,
    lines: gpd.GeoDataFrame,
    kv_threshold: float = 0.0,
    snap_m: float = GATEWAY_INCIDENCE_SNAP_M,
) -> dict[str, float]:
    """Total rated capacity of raw line spans terminating at each substation.

    This is measured straight off the source geometry, independent of
    corridor reconstruction, so it counts every circuit that physically
    lands on the busbar -- including the ones whose far end never resolved
    to another model node. Subtracting the capacity already represented as
    modeled corridors leaves the capacity that genuinely leaves the modeled
    network, which is the only defensible rating for a tie to the BA bus.
    """
    from scipy.spatial import cKDTree

    if lines is None or len(lines) == 0 or len(hubs) == 0:
        return {}

    tree = cKDTree(np.column_stack([hubs.geometry.x.to_numpy(), hubs.geometry.y.to_numpy()]))
    ids = hubs["hub_id"].to_numpy()

    # Count each circuit once per substation, not each digitized span. A
    # substation yard typically contains many short spans belonging to the
    # same circuit, and summing every span endpoint inside the snap radius
    # inflates the busbar's incident capacity several-fold (statewide this
    # was the difference between 235 GW and a plausible figure).
    endpoints, caps, line_key = [], [], []
    for li, (_, ln) in enumerate(lines.iterrows()):
        if C.line_rated_kv(ln) < kv_threshold:
            continue
        cap = C.line_limit_w(ln)
        for part in _line_parts(ln.geometry):
            coords = list(part.coords)
            if len(coords) < 2:
                continue
            for xy in (coords[0][:2], coords[-1][:2]):
                endpoints.append(xy)
                caps.append(cap)
                line_key.append(li)

    if not endpoints:
        return {}

    dist, idx = tree.query(np.asarray(endpoints, dtype=float), k=1)
    per_hub: dict[str, dict[int, float]] = {}
    for d, i, cap, li in zip(dist, idx, caps, line_key):
        if d > snap_m:
            continue
        per_hub.setdefault(ids[i], {})[li] = cap
    return {hid: sum(v.values()) for hid, v in per_hub.items()}


def voltage_tier_gateways(
    hubs: gpd.GeoDataFrame,
    edges: pd.DataFrame,
    line_rows: list[dict],
    generators: list[dict] | None = None,
    kv_threshold: float = GATEWAY_KV_THRESHOLD,
    capacity_floor_w: float = GATEWAY_CAPACITY_FLOOR_W,
    raw_incident: dict[str, float] | None = None,
) -> pd.DataFrame:
    """BA<->substation interfaces at every bulk-transmission boundary substation.

    Every substation touching a line at/above ``kv_threshold`` in each
    connected component becomes a gateway to that component's BA. Components
    with no line at/above the threshold fall back to a single gateway (the
    substation with the largest EV energy), so small/isolated pieces of the
    network still connect somewhere.

    Sizing is the part that matters. A gateway is a tie to the rest of the
    BA's system, so it may only carry the capacity that actually leaves the
    modeled network: ``raw_incident`` (every circuit landing on the busbar,
    measured off source geometry) minus the capacity already represented as
    modeled corridors at that same substation. Sizing a gateway off total
    incident high-voltage capacity instead -- as this did previously --
    counts each line twice, once as the corridor to its neighbouring
    substation and again as a radial tie to the BA bus, and the BA node is a
    single bus with no internal limit. That produced 372 GW of gateway
    capacity against a 63 GW statewide peak (SDG&E: 45 GW against a 5.4 GW
    peak), letting flow hop substation -> BA bus -> substation around any
    modeled corridor, so no corridor could ever bind.

    Interface capacity accounts for local generation as well as load: a
    substation can be an isolated single-node component and still host a
    huge power plant (e.g. Diablo Canyon's 2,240 MW landed on one PG&E
    substation with no captured nearby lines) -- sizing the gateway off load
    alone left ~2,200 MW of nuclear output with nowhere to go, forcing it
    into the wastage slack regardless of cost (57% of one four-week run's
    total wastage came from this single node).
    """
    import networkx as nx

    raw_incident = raw_incident or {}

    incident_hv_capacity: dict[str, float] = {}
    for r in line_rows:
        if r["rated_kv"] < kv_threshold:
            continue
        incident_hv_capacity[r["source"]] = incident_hv_capacity.get(r["source"], 0.0) + r["installed_capacity_W"]
        incident_hv_capacity[r["target"]] = incident_hv_capacity.get(r["target"], 0.0) + r["installed_capacity_W"]

    generation_capacity: dict[str, float] = {}
    for rec in generators or []:
        hid = rec.get("hub_id")
        cap = rec.get("installed_capacity")
        if not hid or cap is None:
            continue
        try:
            cap = float(cap)
        except (TypeError, ValueError):
            continue
        if cap > 0:
            generation_capacity[hid] = generation_capacity.get(hid, 0.0) + cap

    # Capacity already represented inside the modeled network at each hub.
    modeled_incident: dict[str, float] = {}
    for _, e in edges.iterrows():
        cap = float(e["installed_capacity_W"])
        modeled_incident[e["source"]] = modeled_incident.get(e["source"], 0.0) + cap
        modeled_incident[e["target"]] = modeled_incident.get(e["target"], 0.0) + cap

    g = nx.Graph()
    g.add_nodes_from(hubs["hub_id"].tolist())
    for _, e in edges.iterrows():
        g.add_edge(e["source"], e["target"])
    hubs_ix = hubs.set_index("hub_id")

    rows = []
    for comp in nx.connected_components(g):
        members = hubs_ix.loc[list(comp)]
        candidates = [hid for hid in members.index if incident_hv_capacity.get(hid, 0.0) > 0]
        if candidates:
            component_peak_w = float(members["total_peak_W"].sum()) if "total_peak_W" in members else 0.0
            component_gen_w = sum(generation_capacity.get(hid, 0.0) for hid in members.index)
            sized = []
            for hid in candidates:
                # Only the capacity that leaves the modeled network. Where
                # raw geometry is unavailable for a hub this falls back to
                # the modeled incident capacity, so the tie is never larger
                # than what physically lands on the busbar.
                raw = raw_incident.get(hid)
                if raw is None:
                    external = 0.0
                else:
                    external = max(0.0, raw - modeled_incident.get(hid, 0.0))
                sized.append((hid, max(external, generation_capacity.get(hid, 0.0))))

            # A component must still be able to import its own peak demand:
            # if every one of its gateways is fully accounted for by modeled
            # corridors, the external total can legitimately be ~0 and the
            # component would be islanded with no supply. Top the gateways up
            # pro rata to exactly the component's own peak -- enough to serve
            # its load, and not a watt of spare headroom that could be reused
            # as a bypass around the corridors.
            total = sum(c for _, c in sized)
            need = max(component_peak_w, component_gen_w)
            if total < need:
                deficit = need - total
                share = deficit / len(sized)
                sized = [(hid, c + share) for hid, c in sized]

            for hid, cap in sized:
                rows.append(
                    {
                        "hub_id": hid,
                        "parent_ba": members.loc[hid, "parent_ba"],
                        "interface_capacity_W": max(capacity_floor_w, cap),
                        "n_substations": len(members),
                        "role": "voltage_gateway",
                    }
                )
        else:
            # No line >= threshold anywhere in this component: fall back to
            # one gateway at the highest-EV-energy substation. Size the
            # interface to the component's real assigned peak demand AND its
            # total local generation capacity -- a flat 20 MW cap regardless
            # of load or generation previously stranded substations assigned
            # hundreds of MW of load (e.g. one substation's w_s-weighted base
            # load alone was 228 MW) or, separately, GW-scale power plants
            # (e.g. Diablo Canyon's 2,240 MW) behind a fixed floor sized for a
            # small distribution tap.
            #
            # No headroom multiplier: this is a radial feed, and a radial feed
            # cannot carry more than the load it serves plus the generation it
            # collects. The 1.5x that used to be applied here was 19 GW of
            # spare capacity across 846 components, all of it usable as a
            # bypass through the BA bus rather than as anything physical.
            gw = members["mean_week_kwh"].idxmax() if "mean_week_kwh" in members else members.index[0]
            component_peak_w = float(members["total_peak_W"].sum()) if "total_peak_W" in members else 0.0
            component_gen_w = sum(generation_capacity.get(hid, 0.0) for hid in members.index)
            rows.append(
                {
                    "hub_id": gw,
                    "parent_ba": members.loc[gw, "parent_ba"],
                    "interface_capacity_W": max(capacity_floor_w, component_peak_w, component_gen_w),
                    "n_substations": len(members),
                    "role": "component_gateway_fallback",
                }
            )

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    # Top up each BA to its own peak demand.
    #
    # The per-component top-up above only fires per connected component. Once
    # synthetic feeds collapse California into a single component, a BA whose
    # substations never touch a 230 kV line can end up with almost no
    # interface at all -- measured: IID at 0.09 GW against 0.73 GW of peak,
    # SCE at 0.66x. A BA that cannot import its own demand manufactures
    # shortfall, which reads as congestion in exactly the metric these
    # scenarios exist to measure. Scale each BA's gateways pro rata to cover
    # its peak, and no further: no spare headroom to reuse as a bypass.
    ba_peak = (
        hubs.groupby("parent_ba")["total_peak_W"].sum()
        if "total_peak_W" in hubs.columns
        else pd.Series(dtype=float)
    )
    for ba, need in ba_peak.items():
        sel = out["parent_ba"] == ba
        have = float(out.loc[sel, "interface_capacity_W"].sum())
        # Small margin so the scaled total lands strictly above peak rather
        # than exactly on it, where float rounding leaves a BA 1% short.
        target = float(need) * 1.02
        if have <= 0 or have >= target or not sel.any():
            continue
        out.loc[sel, "interface_capacity_W"] *= target / have
        print(f"    {ba}: interface topped up {have / 1e9:.2f} -> {target / 1e9:.2f} GW")

    return out


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


def main(aggregate: int | None = None, no_synthetic_feeds: bool = False) -> None:
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
    junction_xy = load_junction_points(hubs)
    line_rows = _reconstruct_corridors(hubs, lines, junction_xy=junction_xy)
    edges = build_edges(hubs, lines, rows=line_rows)
    edges = merge_asserted_corridors(edges, hubs)
    # Generators are mapped before the synthetic feeds because a feed to a
    # generation switchyard has to be sized to what injects there, and that is
    # only known once the plants are snapped to hubs.
    print("Spatial join: WECC CA generators / storage -> nearest substation...")
    generators = map_wecc_generators(hubs)
    if not no_synthetic_feeds:
        edges = add_synthetic_feeds(edges, hubs, generators=generators)
    print(f"  {len(edges)} aggregated corridors")

    print("Snapping named interties (Path 15 / 26 / 66 / Palo Verde)...")
    interties = map_interties(hubs)
    bess = candidate_bess(hubs)
    raw_incident = hub_incident_line_capacity(hubs, lines, kv_threshold=GATEWAY_KV_THRESHOLD)
    interfaces = voltage_tier_gateways(
        hubs, edges, line_rows, generators=generators, raw_incident=raw_incident
    )
    print(
        f"  {len(interfaces)} BA gateway interfaces "
        f"({(interfaces['role'] == 'voltage_gateway').sum()} voltage-tier, "
        f"{(interfaces['role'] == 'component_gateway_fallback').sum()} fallback)"
    )
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
                    "interface_capacity_W": float(
                        C.INTERTIE_POINTS.get(it["intertie_id"], {}).get("rating_W", 500e6)
                    ),
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
    parser.add_argument(
        "--no-synthetic-feeds",
        action="store_true",
        help="Leave disconnected components islanded instead of adding tagged radial feeds.",
    )
    _a = parser.parse_args()
    main(aggregate=_a.aggregate, no_synthetic_feeds=_a.no_synthetic_feeds)
