"""
08_12_ica_substation_capacity.py

Build a per-substation capacity from PG&E's ICA detailed results, using the
identity Li & Jenn (2024, PNAS) use rather than a loading-factor assumption.

The identity
------------
`R_Yanning/07_05_grid_EV.R` constructs capacity as

    capacity = headroom + baseload

where `headroom` is the published ICA load result and `baseload` the published
measured load, both per month-hour. Nothing is assumed: headroom is by
definition the additional load a facility can take, so adding the load it
already carries gives what it can carry in total. A facility can therefore
never be rated below the load it is measured to serve, which is the failure
mode this script exists to remove.

What it replaces
----------------
`08_06` derives the transformer limit by summing `facilityra` over the banks
listed in GRIP's `DFSubstationArea___PeakFacilityLoadingPercent`, and where
nothing is published, by dividing the assigned peak by `TYPICAL_LOADING`
(0.856). Both are unsound here:

* The bank sums contradict PG&E's own measurements. Substation 02201, SF X
  (MISSION), sums to 9.88 MVA across its two banks while PG&E's
  `SubstationLoadProfile` reports a 118.9 MW peak at the same id and name, and
  the ICA files give `IC_Safety_Bank_kW` of 18,240-46,090 kW on its feeders.
  `EDSubstations` confirms the substation really has only 2 banks, so this is
  not an incomplete inventory -- `facilityra` simply is not a usable substation
  rating. That one node produced 41% of all S0 shortfall: the model capped its
  net import at 9.88 MW and shed 53 GWh over four weeks.
* 0.856 is the median across substations of the *maximum* bank loading at each
  substation. It is used as though it described a substation as a whole, but
  the substation-level statistic, sum(bank load)/sum(bank rating), is 0.79.

Aggregating line sections to a substation
-----------------------------------------
ICA is published per line section; the model needs a substation. Headroom at a
section includes the impedance of everything upstream, so the far end of a
feeder is the tightest point and the section nearest the substation the
loosest -- on feeder 022010401 at Sept 17:00 the 65 sections span 312-1,200 kW.
Because the transformer sits at the substation bus, this script takes the
MAXIMUM across a feeder's sections (`--section-agg`), then sums feeders to the
substation.

Taking the minimum instead conflates a distribution limit with a transformer
one, and the difference is not subtle: it put the median substation at 2.8%
headroom, contradicting PG&E's own GNA bank loadings (79.7% of rating, so ~20%
headroom). The maximum reconciles the two independent PG&E datasets. What is
lost is real -- the binding line section is a genuine constraint -- but this
model has no distribution layer to carry it, and pushing a feeder-end voltage
limit onto a substation transformer would attribute it to the wrong asset.

Feeder ids begin with the five-digit substation id, so the roll-up needs no
join. Redacted values appear as the literal string "REDACTED", and column names
differ between feeders (`Hourly_Load` vs `Hourly_Load_ICA`), both handled here.

Writes data/meso/substation_ica_capacity.csv
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

ICA_DIR = C.DATA_DIR / "ica" / "pge"
OUT_CSV = C.MESO_DIR / "substation_ica_capacity.csv"

LOAD_COLS = ("Hourly_Load", "Hourly_Load_ICA")

# How line sections within one feeder are reduced. See _read_feeder.
_SECTION_AGG = "max"


def _read_feeder(csv_path: Path) -> pd.DataFrame | None:
    """One feeder's load-side ICA, as (substation_id, feeder, month, hour, headroom_kW)."""
    df = pd.read_csv(csv_path, low_memory=False)
    col = next((c for c in LOAD_COLS if c in df.columns), None)
    if col is None:
        return None
    # The load result is carried on the load rows; generation rows leave it null.
    if "LoadOrGen" in df.columns:
        df = df[df["LoadOrGen"].astype(str).str.upper().str.startswith("L")]
    if df.empty:
        return None
    df["headroom_kW"] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["headroom_kW"])
    if df.empty:
        return None
    # Two archives per feeder: LICA_<id> carries the load-side results and
    # GICA_<id> the generation side. Only the load side has a usable headroom,
    # and stripping just one prefix silently turns the substation id into the
    # literal string "LICA_".
    feeder = csv_path.stem
    for prefix in ("LICA_", "GICA_"):
        if feeder.startswith(prefix):
            feeder = feeder[len(prefix):]
            break
    cols = ["Month", "Hour", "headroom_kW"] + (["GLOBALID"] if "GLOBALID" in df.columns else [])
    out = df[cols].copy()
    out["feeder"] = feeder
    out["substation_id"] = feeder[:5]
    # Two reductions, in this order, because they pull opposite ways:
    #   1. loading scenarios (90th/10th percentile) -> MIN, so capacity is never
    #      credited to the optimistic case;
    #   2. line sections -> MAX, for the reason below.
    # Doing them in one groupby would let a max span scenarios too.
    per_section = (
        out.groupby(["substation_id", "feeder", "GLOBALID", "Month", "Hour"], as_index=False)
        ["headroom_kW"].min()
        if "GLOBALID" in out.columns else out
    )
    out = per_section
    # Reduce the feeder's line sections to one number per month-hour. MAX, not
    # min: ICA is published per line section and the headroom at a section
    # includes the impedance of everything upstream of it, so the far end of a
    # feeder is the tightest point and the section nearest the substation the
    # loosest. The transformer sits at the substation bus, so the bank-relevant
    # headroom is the loosest one. Measured on feeder 022010401 at Sept 17:00,
    # sections span 312-1,200 kW -- a factor of four. Taking the minimum made
    # the median substation show 2.8% headroom, which contradicted PG&E's own
    # GNA bank loadings (79.7% of rating, so ~20% headroom); taking the maximum
    # reconciles the two independent datasets.
    #
    # Note this reduces sections, then main() sums feeders. Within a feeder the
    # binding section is a distribution constraint, not a transformer one, and
    # this model has no distribution layer to carry it.
    agg = _SECTION_AGG  # "max", "median" or "min"; see above
    return (
        out.groupby(["substation_id", "feeder", "Month", "Hour"], as_index=False)["headroom_kW"]
        .agg(agg)
    )


