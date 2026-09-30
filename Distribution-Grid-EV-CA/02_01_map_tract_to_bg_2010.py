"""
02_01_map_tract_to_bg_2010.py — Python port of ``02_01_map_tract_to_bg_2010.R``.

Builds a population-share table mapping 2010 census block groups → tracts.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

TRACT_POP = "data/mapping/census bg to tract 2010/Census Tract Population_2010_short.csv"
BG_POP = "data/mapping/census bg to tract 2010/Census Block Group Population_2010_short.csv"
OUT_CSV = "data/mapping/census bg to tract 2010/popluation share bg to tract 2010.csv"


def _geoid_short(s: pd.Series, strip_last: int = 0) -> pd.Series:
    """Emulate ``substr(GEOID, 11, nchar(GEOID) - strip_last)``.

    R is 1-indexed; ``substr(x, 11, end)`` keeps characters from position 11 to end.
    In Python that's ``x[10:end]``.
    """
    tail = s.astype(str).str.slice(10)
    if strip_last > 0:
        tail = tail.str.slice(0, -strip_last)
    return tail


def main() -> None:
    tract_pop = pd.read_csv(TRACT_POP)
    bg_pop = pd.read_csv(BG_POP)

    tract_pop["GEOIDshort"] = pd.to_numeric(_geoid_short(tract_pop["GEOID"]), errors="coerce")
    bg_pop["GEOIDshort"] = pd.to_numeric(_geoid_short(bg_pop["GEOID"]), errors="coerce")

    # tract id embedded in bg id: drop last char
    bg_pop["GEOIDtract"] = pd.to_numeric(_geoid_short(bg_pop["GEOID"], strip_last=1), errors="coerce")

    bg_pop = bg_pop.rename(columns={"Population": "bg_pop", "GEOIDshort": "GEOIDbg"})
    tract_pop = tract_pop.rename(columns={"Population": "tract_pop", "GEOIDshort": "GEOIDtract"})

    pop = pd.merge(
        bg_pop[["GEOIDbg", "GEOIDtract", "bg_pop"]],
        tract_pop[["GEOIDtract", "tract_pop"]],
        on="GEOIDtract",
        how="left",
    )

    # Population-share bg vs tract
    pop["share"] = pd.to_numeric(pop["bg_pop"], errors="coerce") / pd.to_numeric(pop["tract_pop"], errors="coerce")
    print(pop["share"].describe())
    pop.loc[pop["tract_pop"].astype(str) == "0", "share"] = 0

    # Fallback: some populations are scientific-notation strings — drop trailing 8 chars
    bad = pop["share"].isna()
    pop.loc[bad, "tract_pop"] = pop.loc[bad, "tract_pop"].astype(str).str.slice(
        0, pop.loc[bad, "tract_pop"].astype(str).str.len() - 8
    )
    pop.loc[bad, "share"] = pd.to_numeric(pop.loc[bad, "bg_pop"], errors="coerce") / pd.to_numeric(
        pop.loc[bad, "tract_pop"], errors="coerce"
    )
    print(pop["share"].describe())

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    pop.to_csv(OUT_CSV, index=False)


if __name__ == "__main__":
    main()
