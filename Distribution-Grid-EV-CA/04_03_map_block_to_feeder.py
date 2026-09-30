"""
04_03_map_block_to_feeder.py — Python port of ``04_03_map_block to feeder.R``.

Requires ``geopandas`` and ``pyogrio``/``fiona``.
"""

from __future__ import annotations

import os
import time
from typing import Iterable

import geopandas as gpd
import pandas as pd

# ---------------------------------------------------------------------------
# Paths mirror the R script
# ---------------------------------------------------------------------------
PGE_SHP = "data/mapping/block to feeder 2010/shape all/PGE feeders.shp"
SCE_SHP = "data/mapping/block to feeder 2010/shape all/SCE circuits.shp"
SDGE_SHP = "data/mapping/block to feeder 2010/shape SDGE lines merge/SDGE Feeder lines.shp"

ALL_FEEDERS_OUT = "data/mapping/block to feeder 2010/shape all/all feeders_map_line.shp"
BLOCK_SHP = "data/mapping/census block to TAZ 2010/shape_block_2010/tl_2019_06_tabblock10.shp"

NEAREST_OUT = "data/mapping/block to feeder 2010/shape mapped/block 2010 to feeder_map_line_nearest.shp"
INTERSECT_IN = "data/mapping/block to feeder 2010/shape mapped/block 2010 to feeder_map_line_intersect.shp"
COMBO_OUT = "data/mapping/block to feeder 2010/shape mapped/block 2010 to feeder_map_line_combo.shp"
UNASSIGNED_OUT = "data/mapping/block to feeder 2010/shape mapped/feeder_unassigned_map_line_combo.shp"


def _prep_utility(path: str, id_field: str, utility: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)[[id_field, "geometry"]].copy()
    gdf = gdf.rename(columns={id_field: "FeederID"})
    gdf["utility"] = utility
    return gdf


def combine_feeders() -> gpd.GeoDataFrame:
    pge = _prep_utility(PGE_SHP, "FeederID", "PGE")
    sce = _prep_utility(SCE_SHP, "CIRCUIT_NA", "SCE")
    sdge = _prep_utility(SDGE_SHP, "FEEDERID", "SDGE")
    pge = pge.to_crs(sce.crs)
    sdge = sdge.to_crs(sce.crs)
    feeders_all = pd.concat([sdge, sce, pge], ignore_index=True)
    feeders_all = gpd.GeoDataFrame(feeders_all, geometry="geometry", crs=sce.crs)
    os.makedirs(os.path.dirname(ALL_FEEDERS_OUT), exist_ok=True)
    feeders_all.to_file(ALL_FEEDERS_OUT)
    return feeders_all


def nearest_feeder(feeders_all: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    block = gpd.read_file(BLOCK_SHP)[["GEOID10", "geometry"]].to_crs(feeders_all.crs)
    t0 = time.time()
    joined = gpd.sjoin_nearest(block, feeders_all, how="left")
    print(f"nearest join took {time.time() - t0:.1f}s")
    os.makedirs(os.path.dirname(NEAREST_OUT), exist_ok=True)
    joined[["GEOID10", "FeederID", "utility", "geometry"]].to_file(NEAREST_OUT)
    return joined


def main() -> None:
    feeders_all = combine_feeders()
    nearest = nearest_feeder(feeders_all)
    assigned = set(nearest["FeederID"].dropna().unique().tolist())
    print(f"assigned feeders (nearest): {len(assigned)} / {feeders_all['FeederID'].nunique()}")

    # The intersect option comes from QGIS; expose it as a simple file read.
    if os.path.isfile(INTERSECT_IN):
        block_inter = gpd.read_file(INTERSECT_IN)[["GEOID10", "FeederID", "utility", "geometry"]]
        intersect_blocks = block_inter.dropna(subset=["FeederID"])
        rest = nearest[~nearest["GEOID10"].isin(intersect_blocks["GEOID10"])].copy()
        rest = rest[["GEOID10", "FeederID", "utility", "geometry"]]
        intersect_blocks = intersect_blocks.to_crs(rest.crs)
        combo = pd.concat([intersect_blocks, rest], ignore_index=True)
        combo = gpd.GeoDataFrame(combo, geometry="geometry", crs=rest.crs)
        combo.to_file(COMBO_OUT)

        unassigned = feeders_all[~feeders_all["FeederID"].isin(combo["FeederID"].unique())]
        unassigned.to_file(UNASSIGNED_OUT)


if __name__ == "__main__":
    main()
