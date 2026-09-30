"""
08_06_substation_ratings.py

Step-down (transformer bank) rating per substation, for the transformer limit
in the nested model.

Why this matters
----------------
Without a rating the model gives every substation unlimited throughput, so
power moves from transmission into local load with no transformer between
them. The transformer binds across much of the system while the corridors above
it still have headroom, so a model without it can only ever find congestion on
transmission lines -- and will attribute EV impacts to the wrong asset class.
Measured directly: relaxing corridors alone (S3) leaves 79.6 GWh of shortfall
over four weeks, while relaxing corridors and transformers together (S4) leaves
6.4 GWh, so 38% of undelivered energy is transformer-bound.

Sources, in precedence order:
  1. PG&E ICA: published load headroom + published measured baseload (08_12)
  2. PG&E GRIP ``DFSubstationArea``: summed bank ratings (MVA) per substation
  3. SCE DRPEP ``GNA Substations``: published substation rating
  4. SCE DRPEP ``ICA Substations``: projected load + max remaining capacity
  5. derived: assigned peak demand / TYPICAL_LOADING, so an unmeasured
     substation is given the same headroom ratio as the measured median

Sources 1 and 4 are the same identity, ``capacity = headroom + baseload``, in
the two forms the utilities publish. Source 2 is kept only as a fallback: the
bank sums contradict PG&E's own measurements badly enough at some substations
(02201 sums to 9.88 MVA against a published 118.9 MW peak) that they cannot be
treated as a substation rating.

A caution on TYPICAL_LOADING below, which source 5 depends on: 0.856 is the
median across substations of the *maximum* bank loading at each substation, not
a substation-level loading. The substation-level statistic,
sum(bank load)/sum(bank rating), is 0.79. The docstring here previously quoted
the max-of-banks figures ("median 85.6%, 41% above 90%, 19% above 100%") as
though they described substations; the per-bank values are 79.7%, 32.1% and
13.2%.

Writes:
  data/meso/substation_ratings.csv
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio

import common as C

OUT_CSV = C.MESO_DIR / "substation_ratings.csv"
SCE_DIR = C.DATA_DIR / "ica" / "sce"

# Substation-level loading: sum(bank load) / sum(bank rating), median across
# substations. Used to turn an assigned peak into a plausible rating where nothing
# is published, so derived substations carry the same headroom ratio as measured
# ones rather than an arbitrary multiple.
#
# Provenance, because this constant has been wrong twice and no external source
# exists for it. Recomputed from GRIP DFSubstationArea___PeakFacilityLoadingPercent
# (1,236 banks, 694 PG&E substations), three statistics can be formed:
#
#   median over substations of the MAX bank loading      0.7988
#   fleet-wide sum(bank load) / sum(bank rating)         0.5298
#   median over substations of sum(load)/sum(rating)     0.6362   <- this one
#
# The value previously used, 0.856, reproduces none of them, and the 0.79 quoted
# in the model specification is the first statistic -- the max-of-banks median
# that the same document criticises -- mislabelled as substation-level. A rating
# describes a substation, so the substation-level statistic is the right one.
#
# It is also the only one corroborated independently: SDG&E's ICA gives a measured
# loading of 0.6450 on the capacity = headroom + baseload basis (08_16), agreeing
# with PG&E's 0.6362 to within 1.4% across two utilities and two unrelated
# datasets. 0.64 sits between them.
#
# Caveat kept with the number: peakfacili reaches 336% in the GRIP data, so some
# banks are recorded loaded above nameplate, which biases all three statistics
# upward by an unknown amount. See docs/model_specification.md.
TYPICAL_LOADING = 0.64

# Floor so a node with near-zero assigned load still has a usable interface.
MIN_RATING_W = 5e6


def pge_ratings() -> pd.DataFrame:
    """Summed bank ratings per PG&E substation, keyed by GRIP substation id."""
    path = C.grip_layer("DFSubstationArea___PeakFacilityLoadingPercent")
    if not path.is_file():
        print("  PG&E DFSubstationArea missing; skipping")
        return pd.DataFrame(columns=["substation_id", "rating_W"])
    df = pyogrio.read_dataframe(str(path), read_geometry=False)
    df["facilityra"] = pd.to_numeric(df["facilityra"], errors="coerce")
    # `substation` is the GRIP substation id; `substati_1` is its name.
    out = (
        df.dropna(subset=["facilityra"])
        .groupby("substation", as_index=False)["facilityra"]
        .sum()
        .rename(columns={"substation": "substation_id", "facilityra": "mva"})
    )
    out["substation_id"] = out["substation_id"].astype(str)
    out["rating_W"] = out["mva"] * 1e6
    print(f"  PG&E: {len(out):,} substations, {out['mva'].sum() / 1e3:.1f} GVA")
    return out[["substation_id", "rating_W"]]


def _sce_name_to_node() -> dict[str, str]:
    """SCE substation name -> model node id, by location."""
    pts_path = SCE_DIR / "ica_substations_geom.gpkg"
    if not (pts_path.is_file() and C.HIFLD_SUBSTATIONS_GPKG.is_file()):
        return {}
    pts = gpd.read_file(pts_path).to_crs(C.CA_ALBERS_CRS)
    hif = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG).to_crs(C.CA_ALBERS_CRS)
    hif["substation_id"] = "HIFLD_" + hif["OBJECTID"].astype(str)
    name_col = C.pick_column(pts.columns, ("SUB_NAME", "sub_name", "NAME"))
    j = gpd.sjoin_nearest(
        pts[[name_col, "geometry"]], hif[["substation_id", "geometry"]],
        how="left", distance_col="d",
    )
    j = j[j["d"] <= 2000.0]
    return {
        str(r[name_col]).strip().upper(): r["substation_id"] for _, r in j.iterrows()
    }


def sce_ratings() -> pd.DataFrame:
    """Substation ratings from SCE GNA, falling back to ICA load + headroom."""
    lut = _sce_name_to_node()
    if not lut:
        print("  SCE substation geometry missing; skipping")
        return pd.DataFrame(columns=["substation_id", "rating_W"])

    rows: dict[str, float] = {}

    gna = SCE_DIR / "gna_substations.parquet"
    if gna.is_file():
        g = pd.read_parquet(gna)
        g["rating"] = pd.to_numeric(g.get("rating"), errors="coerce")
        for _, r in g.dropna(subset=["rating"]).iterrows():
            nid = lut.get(str(r.get("substation_name", "")).strip().upper())
            if nid and r["rating"] > 0:
                rows[nid] = max(rows.get(nid, 0.0), float(r["rating"]) * 1e6)
        print(f"  SCE GNA: {len(rows):,} substations rated")

    ica = SCE_DIR / "substations.parquet"
    if ica.is_file():
        s = pd.read_parquet(ica)
        pl = pd.to_numeric(s.get("PROJECTED_LOAD"), errors="coerce")
        mr = pd.to_numeric(s.get("MAX_REMAIN_CAP"), errors="coerce")
        cap = (pl.fillna(0) + mr.fillna(0)) * 1e6
        added = 0
        for nid_name, c in zip(s.get("SUB_NAME", []), cap):
            nid = lut.get(str(nid_name).strip().upper())
            if nid and c > 0 and nid not in rows:
                rows[nid] = float(c)
                added += 1
        print(f"  SCE ICA: {added:,} further substations from load + headroom")

    return pd.DataFrame({"substation_id": list(rows), "rating_W": list(rows.values())})


def pge_ica_ratings() -> pd.DataFrame:
    """PG&E capacity as ``ICA headroom + measured baseload``, from 08_12.

    Takes precedence over ``pge_ratings()`` because the GNA bank sums are not a
    usable substation rating. Substation 02201, SF X (MISSION), sums to
    9.88 MVA across the two banks GRIP lists, while PG&E's own
    ``SubstationLoadProfile`` reports a 118.9 MW peak at the same id and name
    and the ICA files give ``IC_Safety_Bank_kW`` of 18,240-46,090 kW on its
    feeders. ``EDSubstations`` confirms it really has 2 banks, so the inventory
    is complete and ``facilityra`` simply does not mean what summing it assumes.
    Under the bank sum that node alone shed 53 GWh over four weeks -- 41% of all
    S0 shortfall -- at a substation the real grid serves without difficulty.

    The identity is the one Li & Jenn (2024) use throughout
    (`R_Yanning/07_05_grid_EV.R`, line 23): headroom is by definition the
    additional load a facility can take, so capacity is headroom plus the load
    it already carries. It needs both halves, so 08_12 emits only substations
    that have both and the rest fall through to the chain below.
    """
    path = C.MESO_DIR / "substation_ica_capacity.csv"
    if not path.is_file():
        print("  PG&E ICA capacity missing (run 08_12); falling back to GNA bank sums")
        return pd.DataFrame(columns=["substation_id", "rating_W"])
    d = pd.read_csv(path)
    d["substation_id"] = d["substation_id"].astype(str).str.zfill(5)
    d = d[d["rating_W"] > 0]
    print(f"  PG&E ICA: {len(d):,} substations from headroom + measured baseload")
    return d[["substation_id", "rating_W"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--typical-loading", type=float, default=TYPICAL_LOADING)
    ap.add_argument("--no-ica", action="store_true",
                    help="Ignore the PG&E ICA capacities and use GNA bank sums only.")
    args = ap.parse_args()

    nodes_csv = C.MESO_DIR / "meso_nodes.csv"
    C.require_file(nodes_csv, hint="Run 09_01_build_ca_meso_grid.py first.")
    nodes = pd.read_csv(nodes_csv)
    nodes["substation_id"] = nodes["substation_id"].astype(str)

    print("Collecting published ratings...")
    # Order matters: drop_duplicates keeps the first, so the ICA identity wins
    # over the GNA bank sums wherever both exist.
    sources = [] if args.no_ica else [pge_ica_ratings()]
    sources += [pge_ratings(), sce_ratings()]
    published = pd.concat(sources, ignore_index=True)
    published = published.drop_duplicates("substation_id")

    df = nodes[["substation_id", "hub_id", "total_peak_W"]].merge(
        published, on="substation_id", how="left"
    )
    ica_ids = set() if args.no_ica else set(pge_ica_ratings()["substation_id"])
    df["rating_source"] = np.where(
        df["substation_id"].isin(ica_ids), "ica",
        np.where(df["rating_W"].notna(), "published", "derived"),
    )

    derived = df["rating_W"].isna()
    df.loc[derived, "rating_W"] = np.maximum(
        df.loc[derived, "total_peak_W"] / max(args.typical_loading, 1e-6),
        MIN_RATING_W,
    )
    # A published rating below the assigned peak would make the node
    # infeasible on its own demand. Keep the published value but record it, so
    # the load allocation gets fixed rather than the rating quietly inflated.
    tight = df["rating_W"] < df["total_peak_W"]
    df["below_assigned_peak"] = tight

    C.ensure_dir(OUT_CSV.parent)
    df.to_csv(OUT_CSV, index=False)

    print(f"\n  wrote {OUT_CSV}")
    print(df.groupby("rating_source").agg(
        nodes=("substation_id", "size"),
        rating_GVA=("rating_W", lambda s: s.sum() / 1e9),
        peak_GW=("total_peak_W", lambda s: s.sum() / 1e9),
    ).round(2).to_string())
    ok = df["rating_W"] > 0
    print(f"\n  implied loading (peak / rating): median "
          f"{(df.loc[ok, 'total_peak_W'] / df.loc[ok, 'rating_W']).median():.2f}")
    print(f"  nodes whose published rating is below assigned peak: {int(tight.sum()):,}")


if __name__ == "__main__":
    main()
