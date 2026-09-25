"""
08_00_county_base_load.py

Build non-EV (base) hourly load per TAZ from measured county demand, replacing
the previous approach of disaggregating WECC balancing-area baseload with
socio-economic weights.

Why this exists
---------------
`08_03._taz_socio_weights()` is meant to weight TAZs by housing and employment,
but `data/shps/taz_id.shp` carries only a TAZ id and geometry, so the code
silently falls back to using each TAZ's *EV charging demand* as a proxy for
base electricity load. That proxy is biased rural: rural TAZs have high
per-capita driving but low base-load density. Combined with nearest-substation
mapping (which makes one rural substation the nearest neighbour for a wide
area), it badly over-loaded sparse substations -- e.g. STONE CORRAL, a 60/70 kV
rural substation, was assigned 281 MW, ranking 2nd of 814 in SCE at 14x the
median weight, and alone produced 36% of all system shortfall.

Approach
--------
1. County hourly demand comes from NREL's "Hourly Electricity Demand Profiles
   for Each County in the Contiguous United States" (OEDI 8562), which is
   measured/reconciled demand rather than a proxy. Validated for 2019: 274.9
   TWh statewide and a 56.8 GW peak, against ~277 TWh actual.
2. Within a county, demand is split across TAZs by 2010 Census block
   population (SF1 P0010001), assigning each block to its nearest TAZ centroid.
   Population is an imperfect proxy for commercial/industrial load, but the
   extrapolation is now *within* a county rather than across a whole balancing
   area, so the error is far smaller and is not systematically rural-biased.

Writes:
  data/meso/taz_ids_base.npy
  data/meso/taz_hourly_base_8760.npy    [W], shape (n_taz, 8760)
  data/meso/taz_county_population.csv   diagnostics
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

import common as C

SF1_DIR = C.DATA_DIR / "ca2010.sf1"
SF1_GEO = SF1_DIR / "cageo2010.sf1"
SF1_SEG1 = SF1_DIR / "ca000012010.sf1"
NREL_COUNTY_H5 = C.DATA_DIR / "nrel" / "historic_load_hourly_2016_2023_county.h5"

# NREL OEDI 8562 publishes its hourly axis in UTC without a tz label.
PACIFIC_TZ = "America/Los_Angeles"

TAZ_IDS_BASE_NPY = C.MESO_DIR / "taz_ids_base.npy"
TAZ_HOURLY_BASE_NPY = C.MESO_DIR / "taz_hourly_base_8760.npy"
TAZ_COUNTY_POP_CSV = C.MESO_DIR / "taz_county_population.csv"

# 2010 SF1 geographic header is fixed width; 0-indexed slices.
GEO_SUMLEV = slice(8, 11)
GEO_LOGRECNO = slice(18, 25)
GEO_STATE = slice(27, 29)
GEO_COUNTY = slice(29, 32)
GEO_INTPTLAT = slice(336, 347)
GEO_INTPTLON = slice(347, 359)

SUMLEV_BLOCK = "101"


def load_block_population() -> pd.DataFrame:
    """Census block centroids with total population and county FIPS."""
    C.require_file(SF1_GEO, hint="Need the SF1 geographic header cageo2010.sf1")
    C.require_file(SF1_SEG1, hint="Need SF1 segment 1 ca000012010.sf1")

    recs = []
    with open(SF1_GEO, "r", encoding="latin-1") as fh:
        for line in fh:
            if line[GEO_SUMLEV] != SUMLEV_BLOCK:
                continue
            lat = line[GEO_INTPTLAT].strip()
            lon = line[GEO_INTPTLON].strip()
            if not lat or not lon:
                continue
            recs.append(
                (
                    int(line[GEO_LOGRECNO]),
                    line[GEO_STATE] + line[GEO_COUNTY],
                    float(lat),
                    float(lon),
                )
            )
    geo = pd.DataFrame(recs, columns=["logrecno", "county_fips", "lat", "lon"])
    print(f"  SF1 geo: {len(geo):,} block records")

    # Segment 1 is comma delimited; P0010001 (total population) is the first
    # data field after FILEID, STUSAB, CHARITER, CIFSN, LOGRECNO.
    seg = pd.read_csv(
        SF1_SEG1,
        header=None,
        usecols=[4, 5],
        names=["logrecno", "pop"],
        encoding="latin-1",
    )
    out = geo.merge(seg, on="logrecno", how="left")
    out["pop"] = out["pop"].fillna(0.0).astype(float)
    print(f"  block population total: {out['pop'].sum():,.0f}")
    return out


def assign_blocks_to_taz(blocks: pd.DataFrame) -> pd.DataFrame:
    """Aggregate block population onto the nearest TAZ centroid."""
    from scipy.spatial import cKDTree

    C.require_file(C.TAZ_CENTROID_GPKG, hint="Expected data/shps/TAZ_centroid_sf.gpkg")
    taz = gpd.read_file(C.TAZ_CENTROID_GPKG)
    taz_col = C.pick_column(taz.columns, ("TAZ", "TAZ12", "TAZ_Zone", "taz", "ZONE"))
    taz = taz.rename(columns={taz_col: "TAZ"})
    if taz.crs is None:
        taz = taz.set_crs(4326)
    taz = taz.to_crs(C.CA_ALBERS_CRS)

    bg = gpd.GeoDataFrame(
        blocks,
        geometry=gpd.points_from_xy(blocks["lon"], blocks["lat"]),
        crs="EPSG:4326",
    ).to_crs(C.CA_ALBERS_CRS)

    tree = cKDTree(np.column_stack([taz.geometry.x, taz.geometry.y]))
    _, idx = tree.query(np.column_stack([bg.geometry.x, bg.geometry.y]), k=1)
    bg["TAZ"] = taz["TAZ"].to_numpy()[idx]

    agg = (
        bg.groupby(["TAZ", "county_fips"], as_index=False)["pop"]
        .sum()
        .sort_values("pop", ascending=False)
    )
    # A TAZ can straddle a county line; attribute it to the county holding most
    # of its population.
    agg = agg.drop_duplicates("TAZ", keep="first").reset_index(drop=True)
    print(f"  TAZs with population: {(agg['pop'] > 0).sum():,} of {len(agg):,}")
    return agg


def load_county_hourly(year: int) -> pd.DataFrame:
    """Hourly county demand [MW] for California, indexed by county FIPS."""
    import h5py

    C.require_file(
        NREL_COUNTY_H5,
        hint="Download OEDI 8562 historic_load_hourly_2016_2023_county.h5 into data/nrel/",
    )
    with h5py.File(NREL_COUNTY_H5, "r") as fh:
        fips = np.array([s.decode() for s in fh["data/axis0"][:]])
        ts = pd.to_datetime(fh["data/axis1"][:])
        ca = np.flatnonzero(pd.Series(fips).str.startswith("p06").values)

        # The file's timestamp axis is UTC, carried without a tz label. Using
        # it as local time shifts every county's demand by 7-8 hours, which
        # inverts the diurnal shape: raw, California demand troughs at index
        # hour 11 and peaks at 22, whereas the real system troughs at 03-05
        # and peaks at 17-19 local. Against PG&E's measured substation
        # profiles the raw series correlates at r=0.44 and peaks at hour 2;
        # converted to Pacific time it correlates at r=0.80-0.83 and peaks at
        # 18-19, matching the measurement. Left uncorrected, EV evening
        # charging lands on the wrong part of the base-load curve.
        local = ts.tz_localize("UTC").tz_convert(PACIFIC_TZ)
        rows = np.flatnonzero(local.year == year)
        if len(rows) == 0:
            raise ValueError(f"year {year} not present in {NREL_COUNTY_H5}")
        block = fh["data/block0_values"][rows[0] : rows[-1] + 1, :][:, ca]
    cols = [f[1:] for f in fips[ca]]  # strip the 'p' prefix -> county FIPS
    # Keep a tz-naive local index so downstream hour-of-day logic is local.
    df = pd.DataFrame(block, columns=cols, index=local[rows].tz_localize(None))
    print(
        f"  {year}: {df.shape[0]} h x {df.shape[1]} counties; "
        f"statewide peak {df.sum(axis=1).max():,.0f} MW, "
        f"annual {df.sum().sum() / 1e6:,.1f} TWh"
    )
    return df


def main(year: int = 2019) -> None:
    print("Parsing 2010 SF1 block population...")
    blocks = load_block_population()

    print("Assigning blocks to TAZ centroids...")
    taz_pop = assign_blocks_to_taz(blocks)

    print(f"Loading NREL county hourly demand for {year}...")
    county = load_county_hourly(year)

    # Population share of each TAZ within its county.
    taz_pop["county_pop"] = taz_pop.groupby("county_fips")["pop"].transform("sum")
    taz_pop["share"] = np.where(
        taz_pop["county_pop"] > 0, taz_pop["pop"] / taz_pop["county_pop"], 0.0
    )

    missing = sorted(set(taz_pop["county_fips"]) - set(county.columns))
    if missing:
        print(f"  WARNING: {len(missing)} county FIPS absent from NREL data: {missing[:5]}")

    taz_ids = taz_pop["TAZ"].to_numpy()
    n_hours = C.HOURS_YEAR
    out = np.zeros((len(taz_ids), n_hours), dtype=np.float32)
    for i, (fips, share) in enumerate(zip(taz_pop["county_fips"], taz_pop["share"])):
        if share <= 0 or fips not in county.columns:
            continue
        series = C.pad_or_wrap_hours(county[fips].to_numpy(), n_hours)
        out[i] = (series * share * 1e6).astype(np.float32)  # MW -> W

    C.ensure_dir(C.MESO_DIR)
    np.save(TAZ_IDS_BASE_NPY, taz_ids)
    np.save(TAZ_HOURLY_BASE_NPY, out)
    taz_pop.to_csv(TAZ_COUNTY_POP_CSV, index=False)

    tot_peak = out.sum(axis=0).max() / 1e9
    print(
        f"Wrote {TAZ_HOURLY_BASE_NPY}  n_taz={len(taz_ids)}  "
        f"statewide base peak {tot_peak:.2f} GW"
    )
    print(f"Wrote {TAZ_COUNTY_POP_CSV}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    p = argparse.ArgumentParser(description="County-based non-EV load per TAZ")
    p.add_argument("--year", type=int, default=2019, help="calendar year to extract")
    main(year=p.parse_args().year)