def read_division(zip_path: Path, limit: int | None = None) -> pd.DataFrame:
    z = zipfile.ZipFile(zip_path)
    # Each feeder ships twice: LICA_<id> for the load side and GICA_<id> for
    # generation. Only the load side carries a headroom, so reading the
    # generation half is pure cost -- it is exactly half of every division.
    members = [n for n in z.namelist() if n.endswith(".7z") and Path(n).name.startswith("LICA_")]
    if not members:
        members = [n for n in z.namelist() if n.endswith(".7z")]
    if limit:
        members = members[:limit]
    import py7zr

    frames = []
    for i, name in enumerate(members, 1):
        try:
            with tempfile.TemporaryDirectory() as td:
                with py7zr.SevenZipFile(io.BytesIO(z.read(name))) as a:
                    a.extractall(path=td)
                for csv in Path(td).rglob("*.csv"):
                    rec = _read_feeder(csv)
                    if rec is not None:
                        frames.append(rec)
        except Exception as exc:  # a single corrupt feeder should not stop a division
            print(f"    {name}: {type(exc).__name__} {exc}")
        if i % 100 == 0:
            print(f"    {i}/{len(members)} feeders", flush=True)
    if not frames:
        return pd.DataFrame(columns=["substation_id", "feeder", "Month", "Hour", "headroom_kW"])
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--divisions", nargs="*", default=None,
                    help="Division names (zip basenames). Default: every zip present.")
    ap.add_argument("--limit-feeders", type=int, default=None,
                    help="Read only the first N feeders per division, for a quick check.")
    ap.add_argument("--section-agg", choices=["max", "median", "min"], default="max",
                    help="Reduce line sections within a feeder by max (bank-relevant, "
                         "default) or min (tightest distribution section).")
    ap.add_argument("--agg", choices=["min-then-sum", "min", "sum"], default="min-then-sum",
                    help="Line sections -> feeder -> substation. See the module docstring.")
    args = ap.parse_args()
    global _SECTION_AGG
    _SECTION_AGG = args.section_agg

    zips = sorted(ICA_DIR.glob("*.zip"))
    if args.divisions:
        want = {d.lower() for d in args.divisions}
        zips = [z for z in zips if z.stem.lower() in want]
    zips = [z for z in zips if "DataDictionary" not in z.stem]
    if not zips:
        raise SystemExit(f"No division zips in {ICA_DIR}. Run 08_11_fetch_pge_ica.py first.")

    print(f"Reading {len(zips)} division(s) from {ICA_DIR}")
    parts = []
    for z in zips:
        print(f"  {z.stem} ({z.stat().st_size / 1e6:,.0f} MB)")
        parts.append(read_division(z, args.limit_feeders))
    ica = pd.concat(parts, ignore_index=True)
    if ica.empty:
        raise SystemExit("No load-side ICA rows found.")
    print(f"\n  {len(ica):,} feeder month-hour rows, "
          f"{ica.substation_id.nunique():,} substations, {ica.feeder.nunique():,} feeders")

    # line sections were already reduced to the feeder minimum inside _read_feeder
    if args.agg == "min-then-sum":
        sub = ica.groupby(["substation_id", "Month", "Hour"], as_index=False)["headroom_kW"].sum()
    elif args.agg == "min":
        sub = ica.groupby(["substation_id", "Month", "Hour"], as_index=False)["headroom_kW"].min()
    else:
        sub = ica.groupby(["substation_id", "Month", "Hour"], as_index=False)["headroom_kW"].sum()

    # measured baseload, the other half of the identity
    base = C.measured_substation_baseload_high()
    merged = sub.merge(base, on=["substation_id", "Month", "Hour"], how="inner")
    merged["capacity_kW"] = merged["headroom_kW"] + merged["baseload_kW"]

    # The identity needs BOTH halves. Substations present in ICA but absent from
    # SubstationLoadProfile (02213 SF K and 02226 SF L among them) would
    # otherwise emit headroom alone as if it were a capacity, which is worse
    # than the rating being replaced: SF K would drop from 31.7 MW to 2.9 MW
    # against a measured 86.9 MW peak. An inner join drops them so 08_06's
    # existing precedence chain still applies to those.
    have_ica = set(sub.substation_id)
    have_both = set(merged.substation_id)
    dropped = sorted(have_ica - have_both)
    if dropped:
        print(f"  {len(dropped)} substations have ICA but no measured baseload; "
              f"left to the existing rating chain: {dropped[:8]}"
              f"{' ...' if len(dropped) > 8 else ''}")

    per = merged.groupby("substation_id", as_index=False).agg(
        ica_capacity_kW=("capacity_kW", "max"),
        ica_headroom_kW=("headroom_kW", "min"),
        measured_peak_kW=("baseload_kW", "max"),
        month_hours=("capacity_kW", "size"),
    )
    per["rating_W"] = per["ica_capacity_kW"] * 1e3
    C.ensure_dir(OUT_CSV.parent)
    per.to_csv(OUT_CSV, index=False)

    print(f"\n  {len(per):,} substations with an ICA-derived capacity")
    print(per["ica_capacity_kW"].describe().to_string())
    print(f"\n  wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
