"""
08_02_map_block_to_feeder.py

TAZ -> census block -> feeder -> substation, following the method used in
Li & Jenn (2024, PNAS), whose code ships in ``R_Yanning/``.

Why this replaces the k-nearest-substation rule
-----------------------------------------------
``08_01`` assigns TAZ load to substations by inverse-distance weighting over
the k nearest substations. Distance is a proxy for "which substation serves
this load", and a poor one: a TAZ can sit beside a substation that serves a
different feeder entirely. The ASTR2026 extended abstract already calls the
nearest-centroid rule "a screening proxy rather than a feeder-boundary model"
and commits to replacing it once feeder maps are available. They now are.

The chain (mirroring R_Yanning/04_01 and 04_03)
-----------------------------------------------
1. Block shares within TAZ (``04_01_get_share_block vs TAZ.R``):
   population share for home charging, LODES workplace jobs share for work and
   public charging. Both renormalised within each TAZ.
2. Block -> feeder (``04_03_map_block to feeder.R``): assign by *intersection*
   where a block polygon actually meets a feeder line, taking the longest
   intersection; fall back to *nearest feeder* for blocks that meet none. That
   two-pass combination is exactly what the R code does, and it matters --
   nearest alone silently attaches rural blocks to a feeder that merely passes
   near them.
3. Feeder -> substation: PG&E ``FeederDetail.Substation`` and SCE
   ``distribution_circuits.sub_name`` both name the parent substation, so the
   last hop needs no geometry at all.

Writes:
  data/mapping/block_to_feeder.csv          block -> feeder, utility, method
  data/mapping/taz_to_substation_feeder.csv TAZ -> substation, w_home, w_work
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd

import common as C

CENSUS_DIR = C.DATA_DIR / "census"
BLOCK_ZIP = CENSUS_DIR / "tabblock2010_06_pophu.zip"
LODES_GZ = CENSUS_DIR / "ca_wac_S000_JT00_2019.csv.gz"
SCE_CIRCUITS = C.DATA_DIR / "ica" / "sce" / "distribution_circuits.gpkg"

BLOCK_TO_FEEDER_CSV = C.DATA_DIR / "mapping" / "block_to_feeder.csv"
TAZ_TO_SUB_FEEDER_CSV = C.DATA_DIR / "mapping" / "taz_to_substation_feeder.csv"


def _norm(name: str) -> str:
    """Normalise a substation name for cross-source matching."""
    s = str(name or "").upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(SUBSTATION|SUB|STATION|SWITCHING|TAP)\b", " ", s)
    s = re.sub(r"\b\d{1,3}\s*/\s*\d{1,3}\s*KV\b", " ", s)
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return s.strip()


def load_feeders() -> gpd.GeoDataFrame:
    """Unified feeder lines with their parent substation, per utility."""
    frames = []

    C.require_file(C.GRIP_FEEDER_DETAIL, hint="Need GRIP FeederDetail.shp")
    pge = gpd.read_file(C.GRIP_FEEDER_DETAIL)
    pge = pge.rename(columns={"FeederID": "feeder_id", "Substation": "sub_name"})
    pge["utility"] = "PGE"
    frames.append(pge[["feeder_id", "sub_name", "utility", "geometry"]].to_crs(C.CA_ALBERS_CRS))

    if SCE_CIRCUITS.is_file():
        sce = gpd.read_file(SCE_CIRCUITS)
        sce = sce.rename(columns={"circt_nam": "feeder_id", "sub_name": "sub_name"})
        sce["utility"] = "SCE"
        frames.append(sce[["feeder_id", "sub_name", "utility", "geometry"]].to_crs(C.CA_ALBERS_CRS))
    else:
        print(f"  WARNING: {SCE_CIRCUITS} missing - SCE feeders not included")

    # SDG&E publishes ICA under the same CPUC mandate but from its own portal;
    # no REST endpoint located yet, so its ~7% of state load still falls back
    # to the k-nearest rule downstream.
    out = pd.concat(frames, ignore_index=True)
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=C.CA_ALBERS_CRS)
    return out[out.geometry.notna() & ~out.geometry.is_empty].reset_index(drop=True)


def load_blocks() -> gpd.GeoDataFrame:
    """2010 census blocks with population."""
    C.require_file(BLOCK_ZIP, hint="Download TIGER2010BLKPOPHU tabblock2010_06_pophu.zip")
    with zipfile.ZipFile(BLOCK_ZIP) as zf:
        shp = [n for n in zf.namelist() if n.lower().endswith(".shp")]
    gdf = gpd.read_file(f"zip://{BLOCK_ZIP}!{shp[0]}")
    id_col = C.pick_column(gdf.columns, ("BLOCKID10", "GEOID10", "GEOID"))
    pop_col = C.pick_column(gdf.columns, ("POP10", "POP", "POP100"))
    gdf = gdf.rename(columns={id_col: "block_id", pop_col: "pop"})
    gdf["block_id"] = gdf["block_id"].astype(str).str.zfill(15)
    gdf["pop"] = pd.to_numeric(gdf["pop"], errors="coerce").fillna(0.0)
    return gdf[["block_id", "pop", "geometry"]].to_crs(C.CA_ALBERS_CRS)


def load_jobs() -> pd.DataFrame:
    """LODES workplace-area jobs per block (C000 = total jobs)."""
    if not LODES_GZ.is_file():
        print(f"  WARNING: {LODES_GZ} missing - workplace weights fall back to population")
        return pd.DataFrame(columns=["block_id", "jobs"])
    df = pd.read_csv(LODES_GZ, usecols=["w_geocode", "C000"], compression="gzip")
    df["block_id"] = df["w_geocode"].astype(str).str.zfill(15)
    df["jobs"] = pd.to_numeric(df["C000"], errors="coerce").fillna(0.0)
    return df[["block_id", "jobs"]]


# Which CEC territory names belong to each utility whose feeders we hold.
TERRITORY_UTILITY = (
    ("pacific gas", "PGE"),
    ("southern california edison", "SCE"),
)


def _mask_to_territory(mapped: pd.DataFrame, blocks: gpd.GeoDataFrame) -> pd.DataFrame:
    """Drop feeder assignments outside the assigning utility's territory."""
    if not C.CEC_LSE_IOU_POU_GPKG.is_file():
        print("    WARNING: no CEC territory layer; feeder assignments left unmasked")
        return mapped

    terr = gpd.read_file(C.CEC_LSE_IOU_POU_GPKG).to_crs(blocks.crs)
    name_col = C.pick_column(terr.columns, ("Utility", "OnlineName", "Acronym"))

    def _util(name):
        low = str(name or "").lower()
        for frag, tag in TERRITORY_UTILITY:
            if frag in low:
                return tag
        return None

    terr["util"] = terr[name_col].map(_util)
    terr = terr[terr["util"].notna()][["util", "geometry"]]
    if terr.empty:
        return mapped

    pts = blocks[["block_id", "geometry"]].copy()
    pts["geometry"] = pts.geometry.representative_point()
    j = gpd.sjoin(pts, terr, how="left", predicate="within").drop_duplicates("block_id")
    block_util = j.set_index("block_id")["util"]

    before = len(mapped)
    keep = mapped["block_id"].map(block_util) == mapped["utility"]
    out = mapped[keep].copy()
    print(f"    territory mask: kept {len(out):,} of {before:,} feeder assignments "
          f"({before - len(out):,} outside the assigning utility's territory)")
    return out


