"""
08_06_substation_ratings.py

Step-down (transformer bank) rating per substation, for the transformer limit
in the nested model.

Why this matters
----------------
Without a rating the model gives every substation unlimited throughput, so
power moves from transmission into local load with no transformer between
them. That is not a small omission: PG&E's published bank loadings run at a
median 85.6% of rating, 41% of substations are above 90%, and 19% are already
above 100%. The transformer binds across most of the system while the
corridors above it still have headroom, so a model without it can only ever
find congestion on transmission lines -- and will attribute EV impacts to the
wrong asset class.

Sources, in precedence order:
  1. PG&E GRIP ``DFSubstationArea``: summed bank ratings (MVA) per substation
  2. SCE DRPEP ``GNA Substations``: published substation rating
  3. SCE DRPEP ``ICA Substations``: projected load + max remaining capacity
  4. derived: assigned peak demand / TYPICAL_LOADING, so an unmeasured
     substation is given the same headroom ratio as the measured median

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

# Median peak facility loading across PG&E's published banks. Used to turn an
# assigned peak into a plausible rating where nothing is published, so derived
# substations carry the same headroom ratio as measured ones rather than an
# arbitrary multiple.
TYPICAL_LOADING = 0.856

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--typical-loading", type=float, default=TYPICAL_LOADING)
    args = ap.parse_args()

    nodes_csv = C.MESO_DIR / "meso_nodes.csv"
    C.require_file(nodes_csv, hint="Run 09_01_build_ca_meso_grid.py first.")
    nodes = pd.read_csv(nodes_csv)
    nodes["substation_id"] = nodes["substation_id"].astype(str)

    print("Collecting published ratings...")
    published = pd.concat([pge_ratings(), sce_ratings()], ignore_index=True)
    published = published.drop_duplicates("substation_id")

    df = nodes[["substation_id", "hub_id", "total_peak_W"]].merge(
        published, on="substation_id", how="left"
    )
    df["rating_source"] = np.where(df["rating_W"].notna(), "published", "derived")

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
