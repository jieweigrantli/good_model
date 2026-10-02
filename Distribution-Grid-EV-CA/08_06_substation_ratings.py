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
  5. SDG&E ICA: section headroom summed to the substation + projected load
  6. derived: assigned peak demand / TYPICAL_LOADING, so an unmeasured
     substation is given the same headroom ratio as the measured median

Sources 1, 4 and 5 are the same identity, ``capacity = headroom + baseload``, in
the three forms the investor-owned utilities publish it, so all three IOUs now rest
on their own published capacity data. Source 2 is kept only as a fallback: the bank
sums contradict PG&E's own measurements badly enough at some substations (02201 sums
to 9.88 MVA against a published 118.9 MW peak) that they cannot be treated as a
substation rating, and where a neighbour proves the inventory incomplete they are
demoted outright.

Source 6 depends on ``TYPICAL_LOADING``; see the comment on that constant for its
derivation across all three utilities and the 0.502 to 0.639 band it sits in.

Writes:
  data/meso/substation_ratings.csv
"""

from __future__ import annotations

import argparse
import re

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
# The same per-substation statistic can now be measured on all three investor-owned
# utilities from their own published data:
#
#   PG&E   GRIP bank ratings and loadings          694 substations   0.6362
#   SCE    ICA projected load + remaining capacity 700 substations   0.5017
#   SDG&E  ICA section headroom + projected load    96 substations   0.6390
#
# PG&E and SDG&E agree to 0.4% from unrelated datasets, which is what supports 0.64.
# SCE dissents at 0.5017, 21% lower, and since this constant is applied mostly to
# HIFLD-sourced nodes and SCE holds the largest population of those, 0.64 probably
# over-rates SCE substations. A per-utility constant is the obvious refinement. The
# honest uncertainty band on this parameter is 0.502 to 0.639, not a single number.
#
# Caveats kept with the number: peakfacili reaches 336% in the GRIP data, so some
# banks are recorded loaded above nameplate and the PG&E statistics are biased
# upward by an unknown amount; SDG&E's PROJ_LOAD and SCE's PROJECTED_LOAD are
# projected rather than as-built; and SDG&E rests on 96 substations against ~700
# for the other two. See docs/model_specification.md section 0.8.
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


def _strip_voltage_suffix(name) -> str:
    """Drop the voltage transformation GNA appends to a substation name.

    SCE's two datasets name the same substations differently, and until this was
    noticed the GNA rating path resolved **nothing at all** -- 0 of 843 names
    matched, so every SCE rating came from the ICA path instead:

        GNA Substations (layer 1)   "Acton 66/12", "Alessandro 115/33"
        ICA substations             "Windsor Hills", "Woodruff"

    GNA suffixes the transformation, so one substation appears once per voltage
    pair ("Alessandro 115/33" and "Alessandro 115/12" are the same yard).
    Stripping the suffix lifts the overlap from 0 to 702 of 734 ICA names, and
    collapsing the duplicates by taking the largest rating per yard is what
    ``sce_ratings`` already does.

    The pattern has no "KV" in it, which is why ``08_08.norm`` -- written for
    HIFLD and OSM names, where the voltage is spelled out -- does not catch it.
    """
    s = str(name or "").strip()
    s = re.sub(r"\s+\d{1,3}(?:\.\d+)?(?:\s*/\s*\d{1,3}(?:\.\d+)?)+\s*(?:KV)?$", "",
               s, flags=re.I)
    return s.strip().upper()


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

    # A published value below MIN_RATING_W is treated as MISSING rather than as a
    # rating, so it falls through to the derived rule like any unrated node.
    #
    # Both SCE sources carry near-zero entries that are absent data rather than tiny
    # substations: 46 of 231 GNA ratings and 63 of 735 ICA capacities sit below 1 MW,
    # against medians of 72.5 and 31.1 MW. Passed through, those produce substations
    # rated at a fraction of the load they already serve -- HIFLD_2417 at 0.37 MW
    # against 34.6 MW of assigned peak, HIFLD_555 at 1.36 MW against 87.9 MW -- which
    # is not tightness the model should discover but an input that cannot be true.
    # Two of them, HIFLD_555 and HIFLD_562, were among the largest SCE deficits and
    # were initially mistaken for stranded topology.
    #
    # The sub-floor value is discarded, never clamped up to the floor. Clamping would
    # put a spike of substations at exactly MIN_RATING_W and would be fitting an
    # output; discarding says only that a number this small is not a measurement of a
    # substation, which is the same validity test applied to PG&E bank sums below
    # their own served load.
    rows: dict[str, float] = {}
    rejected = {"gna": 0, "ica": 0}

    gna = SCE_DIR / "gna_substations.parquet"
    if gna.is_file():
        g = pd.read_parquet(gna)
        g["rating"] = pd.to_numeric(g.get("rating"), errors="coerce")
        for _, r in g.dropna(subset=["rating"]).iterrows():
            nid = lut.get(_strip_voltage_suffix(r.get("substation_name")))
            if not nid or not r["rating"] > 0:
                continue
            w = float(r["rating"]) * 1e6
            if w < MIN_RATING_W:
                rejected["gna"] += 1
                continue
            rows[nid] = max(rows.get(nid, 0.0), w)
        print(f"  SCE GNA: {len(rows):,} substations rated"
              + (f" ({rejected['gna']:,} rejected below {MIN_RATING_W / 1e6:.0f} MW)"
                 if rejected["gna"] else ""))

    ica = SCE_DIR / "substations.parquet"
    if ica.is_file():
        s = pd.read_parquet(ica)
        pl = pd.to_numeric(s.get("PROJECTED_LOAD"), errors="coerce")
        mr = pd.to_numeric(s.get("MAX_REMAIN_CAP"), errors="coerce")
        cap = (pl.fillna(0) + mr.fillna(0)) * 1e6
        added = 0
        for nid_name, c in zip(s.get("SUB_NAME", []), cap):
            nid = lut.get(_strip_voltage_suffix(nid_name))
            if not nid or not c > 0 or nid in rows:
                continue
            if c < MIN_RATING_W:
                rejected["ica"] += 1
                continue
            rows[nid] = float(c)
            added += 1
        print(f"  SCE ICA: {added:,} further substations from load + headroom"
              + (f" ({rejected['ica']:,} rejected below {MIN_RATING_W / 1e6:.0f} MW)"
                 if rejected["ica"] else ""))

    return pd.DataFrame({"substation_id": list(rows), "rating_W": list(rows.values())})


SDGE_DIR = C.DATA_DIR / "ica" / "sdge"


def sdge_ratings(section_agg: str = "max") -> pd.DataFrame:
    """SDG&E substation capacity as ``ICA section headroom + projected load``.

    The same identity 08_12 applies to PG&E, on the dataset 08_16 downloads. SDG&E
    publishes its whole ICA as open ArcGIS feature services, so this needs no data
    request:

    * ``Load Capacity Grids`` -- 501,409 line sections, each with
      ``ICAWOF_UNILOAD`` (load integration capacity, MW), ``CIRCUIT_NAME`` and
      ``SUBID``. Sections aggregate to a feeder and feeders sum to a substation,
      exactly as in PG&E's ICA.
    * ``Substations`` -- 107 substations with ``PROJ_LOAD`` and polygon geometry.

    ``SUBID`` is the substation *name*, and SDG&E's 167 nodes in this model are all
    HIFLD-sourced with no utility identity, so the join runs through geometry the way
    ``_sce_name_to_node`` does for SCE: SDG&E's own substation centroid to the nearest
    HIFLD node within 2 km. The service returns Web Mercator, so the stored lon/lat
    are EPSG:3857 and are reprojected here.

    An unfitted cross-check worth keeping: ``PROJ_LOAD`` sums to 4,997 MW against the
    ~5,300 MW this model already allocates to SDG&E, 6% agreement from two unrelated
    derivations.

    ``section_agg`` mirrors 08_12's ``--section-agg``. Headroom at a section includes
    upstream impedance, so the section nearest the substation is the bank-relevant
    one and ``max`` is the default; the choice is flagged as high-risk in the
    specification because it moves the result materially.
    """
    lca_path = SDGE_DIR / "load_capacity.parquet"
    sub_path = SDGE_DIR / "substations.parquet"

    if not (lca_path.is_file() and sub_path.is_file()):
        print("  SDG&E ICA missing (run 08_16); skipping")
        return pd.DataFrame(columns=["substation_id", "rating_W"])

    if not C.HIFLD_SUBSTATIONS_GPKG.is_file():
        print("  SDG&E: HIFLD substation layer missing; skipping")
        return pd.DataFrame(columns=["substation_id", "rating_W"])

    lca = pd.read_parquet(lca_path, columns=["SUBID", "CIRCUIT_NAME", "ICAWOF_UNILOAD"])
    lca["ICAWOF_UNILOAD"] = pd.to_numeric(lca["ICAWOF_UNILOAD"], errors="coerce")
    per_feeder = (
        lca.dropna(subset=["ICAWOF_UNILOAD"])
        .groupby(["SUBID", "CIRCUIT_NAME"], as_index=False)["ICAWOF_UNILOAD"]
        .agg(section_agg)
    )
    headroom = (
        per_feeder.groupby("SUBID", as_index=False)["ICAWOF_UNILOAD"]
        .sum()
        .rename(columns={"ICAWOF_UNILOAD": "headroom_MW"})
    )
    headroom["key"] = headroom["SUBID"].astype(str).str.strip().str.upper()

    subs = pd.read_parquet(sub_path)
    subs["key"] = subs["NAME"].astype(str).str.strip().str.upper()
    subs["PROJ_LOAD"] = pd.to_numeric(subs["PROJ_LOAD"], errors="coerce")

    df = subs.merge(headroom[["key", "headroom_MW"]], on="key", how="inner")
    df = df[df["PROJ_LOAD"].notna() & (df["PROJ_LOAD"] > 0)]

    if df.empty:
        print("  SDG&E: no substation matched both headroom and projected load")
        return pd.DataFrame(columns=["substation_id", "rating_W"])

    # SDG&E centroids (Web Mercator) -> nearest HIFLD node, the way SCE is joined.
    pts = gpd.GeoDataFrame(
        df[["key", "headroom_MW", "PROJ_LOAD"]],
        geometry=gpd.points_from_xy(df["lon"], df["lat"]),
        crs="EPSG:3857",
    ).to_crs(C.CA_ALBERS_CRS)

    hif = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG).to_crs(C.CA_ALBERS_CRS)
    hif["substation_id"] = "HIFLD_" + hif["OBJECTID"].astype(str)
    j = gpd.sjoin_nearest(pts, hif[["substation_id", "geometry"]], how="left",
                          distance_col="d")
    far = int((j["d"] > 2000.0).sum())
    j = j[j["d"] <= 2000.0]

    j["rating_W"] = (j["headroom_MW"] + j["PROJ_LOAD"]) * 1e6
    below = int((j["rating_W"] < MIN_RATING_W).sum())
    j = j[j["rating_W"] >= MIN_RATING_W]

    out = (
        j.groupby("substation_id", as_index=False)["rating_W"].max()
        if len(j) else pd.DataFrame(columns=["substation_id", "rating_W"])
    )
    print(f"  SDG&E ICA: {len(out):,} substations from section headroom + projected load "
          f"(agg={section_agg}"
          + (f", {far:,} beyond the 2 km join" if far else "")
          + (f", {below:,} below {MIN_RATING_W / 1e6:.0f} MW" if below else "") + ")")
    return out[["substation_id", "rating_W"]]


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


# A nearby substation whose ICA rating exceeds its own bank sum by at least this
# much is taken as proof that the bank inventory in that cluster is incomplete.
BANK_SUM_PROOF_FACTOR = 2.0
BANK_SUM_PROOF_RADIUS_M = 8000.0


def _demote_below_measured_load(df):
    """Discard a published rating that falls below the substation's own measured load.

    A measured load is a harder fact than a published rating. If a utility's own
    metering says a substation carries 100 MW and its own planning data rates it at
    88 MW, the rating is the number that cannot be right -- the substation is
    demonstrably carrying that load. Using it anyway makes the node infeasible on
    its own demand and it sheds load in every hour of the horizon, which reads as
    congestion rather than as a contradiction between two published datasets.

    This is where SCE's two datasets disagree. GNA's facility rating runs a median
    0.74 of the ICA-derived capacity, while SCE's amp-based load profile runs a
    median 0.82 of its own projection, so wherever ``MAX_REMAIN_CAP`` is small the
    rating lands below the measured load. 13 of 131 GNA-rated substations were
    affected, carrying 101.7 GWh -- 60% of SCE's transformer-bound shortfall and
    about half of the 206 GWh total at three-IOU scope.

    Why not an emergency rating instead. Real transformers do run above nameplate:
    PG&E records 13.2% of banks above 100% with a p95 of 113%, and SCE records
    facility loading to 114%. But IEEE C57.91 emergency ratings are duration-limited,
    typically four hours with up to 5% loss of transformer life, and these nodes are
    short in 672 of 672 hours. A flat multiplier over the whole horizon would invent
    a loading category the standard does not have, and would mask the data conflict
    rather than resolve it.

    The test is deliberately against *measured* load only, never the allocated peak.
    Allocation is the weaker side of the comparison and is corrected in 08_03
    instead; demoting on an allocated peak would let an allocation error silently
    rewrite a published rating.
    """
    path = C.MESO_DIR / "measured_base_peak.csv"

    if not path.is_file():
        print("  measured-load demotion: measured_base_peak.csv missing (run 08_03); skipped")
        return pd.Series(False, index=df.index)

    meas = pd.read_csv(path)
    meas["substation_id"] = meas["substation_id"].astype(str)
    peak = meas.set_index("substation_id")["measured_peak_W"]

    m = df["substation_id"].astype(str).map(peak)
    hit = df["rating_W"].notna() & m.notna() & (df["rating_W"] < m)

    if not hit.any():
        print("  measured-load demotion: no published rating falls below its measured load")
        return pd.Series(False, index=df.index)

    shortfall_ratio = (df.loc[hit, "rating_W"] / m[hit])
    df.loc[hit, "rating_W"] = np.nan
    print(f"  measured-load demotion: {int(hit.sum()):,} published ratings fall below the "
          f"substation's own measured load and are demoted to derived "
          f"(rating/measured: median {shortfall_ratio.median():.2f}, "
          f"worst {shortfall_ratio.min():.2f})")
    return hit


def _demote_proven_incomplete_bank_sums(df, ica_ids, nodes):
    """Discard bank-sum ratings that are below their own load, where a neighbour proves why.

    Two different faults make a published rating fall below the load its substation
    already serves, and they need opposite treatment:

    * the **load allocation** put too much there, in which case the rating is right
      and the allocation should be fixed -- which is what 08_03's sibling
      reallocation addresses; or
    * the **bank inventory is incomplete**, in which case the rating is not a rating
      at all and using it manufactures congestion.

    San Francisco shows the second fault conclusively, and shows it from inside the
    same cluster. SF X (02201) carries a GRIP bank sum of 9.88 MVA and an ICA rating
    of 151.2 MW: the bank inventory understates the real capacity by 15.3x at a
    substation where both numbers exist. SF K and SF L have no ICA record, bank sums
    of 31.7 and 24.7 MW, and allocated peaks of 86.9 and 58.7 MW. Their bank sums
    cannot be trusted when a neighbour's is wrong by that margin, and between them
    they carried 17.0 of the 20.09 GWh of transformer-attributable shortfall.

    Sibling reallocation cannot fix that case: the one SF substation with real spare
    capacity, SF G at 13.1 MW on a 43.0 MW rating, is *measured*, so its load is a
    fact and cannot be overwritten. The three eligible substations hold 165.4 MW
    against 77.2 MW of bank sums, so every split leaves all three overloaded and
    pushes SF J from a healthy 0.95x to 2.14x.

    The rule is deliberately narrow, so this is a validity test on a data source
    rather than a correction toward an expected answer. A substation is demoted only
    when **both** hold:

    * its own bank-sum rating is below its own assigned peak; and
    * a substation within 8 km has both a bank sum and an ICA rating, and the ICA
      rating is at least 2x the bank sum -- demonstrated incompleteness, in that
      cluster, from the utility's own two datasets.

    Demoted substations fall through to ``peak / TYPICAL_LOADING`` like any node with
    nothing published. Nothing is clipped and no target is imposed; a rating we can
    show to be incomplete is simply not used as a rating. Substations that fail the
    first test but have no nearby proof keep their published value and stay flagged,
    because for those the allocation remains the more likely fault.
    """
    import geopandas as gpd

    tight = df["below_assigned_peak"] & df["rating_W"].notna() & ~df["substation_id"].isin(ica_ids)

    if not tight.any():
        print("  bank-sum demotion: no published rating falls below its own peak")
        return pd.Series(False, index=df.index)

    # Substations where both a bank sum and an ICA rating exist, so the two can be
    # compared directly.
    ica = pge_ica_ratings().set_index("substation_id")["rating_W"]
    both = df[df["substation_id"].isin(ica.index) & df["rating_W"].notna()].copy()
    gna = pge_ratings().set_index("substation_id")["rating_W"]
    both["bank_W"] = both["substation_id"].map(gna)
    both = both[both["bank_W"].notna() & (both["bank_W"] > 0)]
    both["factor"] = both["substation_id"].map(ica) / both["bank_W"]
    proof = both[both["factor"] >= BANK_SUM_PROOF_FACTOR]

    if proof.empty:
        print("  bank-sum demotion: no cluster has a proven-incomplete bank sum; none demoted")
        return pd.Series(False, index=df.index)

    try:
        g = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg").to_crs(C.CA_ALBERS_CRS)
    except Exception as exc:  # noqa: BLE001 - geometry optional
        print(f"  bank-sum demotion: geometry unavailable ({exc}); none demoted")
        return pd.Series(False, index=df.index)

    g["substation_id"] = g["substation_id"].astype(str)
    xy = {s: (p.x, p.y) for s, p in zip(g.substation_id, g.geometry)}
    proof_pts = [xy[s] for s in proof["substation_id"].astype(str) if s in xy]

    out = pd.Series(False, index=df.index)
    for i in df.index[tight]:
        p = xy.get(str(df.at[i, "substation_id"]))
        if p is None:
            continue
        if any((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 <= BANK_SUM_PROOF_RADIUS_M ** 2
               for q in proof_pts):
            out.at[i] = True

    df.loc[out, "rating_W"] = np.nan
    print(f"  bank-sum demotion: {int(tight.sum())} published ratings sit below their own "
          f"peak; {int(out.sum())} of them lie within "
          f"{BANK_SUM_PROOF_RADIUS_M / 1000:.0f} km of a substation whose ICA rating exceeds "
          f"its bank sum by >= {BANK_SUM_PROOF_FACTOR:.0f}x, so those are demoted to derived")
    if len(proof):
        worst = proof.nlargest(3, "factor")
        for _, r in worst.iterrows():
            print(f"    proof: {r['substation_id']} bank {r['bank_W'] / 1e6:,.1f} MW vs "
                  f"ICA {ica[r['substation_id']] / 1e6:,.1f} MW ({r['factor']:.1f}x)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--typical-loading", type=float, default=TYPICAL_LOADING)
    ap.add_argument("--no-ica", action="store_true",
                    help="Ignore the PG&E ICA capacities and use GNA bank sums only.")
    ap.add_argument("--section-agg", default="max", choices=["max", "median", "min"],
                    help="How SDG&E ICA line sections aggregate to a feeder. Mirrors "
                         "08_12's flag for PG&E. max is the default because headroom at "
                         "a section includes upstream impedance, so the section nearest "
                         "the substation is the bank-relevant one; the choice moves the "
                         "result materially and is flagged in the specification.")
    args = ap.parse_args()

    nodes_csv = C.MESO_DIR / "meso_nodes.csv"
    C.require_file(nodes_csv, hint="Run 09_01_build_ca_meso_grid.py first.")
    nodes = pd.read_csv(nodes_csv)
    nodes["substation_id"] = nodes["substation_id"].astype(str)

    print("Collecting published ratings...")
    # Order matters: drop_duplicates keeps the first, so the ICA identity wins
    # over the GNA bank sums wherever both exist.
    sources = [] if args.no_ica else [pge_ica_ratings()]
    sources += [pge_ratings(), sce_ratings(), sdge_ratings(args.section_agg)]
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

    # A published rating below the assigned peak would make the node infeasible on
    # its own demand. Two different faults can produce that, and they need opposite
    # treatment, so they are separated before any rating is derived.
    df["below_assigned_peak"] = df["rating_W"] < df["total_peak_W"]
    demoted = _demote_proven_incomplete_bank_sums(df, ica_ids, nodes)
    demoted = demoted | _demote_below_measured_load(df)

    derived = df["rating_W"].isna()
    df.loc[derived, "rating_W"] = np.maximum(
        df.loc[derived, "total_peak_W"] / max(args.typical_loading, 1e-6),
        MIN_RATING_W,
    )
    df.loc[demoted, "rating_source"] = "derived_rating_not_usable"
    # Recomputed after demotion, so the flag describes the ratings actually used.
    df["below_assigned_peak"] = df["rating_W"] < df["total_peak_W"]

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
    print(f"  nodes whose rating is still below assigned peak: "
          f"{int(df['below_assigned_peak'].sum()):,} "
          f"({int(demoted.sum()):,} ratings discarded as not usable: incomplete bank "
          f"inventory, or below the substation's own measured load)")


if __name__ == "__main__":
    main()