def assign_blocks_to_feeders(blocks: gpd.GeoDataFrame, feeders: gpd.GeoDataFrame) -> pd.DataFrame:
    """Intersection first, nearest feeder as fallback (R_Yanning/04_03)."""
    print("  pass 1: blocks intersecting a feeder line...")
    hit = gpd.sjoin(
        blocks[["block_id", "geometry"]], feeders, how="inner", predicate="intersects"
    )
    print(f"    {hit['block_id'].nunique():,} blocks intersect at least one feeder "
          f"({len(hit):,} block-feeder pairs)")

    # Blocks touching exactly one feeder need no further work; only the
    # ambiguous ones pay the cost of measuring intersection length.
    counts = hit.groupby("block_id")["feeder_id"].transform("size")
    single = hit[counts == 1][["block_id", "feeder_id", "sub_name", "utility"]]
    multi = hit[counts > 1]
    print(f"    {len(single):,} unambiguous, {multi['block_id'].nunique():,} need longest-intersection")

    resolved = [single]
    if len(multi):
        bg = blocks.set_index("block_id").geometry
        fg = feeders.geometry
        m = multi.reset_index(drop=True)
        inter_len = gpd.GeoSeries(
            bg.loc[m["block_id"]].values, crs=blocks.crs
        ).intersection(gpd.GeoSeries(fg.loc[m["index_right"]].values, crs=blocks.crs)).length
        m = m.assign(inter_len=np.asarray(inter_len))
        best = m.sort_values("inter_len", ascending=False).drop_duplicates("block_id")
        resolved.append(best[["block_id", "feeder_id", "sub_name", "utility"]])

    by_intersection = pd.concat(resolved, ignore_index=True)
    by_intersection["method"] = "intersect"

    # Only PG&E and SCE publish feeders, so "nearest feeder" outside their
    # territories silently hands LADWP, BANC, IID and SDG&E demand to an IOU
    # substation hundreds of kilometres away. Measured when this was missing:
    # IID's assigned peak fell to zero and SCE's rose to 36.7 GW against a
    # real 24 GW. Blocks outside the mapped territories are left unassigned
    # here and fall back to the k-nearest rule downstream.
    by_intersection = _mask_to_territory(by_intersection, blocks)

    print("  pass 2: nearest feeder for the remainder...")
    rest = blocks[~blocks["block_id"].isin(by_intersection["block_id"])]
    print(f"    {len(rest):,} blocks intersect nothing")
    if len(rest):
        near = gpd.sjoin_nearest(
            rest[["block_id", "geometry"]], feeders, how="left", distance_col="dist_m"
        ).drop_duplicates("block_id")
        near = near[["block_id", "feeder_id", "sub_name", "utility"]]
        near["method"] = "nearest"
        near = _mask_to_territory(near, blocks)
        by_intersection = pd.concat([by_intersection, near], ignore_index=True)

    return by_intersection.dropna(subset=["feeder_id"])


