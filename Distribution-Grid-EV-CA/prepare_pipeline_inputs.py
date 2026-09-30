"""
Prepare pipeline input CSVs from raw data already in ``data/``.

Generates:
- ``data/mapping/census bg to tract 2010/Census Block Group Population_2010_short.csv``
  from ``data/ca2010.sf1`` (2010 SF1 table P1, summary level 150)
- ``data/mobility_data/EV_Toolbox/households_all_by_tract.csv``
  from ``data/ca2010.sf1`` (2010 SF1 table H1 total housing units, summary level 140)
- ``data/mobility_data/EV_Toolbox/evhouseholds_by_tract_acc2_2011.csv``
  from ``data/evtoolbox_export_2025_Number of EVs.csv`` (2025 tract EV counts projected to 2020-2044)

The EV-household export currently returns all zeros, so EV counts are used as a proxy for
EV households (typically ~1 EV per adopting household at tract level).
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SF1_DIR = DATA_DIR / "ca2010.sf1"

BG_POP_OUT = (
    DATA_DIR
    / "mapping"
    / "census bg to tract 2010"
    / "Census Block Group Population_2010_short.csv"
)
HH_ALL_OUT = DATA_DIR / "mobility_data" / "EV_Toolbox" / "households_all_by_tract.csv"
HH_EV_OUT = (
    DATA_DIR / "mobility_data" / "EV_Toolbox" / "evhouseholds_by_tract_acc2_2011.csv"
)

EV_COUNTS_CSV = DATA_DIR / "evtoolbox_export_2025_Number of EVs.csv"
EV_HH_CSV = DATA_DIR / "evtoolbox_export_2025_Number of Households with an EV.csv"

GEO_FILE = SF1_DIR / "cageo2010.sf1"
POP_FILE = SF1_DIR / "ca000012010.sf1"  # table P1
HOUSING_FILE = SF1_DIR / "ca000422010.sf1"  # table H1

SUMLEV_TRACT = "140"
SUMLEV_BLOCK_GROUP = "150"
GEOID_PREFIX = "1400000US"
EV_YEARS = range(2020, 2045)


def _read_sf1_table(path: Path) -> pd.DataFrame:
    """Read an SF1 comma-delimited segment keyed by LOGRECNO."""
    rows: list[tuple[int, int]] = []
    with path.open("r", encoding="latin-1", errors="replace") as handle:
        for line in handle:
            parts = line.strip().split(",")
            if len(parts) < 6:
                continue
            rows.append((int(parts[4]), int(parts[5])))
    return pd.DataFrame(rows, columns=["LOGRECNO", "value"])


def _read_geo_index() -> pd.DataFrame:
    """Parse fixed-width SF1 geographic header for tracts and block groups."""
    records: list[dict[str, str]] = []
    with GEO_FILE.open("r", encoding="latin-1", errors="replace") as handle:
        for line in handle:
            sumlev = line[8:11]
            if sumlev not in (SUMLEV_TRACT, SUMLEV_BLOCK_GROUP):
                continue
            state = line[27:29]
            county = line[29:32]
            tract = line[54:60]
            blkgrp = line[60:61]
            geoid_body = state + county + tract
            if sumlev == SUMLEV_BLOCK_GROUP:
                geoid_body += blkgrp
            records.append(
                {
                    "LOGRECNO": int(line[18:25]),
                    "SUMLEV": sumlev,
                    "GEOID": GEOID_PREFIX + geoid_body,
                    "tract": int(state + county + tract),
                }
            )
    return pd.DataFrame(records)


def build_block_group_population() -> pd.DataFrame:
    geo = _read_geo_index()
    geo_bg = geo.loc[geo["SUMLEV"] == SUMLEV_BLOCK_GROUP, ["LOGRECNO", "GEOID"]]
    pop = _read_sf1_table(POP_FILE)
    out = geo_bg.merge(pop, on="LOGRECNO", how="inner").rename(columns={"value": "Population"})
    return out[["GEOID", "Population"]].sort_values("GEOID").reset_index(drop=True)


def build_tract_households() -> pd.DataFrame:
    geo = _read_geo_index()
    geo_tract = geo.loc[geo["SUMLEV"] == SUMLEV_TRACT, ["LOGRECNO", "tract"]]
    housing = _read_sf1_table(HOUSING_FILE)
    out = geo_tract.merge(housing, on="LOGRECNO", how="inner").rename(columns={"value": "households"})
    return out[["tract", "households"]].sort_values("tract").reset_index(drop=True)


def _read_ev_toolbox_export(path: Path) -> pd.DataFrame:
    lines = path.read_text(encoding="utf-8").splitlines()
    data_start = next(i for i, line in enumerate(lines) if line.startswith("BEGIN DATA")) + 1
    return pd.read_csv(io.StringIO("\n".join(lines[data_start:])))


def build_ev_households_by_tract_year() -> pd.DataFrame:
    ev_counts = _read_ev_toolbox_export(EV_COUNTS_CSV)
    tract_col = ev_counts.columns[0]
    value_col = ev_counts.columns[1]
    base = ev_counts[[tract_col, value_col]].rename(
        columns={tract_col: "tract", value_col: "households_2025"}
    )
    base["tract"] = pd.to_numeric(base["tract"], errors="coerce")
    base["households_2025"] = pd.to_numeric(base["households_2025"], errors="coerce").fillna(0)

    # Prefer explicit EV-household export when it contains non-zero values.
    if EV_HH_CSV.is_file():
        ev_hh = _read_ev_toolbox_export(EV_HH_CSV)
        hh_col = ev_hh.columns[1]
        hh = ev_hh[[ev_hh.columns[0], hh_col]].rename(
            columns={ev_hh.columns[0]: "tract", hh_col: "households_2025"}
        )
        hh["tract"] = pd.to_numeric(hh["tract"], errors="coerce")
        hh["households_2025"] = pd.to_numeric(hh["households_2025"], errors="coerce").fillna(0)
        if hh["households_2025"].sum() > 0:
            base = hh

    rows: list[pd.DataFrame] = []
    for year in EV_YEARS:
        scale = min(1.0, max(0.0, (year - 2020) / (2025 - 2020)))
        part = base[["tract"]].copy()
        part["year"] = year
        part["households"] = base["households_2025"] * scale
        rows.append(part)
    return pd.concat(rows, ignore_index=True)[["tract", "year", "households"]]


def main() -> None:
    if not GEO_FILE.is_file():
        raise SystemExit(f"Missing SF1 geography file: {GEO_FILE}")
    if not POP_FILE.is_file():
        raise SystemExit(f"Missing SF1 population file: {POP_FILE}")
    if not HOUSING_FILE.is_file():
        raise SystemExit(f"Missing SF1 housing file: {HOUSING_FILE}")
    if not EV_COUNTS_CSV.is_file():
        raise SystemExit(f"Missing EV toolbox export: {EV_COUNTS_CSV}")

    bg_pop = build_block_group_population()
    hh_all = build_tract_households()
    hh_ev = build_ev_households_by_tract_year()

    BG_POP_OUT.parent.mkdir(parents=True, exist_ok=True)
    HH_ALL_OUT.parent.mkdir(parents=True, exist_ok=True)

    bg_pop.to_csv(BG_POP_OUT, index=False)
    hh_all.to_csv(HH_ALL_OUT, index=False)
    hh_ev.to_csv(HH_EV_OUT, index=False)

    print(f"Wrote {len(bg_pop):,} block groups -> {BG_POP_OUT}")
    print(f"Wrote {len(hh_all):,} tracts -> {HH_ALL_OUT}")
    print(f"Wrote {len(hh_ev):,} tract-year rows -> {HH_EV_OUT}")


if __name__ == "__main__":
    main()
