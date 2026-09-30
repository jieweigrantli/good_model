"""
04_01_get_share_block_vs_TAZ.py — Python port of ``04_01_get_share_block vs TAZ.R``.

Computes, per census block, its share of (population, jobs) within its TAZ.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

MAP_BLOCK_TAZ = "data/mapping/census block to TAZ 2010/map census block to TAZ 2010.csv"
POP_BLOCK = "data/mapping/census block to TAZ 2010/census block population_2010_short.csv"
JOB_BLOCK = "data/mapping/census block to TAZ 2010/LODES/ca_wac_S000_JT00_2019.csv.gz"
OUT_CSV = "data/mapping/census block to TAZ 2010/share_block vs TAZ.csv"


def main() -> None:
    map_block_taz = pd.read_csv(MAP_BLOCK_TAZ)[["GEOID10", "TAZ_Zone"]].copy()
    map_block_taz["GEOID10"] = map_block_taz["GEOID10"].astype(str)

    pop_block = pd.read_csv(POP_BLOCK)
    pop_block["GEOID10"] = pop_block["GEOID"].astype(str).str[-14:]
    pop_block["population_block"] = pd.to_numeric(pop_block["population"], errors="coerce")
    # Fallback: strip trailing 8 chars (sci-notation garbage)
    missing = pop_block["population_block"].isna()
    pop_block.loc[missing, "population_block"] = pd.to_numeric(
        pop_block.loc[missing, "population"].astype(str).str[:-8], errors="coerce"
    )

    job_block = pd.read_csv(JOB_BLOCK)[["w_geocode", "C000"]].rename(
        columns={"w_geocode": "GEOID10", "C000": "jobs_block"}
    )
    job_block["GEOID10"] = job_block["GEOID10"].astype(str)

    share = pd.merge(map_block_taz, pop_block, on="GEOID10")
    share = pd.merge(share, job_block, on="GEOID10", how="left")
    share = share[["GEOID10", "TAZ_Zone", "population_block", "jobs_block"]]
    share["jobs_block"] = share["jobs_block"].fillna(0)

    # blocks outside any TAZ
    share = share.loc[~share["TAZ_Zone"].isna()]

    # TAZ totals + block shares
    share["population_TAZ"] = share.groupby("TAZ_Zone")["population_block"].transform("sum")
    share["jobs_TAZ"] = share.groupby("TAZ_Zone")["jobs_block"].transform("sum")
    share["population_share"] = np.where(
        share["population_TAZ"] == 0, 0.0, share["population_block"] / share["population_TAZ"]
    )
    share["jobs_share"] = np.where(
        share["jobs_TAZ"] == 0, 0.0, share["jobs_block"] / share["jobs_TAZ"]
    )

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    share.to_csv(OUT_CSV, index=False)


if __name__ == "__main__":
    main()