SCE_SUBSTATION_SNAP_M = 2000.0


def resolve_substation_ids(mapped: pd.DataFrame) -> pd.DataFrame:
    """Attach our model substation_id to each feeder's parent substation.

    PG&E resolves by name because GRIP's feeder table and substation layer
    share a naming convention. SCE does not: SCE names a yard "Universal
    69/12 kV" where HIFLD calls it "Universal City", which left 29,889 SCE
    blocks unresolved. SCE therefore resolves by *location* -- its published
    substation points are snapped to the nearest model node -- with name
    matching kept only as a fallback.
    """
    mapped = mapped.copy()
    mapped["sub_norm"] = mapped["sub_name"].map(_norm)

    name_lut: dict[tuple[str, str], str] = {}
    grip = gpd.read_file(C.GRIP_ED_SUBSTATIONS)
    gid = C.pick_column(grip.columns, ("Substati00", "SUBSTATION", "SubstationID"))
    gnm = C.pick_column(grip.columns, ("Substation", "NAME", "Name"))
    for _, r in grip.iterrows():
        key = _norm(r[gnm])
        if key:
            name_lut.setdefault(("PGE", key), str(r[gid]))

    nodes = None
    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        hif = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG).to_crs(C.CA_ALBERS_CRS)
        hif["substation_id"] = "HIFLD_" + hif["OBJECTID"].astype(str)
        nm = C.pick_column(hif.columns, ("Name", "NAME", "Substation"))
        for _, r in hif.iterrows():
            key = _norm(r[nm])
            if key:
                name_lut.setdefault(("SCE", key), r["substation_id"])
        nodes = hif[["substation_id", "geometry"]]

    # SCE: name -> published point -> nearest model node.
    geom_lut: dict[str, str] = {}
    sce_pts = C.DATA_DIR / "ica" / "sce" / "ica_substations_geom.gpkg"
    if sce_pts.is_file() and nodes is not None:
        pts = gpd.read_file(sce_pts).to_crs(C.CA_ALBERS_CRS)
        name_col = C.pick_column(pts.columns, ("SUB_NAME", "sub_name", "NAME"))
        joined = gpd.sjoin_nearest(
            pts[[name_col, "geometry"]], nodes, how="left", distance_col="d"
        )
        joined = joined[joined["d"] <= SCE_SUBSTATION_SNAP_M]
        for _, r in joined.iterrows():
            key = _norm(r[name_col])
            if key:
                geom_lut.setdefault(key, r["substation_id"])
        print(f"    SCE: {len(geom_lut):,} of {len(pts):,} published substations "
              f"snapped to a model node within {SCE_SUBSTATION_SNAP_M:.0f} m")

    def _lookup(util: str, key: str):
        if util == "SCE":
            hit = geom_lut.get(key)
            if hit:
                return hit
        return name_lut.get((util, key))

    mapped["substation_id"] = [
        _lookup(u, k) for u, k in zip(mapped["utility"], mapped["sub_norm"])
    ]
    for util in sorted(mapped["utility"].unique()):
        sel = mapped["utility"] == util
        ok = mapped.loc[sel, "substation_id"].notna().sum()
        print(f"    {util}: {ok:,}/{sel.sum():,} blocks resolved to a model substation "
              f"({100 * ok / max(sel.sum(), 1):.0f}%)")
    return mapped


