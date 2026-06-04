"""
Build ``map bg to TAZ 2010.csv`` by spatially joining 2010 block groups to CSTDM TAZs.

Inputs:
- ``data/tl_2010_06_bg10/tl_2010_06_bg10.shp`` (Census 2010 block groups)
- ``data/shps/taz_id.shp`` (CSTDM TAZ polygons, EPSG:3310)

Output:
- ``data/mapping/census bg to tract 2010/map bg to TAZ 2010.csv``
  with columns ``GEOID`` (numeric block-group id) and ``TAZ_Zone`` (TAZ12 id)
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
BG_SHP = BASE_DIR / "data" / "tl_2010_06_bg10" / "tl_2010_06_bg10.shp"
TAZ_SHP = BASE_DIR / "data" / "shps" / "taz_id.shp"
POP_SHARE = (
    BASE_DIR
    / "data"
    / "mapping"
    / "census bg to tract 2010"
    / "popluation share bg to tract 2010.csv"
)
OUT_CSV = BASE_DIR / "data" / "mapping" / "census bg to tract 2010" / "map bg to TAZ 2010.csv"


def _bg_geoid_numeric(geoid10: pd.Series) -> pd.Series:
    return pd.to_numeric(geoid10.astype(str), errors="coerce")


def main() -> None:
    if not BG_SHP.is_file():
        raise SystemExit(f"Missing block-group shapefile: {BG_SHP}")
    if not TAZ_SHP.is_file():
        raise SystemExit(f"Missing TAZ shapefile: {TAZ_SHP}")

    bg = gpd.read_file(BG_SHP)[["GEOID10", "geometry"]].copy()
    taz = gpd.read_file(TAZ_SHP)[["TAZ12", "geometry"]].copy()

    bg["GEOID"] = _bg_geoid_numeric(bg["GEOID10"])
    bg = bg.loc[bg["GEOID"].notna()].copy()

    # Align CRS: TAZ is EPSG:3310 (California Albers); BG is geographic.
    bg_proj = bg.to_crs(taz.crs)
    bg_proj["geometry"] = bg_proj.geometry.centroid

    # Prefer centroid within TAZ; fall back to nearest TAZ for edge cases.
    within = gpd.sjoin(
        bg_proj,
        taz,
        how="left",
        predicate="within",
    )
    missing = within["TAZ12"].isna()
    if missing.any():
        nearest = gpd.sjoin_nearest(
            bg_proj.loc[missing, ["GEOID10", "GEOID", "geometry"]],
            taz,
            how="left",
        )
        within.loc[missing, "TAZ12"] = nearest["TAZ12"].to_numpy()

    out = (
        within[["GEOID", "TAZ12"]]
        .drop_duplicates(subset=["GEOID"])
        .rename(columns={"TAZ12": "TAZ_Zone"})
        .astype({"TAZ_Zone": int})
        .sort_values("GEOID")
        .reset_index(drop=True)
    )

    if POP_SHARE.is_file():
        expected = pd.read_csv(POP_SHARE, usecols=["GEOIDbg"]).drop_duplicates()
        merged = expected.merge(out, left_on="GEOIDbg", right_on="GEOID", how="left")
        n_missing = merged["TAZ_Zone"].isna().sum()
        print(f"Matched {len(out):,} block groups to TAZs")
        print(f"Population-share table: {len(expected):,} BGs; missing TAZ: {n_missing:,}")
    else:
        print(f"Matched {len(out):,} block groups to TAZs")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_CSV, index=False)
    print(f"Wrote -> {OUT_CSV}")


if __name__ == "__main__":
    main()