def build_taz_weights(mapped: pd.DataFrame, blocks: gpd.GeoDataFrame,
                      jobs: pd.DataFrame) -> pd.DataFrame:
    """TAZ -> substation weights, split into home (population) and work (jobs)."""
    taz = gpd.read_file(C.TAZ_POLYGON_SHP)
    taz_id = C.pick_column(taz.columns, ("TAZ12", "TAZ", "TAZ_Zone", "taz_id", "ID"))
    taz = taz.rename(columns={taz_id: "TAZ"})[["TAZ", "geometry"]].to_crs(blocks.crs)

    pts = blocks.copy()
    pts["geometry"] = pts.geometry.representative_point()
    bt = gpd.sjoin(pts[["block_id", "pop", "geometry"]], taz, how="left", predicate="within")
    miss = bt["TAZ"].isna()
    if miss.any():
        fix = gpd.sjoin_nearest(
            pts.loc[pts["block_id"].isin(bt.loc[miss, "block_id"]), ["block_id", "geometry"]],
            taz, how="left",
        ).drop_duplicates("block_id").set_index("block_id")["TAZ"]
        bt.loc[miss, "TAZ"] = bt.loc[miss, "block_id"].map(fix)
    bt = bt.dropna(subset=["TAZ"])[["block_id", "TAZ", "pop"]]

    df = bt.merge(jobs, on="block_id", how="left").fillna({"jobs": 0.0})
    df = df.merge(mapped[["block_id", "substation_id", "utility", "method"]], on="block_id", how="inner")
    df = df.dropna(subset=["substation_id"])

    # Shares within TAZ: population for home charging, jobs for work/public.
    #
    # Both need a fallback chain, or demand vanishes. LODES records jobs only
    # at workplace blocks, so it covers 259k of 710k blocks; 811 TAZs holding
    # 14.8% of statewide EV energy have no recorded jobs in any of their
    # blocks and would otherwise receive a zero work/public weight. A zero
    # weight is not "no charging here" -- it is "no jobs data here" -- so the
    # weight falls back to population, then to an equal split across the TAZ's
    # blocks. Every TAZ therefore sums to exactly 1 and no energy is dropped.
    df["pop_taz"] = df.groupby("TAZ")["pop"].transform("sum")
    df["jobs_taz"] = df.groupby("TAZ")["jobs"].transform("sum")
    df["n_taz"] = df.groupby("TAZ")["block_id"].transform("size")
    equal = 1.0 / df["n_taz"]

    df["w_home"] = np.where(df["pop_taz"] > 0, df["pop"] / df["pop_taz"], equal)
    df["w_work"] = np.where(
        df["jobs_taz"] > 0,
        df["jobs"] / df["jobs_taz"],
        np.where(df["pop_taz"] > 0, df["pop"] / df["pop_taz"], equal),
    )
    df["home_basis"] = np.where(df["pop_taz"] > 0, "population", "equal")
    df["work_basis"] = np.where(
        df["jobs_taz"] > 0, "jobs", np.where(df["pop_taz"] > 0, "population", "equal")
    )

    out = (
        df.groupby(["TAZ", "substation_id", "utility"], as_index=False)
        .agg(w_home=("w_home", "sum"), w_work=("w_work", "sum"),
             n_blocks=("block_id", "size"),
             home_basis=("home_basis", "first"), work_basis=("work_basis", "first"))
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    if args.skip_existing and TAZ_TO_SUB_FEEDER_CSV.is_file():
        print(f"{TAZ_TO_SUB_FEEDER_CSV} exists; nothing to do")
        return

    print("Loading feeder lines...")
    feeders = load_feeders()
    print(f"  {len(feeders):,} feeder geometries "
          f"({feeders.groupby('utility').size().to_dict()})")

    print("Loading census blocks...")
    blocks = load_blocks()
    print(f"  {len(blocks):,} blocks, {blocks['pop'].sum():,.0f} people")

    print("Loading LODES workplace jobs...")
    jobs = load_jobs()
    print(f"  {len(jobs):,} blocks with jobs, {jobs['jobs'].sum():,.0f} total")

    print("Assigning blocks to feeders...")
    mapped = assign_blocks_to_feeders(blocks, feeders)
    print(f"  {len(mapped):,} blocks mapped "
          f"({mapped['method'].value_counts().to_dict()})")

    print("Resolving feeder parent substations to model node ids...")
    mapped = resolve_substation_ids(mapped)

    C.ensure_dir(BLOCK_TO_FEEDER_CSV.parent)
    mapped.drop(columns=["sub_norm"]).to_csv(BLOCK_TO_FEEDER_CSV, index=False)
    print(f"  wrote {BLOCK_TO_FEEDER_CSV}")

    print("Building TAZ -> substation weights...")
    out = build_taz_weights(mapped, blocks, jobs)
    out.to_csv(TAZ_TO_SUB_FEEDER_CSV, index=False)
    print(f"  wrote {TAZ_TO_SUB_FEEDER_CSV}  ({len(out):,} rows, "
          f"{out['TAZ'].nunique():,} TAZs, {out['substation_id'].nunique():,} substations)")
    print(f"  mean substations per TAZ: {len(out) / max(out['TAZ'].nunique(), 1):.2f}")
    for col in ("w_home", "w_work"):
        ser = out.groupby("TAZ")[col].sum()
        print(f"  {col}: {(ser.round(6) == 1.0).sum():,}/{len(ser):,} TAZs sum to 1")
    print(f"  home weight basis: {out.groupby('home_basis')['TAZ'].nunique().to_dict()}")
    print(f"  work weight basis: {out.groupby('work_basis')['TAZ'].nunique().to_dict()}")


if __name__ == "__main__":
    main()
