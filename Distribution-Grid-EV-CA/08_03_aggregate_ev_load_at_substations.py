"""
08_03_aggregate_ev_load_at_substations.py

Map TAZ EV loads to substations (L^EV_{s,t}) and disaggregate WECC/NEEDS BA
non-EV baseload onto substations with socio-economic weights w_s:

    L^{total}_{s,t} = w_s * L^{base}_{BA,t} + L^{EV}_{s,t}

Writes:
  data/meso/substation_hourly_loads_8760.parquet
  data/meso/substation_weights.csv
  data/meso/top10_substations.csv
  seasonal week arrays + ranking CSVs
  figures/ASTR_diagnostics/substation_ev_concentration.png
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import common as C


def _write_wide_parquet(path: Path, ids, values: np.ndarray, id_col: str, extra: dict | None = None) -> None:
    cols = {id_col: np.asarray(ids)}
    if extra:
        cols.update(extra)
    for h in range(values.shape[1]):
        cols[f"h{h:04d}"] = values[:, h]
    C.ensure_dir(path.parent)
    pd.DataFrame(cols).to_parquet(path, index=False)


def _taz_socio_weights(taz_ids: np.ndarray) -> pd.DataFrame:
    """Housing / employment weights per TAZ; fallback to EV annual kWh."""
    ev_w = pd.DataFrame({"TAZ": taz_ids})
    ev_w["ev_kwh"] = 0.0
    if C.TAZ_TOTAL_DEMAND_CSV.is_file():
        dem = pd.read_csv(C.TAZ_TOTAL_DEMAND_CSV)
        dem["TAZ"] = dem["TAZ"].astype(int)
        ev_w = ev_w.merge(dem[["TAZ", "total_kwh"]], on="TAZ", how="left")
        ev_w["ev_kwh"] = ev_w["total_kwh"].fillna(0.0)

    hh = np.zeros(len(taz_ids), dtype=float)
    emp = np.zeros(len(taz_ids), dtype=float)
    used = "ev_kwh_fallback"
    if C.TAZ_POLYGON_SHP.is_file():
        taz = gpd.read_file(C.TAZ_POLYGON_SHP)
        taz_col = C.pick_column(taz.columns, ("TAZ", "TAZ12", "TAZ_Zone"))
        if taz_col:
            taz["TAZ"] = taz[taz_col].astype(int)
            hh_col = C.pick_column(taz.columns, C.HOUSING_COL_CANDIDATES)
            emp_col = C.pick_column(taz.columns, C.EMPLOYMENT_COL_CANDIDATES)
            lookup = taz.drop_duplicates("TAZ").set_index("TAZ")
            if hh_col:
                hh = lookup.reindex(taz_ids)[hh_col].fillna(0.0).to_numpy(dtype=float)
                used = "taz_polygon"
            if emp_col:
                emp = lookup.reindex(taz_ids)[emp_col].fillna(0.0).to_numpy(dtype=float)
                used = "taz_polygon"
    if hh.sum() <= 0 and emp.sum() <= 0:
        hh = ev_w["ev_kwh"].to_numpy(dtype=float)
        emp = hh.copy()
        used = "ev_kwh_fallback"
    hh_share = hh / hh.sum() if hh.sum() > 0 else np.zeros_like(hh)
    emp_share = emp / emp.sum() if emp.sum() > 0 else np.zeros_like(emp)
    w = 0.5 * hh_share + 0.5 * emp_share
    if w.sum() <= 0:
        w = np.full(len(taz_ids), 1.0 / max(len(taz_ids), 1))
        used = "uniform"
    return pd.DataFrame(
        {
            "TAZ": taz_ids,
            "housing": hh,
            "employment": emp,
            "w_taz": w,
            "weight_source": used,
            "ev_kwh": ev_w["ev_kwh"].to_numpy(dtype=float),
        }
    )


def _station_geometries(mapping: pd.DataFrame) -> gpd.GeoDataFrame:
    rows = []
    if C.GRIP_ED_SUBSTATIONS.is_file():
        grip = gpd.read_file(C.GRIP_ED_SUBSTATIONS)
        if grip.crs is None:
            grip = grip.set_crs(4326)
        grip = grip.to_crs(C.CA_ALBERS_CRS)
        id_col = C.pick_column(grip.columns, ("Substati00", "SUBSTATION", "SubstationID"))
        grip["substation_id"] = grip[id_col].astype(str) if id_col else grip.index.astype(str)
        for _, r in grip.iterrows():
            rows.append({"substation_id": r["substation_id"], "geometry": r.geometry, "source": "grip"})
    if C.HIFLD_SUBSTATIONS_GPKG.is_file():
        hifld = gpd.read_file(C.HIFLD_SUBSTATIONS_GPKG)
        if hifld.crs is None:
            hifld = hifld.set_crs(4326)
        hifld = hifld.to_crs(C.CA_ALBERS_CRS)
        id_col = "OBJECTID" if "OBJECTID" in hifld.columns else ("ID" if "ID" in hifld.columns else None)
        if id_col is not None:
            hifld["substation_id"] = "HIFLD_" + hifld[id_col].astype(str)
            for _, r in hifld.iterrows():
                rows.append({"substation_id": r["substation_id"], "geometry": r.geometry, "source": "hifld"})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=C.CA_ALBERS_CRS) if rows else gpd.GeoDataFrame(
        columns=["substation_id", "geometry", "source"], geometry="geometry", crs=C.CA_ALBERS_CRS
    )
    gdf = gdf.drop_duplicates("substation_id")
    return gdf


def _assign_parent_ba(stations: gpd.GeoDataFrame, mapping: pd.DataFrame) -> pd.Series:
    """Parent BA per substation, from ownership where available.

    The TAZ-vote fallback below decides a substation's BA from whichever
    zones the load allocation happened to attach to it, which fails for bulk
    hubs that serve no local load -- Tesla, a 500 kV PG&E yard, was assigned
    to BANC by a single TAZ 5.4 km away. 08_05 resolves this from the
    substation's own owner and service territory, so prefer that file.
    """
    ba_path = C.MESO_DIR / "substation_ba.csv"
    owned = None
    if ba_path.is_file():
        bdf = pd.read_csv(ba_path)
        bdf["substation_id"] = bdf["substation_id"].astype(str)
        owned = bdf.drop_duplicates("substation_id").set_index("substation_id")["parent_ba"]

    vote = (
        mapping.dropna(subset=["parent_ba"])
        .groupby("substation_id")["parent_ba"]
        .agg(lambda s: s.value_counts().index[0] if len(s) else pd.NA)
    )
    ba_gdf = gpd.GeoDataFrame(
        {
            "parent_ba": list(C.BA_CENTROIDS_LL.keys()),
            "geometry": gpd.points_from_xy(
                [v[0] for v in C.BA_CENTROIDS_LL.values()],
                [v[1] for v in C.BA_CENTROIDS_LL.values()],
            ),
        },
        crs="EPSG:4326",
    ).to_crs(C.CA_ALBERS_CRS)
    nearest = gpd.sjoin_nearest(stations, ba_gdf, how="left")
    nearest = nearest.drop_duplicates("substation_id")
    out = nearest.set_index("substation_id")["parent_ba"]
    out.update(vote)
    if owned is not None:
        before = out.copy()
        out.update(owned.reindex(out.index).dropna())
        changed = int((before != out).sum())
        print(f"  parent_ba: {out.index.isin(owned.index).sum():,} from ownership/territory "
              f"({changed:,} differ from the TAZ vote)")
    return out


def _ba_base_load_w() -> dict[str, np.ndarray]:
    """Hourly non-EV BA demand in Watts from the WECC GOOD graph."""
    path = C.resolve_wec_json()
    C.require_file(path, hint="Need Examples/WEC.json")
    with open(path, encoding="utf-8") as fh:
        graph = json.load(fh)
    nodes = graph["nodes"]
    out: dict[str, np.ndarray] = {}
    for node in nodes:
        nid = node.get("id")
        if nid not in C.CALIFORNIA_REGIONS:
            continue
        assets = node.get("assets") or {}
        profiles = node.get("profiles") or {}
        for _, asset in assets.items():
            if asset.get("_class") != "Load" or str(asset.get("type", "")).lower() != "load":
                continue
            cap = abs(float(asset.get("installed_capacity") or 0.0))
            prof = asset.get("profile")
            if isinstance(prof, str):
                prof = profiles.get(prof)
            arr = C.pad_or_wrap_hours(prof if prof is not None else [1.0], C.HOURS_YEAR)
            out[nid] = (arr * cap).astype(np.float64)
            break
        if nid not in out:
            out[nid] = np.zeros(C.HOURS_YEAR, dtype=np.float64)
    return out


# Charging-energy shares from the ASTR2026 fleet assumptions: 68% residential
# L1/L2, 4% workplace L2, 8% public L2, 20% DC fast charging. Home charging
# follows where people live, everything else follows where they work and stop.
EV_HOME_SHARE = 0.68


def load_knn_mapping(mapping: pd.DataFrame) -> pd.DataFrame:
    """Long-format TAZ -> substation weights (sum to 1 per TAZ).

    Prefers the feeder-routed mapping from 08_02, which follows the
    distribution structure -- TAZ to census block by population and jobs,
    block to feeder by longest intersection, feeder to its parent substation
    -- rather than inverse distance to whichever substation happens to be
    nearest. A TAZ can sit beside a substation that serves a different feeder
    entirely, and the ASTR2026 extended abstract already commits to replacing
    the nearest-centroid rule once feeder maps are available.

    That file carries two weights: ``w_home`` (block population) for
    residential charging and ``w_work`` (LODES workplace jobs) for workplace
    and public charging. They are blended here by the fleet's charging-energy
    shares, since the downstream pipeline carries a single EV profile per TAZ.

    Falls back to the k-nearest file, then to single-nearest.
    """
    feeder = C.DATA_DIR / "mapping" / "taz_to_substation_feeder.csv"
    if feeder.is_file():
        knn = pd.read_csv(feeder)
        knn["weight"] = (
            EV_HOME_SHARE * knn["w_home"] + (1.0 - EV_HOME_SHARE) * knn["w_work"]
        )
        knn["rank"] = 1
        print(f"  TAZ->substation: feeder-routed weights ({len(knn)} rows, "
              f"{knn['TAZ'].nunique()} TAZs, {knn['substation_id'].nunique()} substations)")
    elif C.TAZ_TO_SUBSTATION_KNN_CSV.is_file():
        knn = pd.read_csv(C.TAZ_TO_SUBSTATION_KNN_CSV)
        print(f"  TAZ->substation: k-nearest weights ({len(knn)} rows)")
    else:
        print("  TAZ->substation: WARNING single nearest only (re-run 08_01 for k-nearest)")
        knn = mapping.copy()
        knn["weight"] = 1.0
        knn["rank"] = 1
    knn["TAZ"] = knn["TAZ"].astype(int)
    knn["substation_id"] = knn["substation_id"].astype(str)

    # Any TAZ the feeder chain could not place keeps its k-nearest weights, so
    # no demand is dropped in SDG&E or the publicly owned utilities, which
    # publish no feeder data.
    if feeder.is_file() and C.TAZ_TO_SUBSTATION_KNN_CSV.is_file():
        fallback = pd.read_csv(C.TAZ_TO_SUBSTATION_KNN_CSV)
        fallback["TAZ"] = fallback["TAZ"].astype(int)
        fallback["substation_id"] = fallback["substation_id"].astype(str)
        gap = fallback[~fallback["TAZ"].isin(set(knn["TAZ"]))].copy()
        if len(gap):
            gap = _mask_fallback_to_ba(gap)
            print(f"  + {gap['TAZ'].nunique()} TAZs kept on k-nearest (no feeder coverage)")
            knn = pd.concat([knn, gap], ignore_index=True)
    return knn


def _mask_fallback_to_ba(gap: pd.DataFrame) -> pd.DataFrame:
    """Keep k-nearest fallback inside the TAZ's own balancing area.

    The feeder assignment is masked to service territory, but the fallback
    was not, so the same defect reappeared by another route: 25% of fallback
    weight (371 of 1,503 TAZ-equivalents) landed outside the TAZ's own BA,
    including 130 TAZ-equivalents of LADWP demand on SCE substations.

    The visible consequence was Station 54, a 66 kV SCE substation in Los
    Angeles that receives no feeder-routed load at all and absorbed 21.2
    TAZ-equivalents purely by proximity -- 1,847 MW, about 3% of California's
    peak at one distribution substation, and 37% of the entire S0 shortfall.

    Distance alone cannot decide this: LADWP and SCE substations are
    interleaved across Los Angeles, so the nearest substation to an LADWP
    zone is often SCE's. A TAZ with no same-BA candidate keeps its unmasked
    rows rather than losing its demand.
    """
    ba_path = C.MESO_DIR / "substation_ba.csv"
    if not ba_path.is_file() or "parent_ba" not in gap.columns:
        print("    WARNING: fallback not BA-masked (missing substation_ba.csv)")
        return gap

    ba = pd.read_csv(ba_path).drop_duplicates("substation_id")
    ba["substation_id"] = ba["substation_id"].astype(str)
    sub_ba = ba.set_index("substation_id")["parent_ba"]
    gap = gap.assign(_sub_ba=gap["substation_id"].map(sub_ba))

    same = gap[gap["_sub_ba"] == gap["parent_ba"]]
    orphan = set(gap["TAZ"]) - set(same["TAZ"])
    kept = pd.concat([same, gap[gap["TAZ"].isin(orphan)]], ignore_index=True)

    dropped = gap["weight"].sum() - kept["weight"].sum()
    # Renormalise so every TAZ still sums to 1: masking removes candidates,
    # it must not remove demand.
    kept["weight"] = kept["weight"] / kept.groupby("TAZ")["weight"].transform("sum")
    print(f"    fallback BA mask: dropped {dropped:.0f} TAZ-equivalents of "
          f"cross-territory weight; {len(orphan)} TAZs had no same-BA candidate")
    return kept.drop(columns=["_sub_ba"])


def aggregate_to_substations(
    taz_hourly: np.ndarray, taz_ids: np.ndarray, knn: pd.DataFrame, col: str = "substation_id"
):
    """Weighted sum of TAZ hourly load onto substations: out = W @ taz_hourly."""
    from scipy.sparse import csr_matrix

    taz_pos = pd.Series(np.arange(len(taz_ids)), index=taz_ids)
    m = knn[knn["TAZ"].isin(taz_pos.index)]
    missing = len(taz_pos) - m["TAZ"].nunique()
    if missing:
        lost = float(np.abs(taz_hourly[~taz_pos.index.isin(m["TAZ"])]).sum())
        print(f"  WARNING: {missing} TAZs have no substation mapping (dropping {lost:.3e} load-hours)")
    unique_subs = pd.Index(m[col].astype(str)).unique()
    W = csr_matrix(
        (
            m["weight"].to_numpy(dtype=np.float64),
            (unique_subs.get_indexer(m[col].astype(str)), taz_pos.loc[m["TAZ"]].to_numpy()),
        ),
        shape=(len(unique_subs), len(taz_ids)),
    )
    out = np.asarray(W @ np.asarray(taz_hourly, dtype=np.float64))
    return unique_subs.to_numpy(), out


def _measured_base_profiles(sub_ids) -> tuple[np.ndarray, np.ndarray]:
    """Published substation load profiles, expanded to the model calendar.

    Li & Jenn do not allocate base load at all: they read it per feeder, per
    month-hour, straight from the utility ICA files, and allocate only the EV
    increment. That separation is what keeps an estimated quantity from ever
    being summed with a published one -- our disaggregation chain gets the
    California total right to 2% but individual substations wrong by a factor
    of ten either way (p10 0.10x, p90 1.88x against PG&E's measured peaks).

    PG&E publishes kW directly. SCE publishes **amperes**: the implied voltage
    from ``MVA = A x kV x sqrt(3)`` reproduces each substation's published
    secondary voltage (2.40 -> 2.4, 4.16 -> 4.3, 12 -> 15.5, 16 -> 22.2), so
    the series is converted with the substation's own secondary voltage.

    Both sources give 288 month-hour bins -- a typical day per month, with a
    low and a high. The midpoint is used as the central estimate and mapped
    onto the calendar, so the diurnal and seasonal shape is measured while
    day-to-day weather variation still comes from the allocated series.

    Returns (profiles [n_sub, 8760] in W, mask of which rows are measured).
    """
    import pyogrio

    idx = {str(s): i for i, s in enumerate(sub_ids)}
    prof = np.zeros((len(sub_ids), C.HOURS_YEAR), dtype=np.float64)
    have = np.zeros(len(sub_ids), dtype=bool)
    bins = np.zeros((len(sub_ids), 12, 24), dtype=np.float64)

    # --- PG&E: kW, monthhour "MM_HH" -------------------------------------
    path = C.grip_layer("SubstationLoadProfile")
    n_pge = 0
    if path.is_file():
        sp = pyogrio.read_dataframe(
            str(path), columns=["subid", "monthhour", "high", "low"], read_geometry=False
        )
        for c in ("high", "low"):
            sp[c] = pd.to_numeric(sp[c], errors="coerce")
        # Use the published HIGH, not a midpoint. The midpoint understates
        # badly: for PG&E territory it gives a 13.02 GW coincident peak
        # against NREL's 20.41 GW and PG&E's real ~20-21 GW, while `high`
        # gives 15.59 GW. The remaining gap is transmission-connected load
        # and substations without a profile, and goes to the BA node.
        # `high` carries NaNs that the old midpoint silently skipped; fall
        # back to `low` for those bins, then to zero.
        sp["mid"] = sp["high"].fillna(sp["low"]).fillna(0.0).clip(lower=0)
        sp["m"] = sp["monthhour"].str[:2].astype(int) - 1
        sp["h"] = sp["monthhour"].str[3:].astype(int)
        for sid, grp in sp.groupby("subid"):
            # GRIP stores the profile key without leading zeros ("2409") while
            # the substation layer keeps them ("02409"). Matching raw found
            # only 367 of 639 published profiles; zero-padding finds 638.
            key = str(sid).strip()
            i = idx.get(key)
            if i is None:
                i = idx.get(key.zfill(5))
            if i is None:
                continue
            bins[i, grp["m"].to_numpy(), grp["h"].to_numpy()] = grp["mid"].to_numpy() * 1e3
            have[i] = True
            n_pge += 1

    # --- PG&E feeders, aggregated to their parent substation ----------------
    # Covers substations that publish no substation-level profile. Only used
    # when most of the substation's feeders actually have a profile, since a
    # partial sum would understate its load and create fake headroom.
    n_feed = 0
    fl_path = C.grip_layer("FeederLoadProfile")
    fd_path = C.GRIP_FEEDER_DETAIL
    if fl_path.is_file() and fd_path.is_file() and C.GRIP_ED_SUBSTATIONS.is_file():
        fl = pyogrio.read_dataframe(
            str(fl_path), columns=["FeederID", "MonthHour", "High", "Low"],
            read_geometry=False,
        )
        fd = pyogrio.read_dataframe(
            str(fd_path), columns=["FeederID", "Substation"], read_geometry=False
        )
        ed = pyogrio.read_dataframe(
            str(C.GRIP_ED_SUBSTATIONS), columns=["Substation", "Substati00"],
            read_geometry=False,
        )
        name_to_id = {
            str(a).strip().upper(): str(b).strip()
            for a, b in zip(ed["Substation"], ed["Substati00"])
        }
        # FeederID is an int in the profile layer and a zero-padded string in
        # the detail layer ("12011108" vs "012011108"), the same leading-zero
        # mismatch that hid 271 substation profiles. Pad both to 9 digits.
        fl["FeederID"] = fl["FeederID"].astype(str).str.strip().str.zfill(9)
        fd["FeederID"] = fd["FeederID"].astype(str).str.strip().str.zfill(9)
        fd["sid"] = fd["Substation"].astype(str).str.strip().str.upper().map(name_to_id)
        feeders_per_sub = fd.groupby("sid")["FeederID"].nunique()

        for c in ("High", "Low"):
            fl[c] = pd.to_numeric(fl[c], errors="coerce")
        fl["mid"] = fl["High"].fillna(fl["Low"]).fillna(0.0).clip(lower=0)
        fl = fl.merge(fd[["FeederID", "sid"]], on="FeederID", how="inner").dropna(subset=["sid"])
        covered = fl.groupby("sid")["FeederID"].nunique()

        fl["m"] = fl["MonthHour"].str[:2].astype(int) - 1
        fl["h"] = fl["MonthHour"].str[3:].astype(int)
        agg = fl.groupby(["sid", "m", "h"])["mid"].sum().reset_index()

        for sid, grp in agg.groupby("sid"):
            key = str(sid).strip()
            i = idx.get(key) or idx.get(key.zfill(5))
            if i is None or have[i]:
                continue
            total = feeders_per_sub.get(sid, 0)
            if total == 0 or covered.get(sid, 0) / total < 0.8:
                continue  # too few feeders measured to trust the sum
            bins[i, grp["m"].to_numpy(), grp["h"].to_numpy()] = grp["mid"].to_numpy() * 1e3
            have[i] = True
            n_feed += 1

    # --- SCE: amperes, converted with the substation's secondary voltage --
    sce_dir = C.DATA_DIR / "ica" / "sce"
    lp_path, ss_path = sce_dir / "substation_load_profile.parquet", sce_dir / "substations.parquet"
    n_sce = 0
    if lp_path.is_file() and ss_path.is_file():
        lp = pd.read_parquet(lp_path)
        ss = pd.read_parquet(ss_path)
        kv = (
            ss.assign(
                sec_kv=pd.to_numeric(
                    ss["SUBSTATION_VOLTAGE"].astype(str).str.extract(r"/\s*([0-9.]+)")[0],
                    errors="coerce",
                )
            )
            .groupby("SUB_NAME")["sec_kv"]
            .max()
        )
        name_to_node = C.sce_substation_nodes()
        for c in ("MIN_LOAD", "MAX_LOAD"):
            lp[c] = pd.to_numeric(lp[c], errors="coerce")
        lp["amps"] = lp["MAX_LOAD"].fillna(lp["MIN_LOAD"]).fillna(0.0).clip(lower=0)
        for name, grp in lp.groupby("SUBSTATION"):
            node = name_to_node.get(str(name).strip().upper())
            i = idx.get(str(node)) if node else None
            v = kv.get(name)
            if i is None or pd.isna(v) or v <= 0 or have[i]:
                continue
            watts = grp["amps"].to_numpy() * float(v) * 1e3 * np.sqrt(3.0)
            m = grp["MONTH"].to_numpy().astype(int) % 12
            h = grp["HOUR"].to_numpy().astype(int) % 24
            bins[i, m, h] = watts
            have[i] = True
            n_sce += 1

    if not have.any():
        return prof, have

    # Expand month-hour bins onto the calendar.
    hours = pd.date_range("2019-01-01", periods=C.HOURS_YEAR, freq="h")
    mo, hh = hours.month.to_numpy() - 1, hours.hour.to_numpy()
    prof[have] = bins[have][:, mo, hh]
    print(f"  measured base profiles: {int(have.sum()):,} substations "
          f"({n_pge:,} PG&E substation-level, {n_feed:,} PG&E feeder-aggregated, "
          f"{n_sce:,} SCE amps)")
    return prof, have


def _substitute_measured_base(sub_ids, base_w, w_lookup):
    """Use published profiles where they exist; allocate only the residual.

    Balancing-area hourly totals are preserved exactly, so the NREL county
    demand that anchors the statewide series is conserved. Within a BA the
    measured substations take their published value and everything else
    shares what is left, in proportion to its previously allocated share.
    """
    meas, have = _measured_base_profiles(sub_ids)
    residual_by_ba: dict[str, np.ndarray] = {}
    if not have.any():
        return base_w

    ba = np.array(
        [str(w_lookup.loc[s, "parent_ba"]) if s in w_lookup.index else "" for s in sub_ids]
    )
    out = base_w.copy()
    for b in pd.unique(ba):
        in_ba = ba == b
        if not in_ba.any():
            continue
        target = base_w[in_ba].sum(axis=0)
        m_sel = in_ba & have
        r_sel = in_ba & ~have
        if not m_sel.any():
            continue

        m_tot = meas[m_sel].sum(axis=0)
        # Never let measured exceed the BA total; leave at least 5% for the
        # substations we have no measurement for.
        over = m_tot > 0.95 * target
        k = np.ones_like(m_tot)
        k[over] = np.divide(0.95 * target[over], m_tot[over],
                            out=np.zeros_like(m_tot[over]), where=m_tot[over] > 0)
        out[m_sel] = meas[m_sel] * k

        # Non-measured substations keep their own allocated load. The leftover
        # is NOT pushed into the distribution layer.
        #
        # Forcing it there was wrong and got worse as measurement improved:
        # the BA total comes from NREL county demand while measured
        # substations report their real, lower load, so the residual absorbs a
        # discrepancy between two independent datasets. Splitting that gap
        # across whichever substations lacked a profile put 1,560 MW on
        # LARKIN (Y) and 1,279 MW on SANTA MARIA, and raised S0 shortfall from
        # 75 GWh to 291 GWh precisely *because* coverage improved from 366 to
        # 632 substations -- the same gap divided among fewer nodes.
        #
        # Part of that gap is real: county demand includes transmission-
        # connected industrial customers that never pass through a
        # distribution substation. Those belong at the BA node, at
        # transmission voltage, which is where this residual now goes.
        residual_by_ba[b] = np.clip(target - out[in_ba].sum(axis=0), 0, None)
        print(f"    {b}: {int(m_sel.sum()):,} measured, {int(r_sel.sum()):,} allocated, "
              f"residual to BA node {residual_by_ba[b].max() / 1e9:.2f} GW peak")
    np.save(C.MESO_DIR / "ba_residual_load_W.npy",
            np.array([residual_by_ba.get(b, np.zeros(base_w.shape[1]))
                      for b in sorted(residual_by_ba)]))
    with open(C.MESO_DIR / "ba_residual_load_index.json", "w", encoding="utf-8") as fh:
        json.dump(sorted(residual_by_ba), fh)
    return out


# Radius within which unmeasured substations are treated as able to share load.
# Substations this close are in the same urban network and a utility can and does
# shift load between them; two substations 100 km apart cannot share anything, so
# the reallocation below is deliberately local rather than BA-wide.
SIBLING_RADIUS_M = 5000.0

# Cap allocated load at this multiple of a substation's published rating.
# PG&E's measured loadings reach 1.37x at the 99th percentile and 3.36x at the
# extreme, so a cap near the measured p99 keeps genuine tightness while
# removing allocation artefacts.
RATING_CAP = 1.4


def _reallocate_siblings_by_rating(sub_ids, base_w, w_lookup):
    """Split each local pool of unmeasured substations in proportion to rating.

    What this fixes
    ---------------
    The TAZ -> feeder -> substation chain allocates base load with
    socio-economic weights, which take no account of how large a substation is.
    In dense urban areas that concentrates load arbitrarily. San Francisco is the
    clearest case: of eleven PG&E substations there, the seven with published
    profiles take their measured load and sit at a healthy 1.30 rating-to-load
    ratio, while the four without measurement are allocated 173.4 MW against
    120.2 MW of rating -- and the split among those four is backwards. SF K gets
    86.9 MW on a 31.7 MW rating (2.74x) and SF L 58.6 MW on 24.7 MW (2.37x),
    while SF G, which has the *largest* rating of the four at 43.0 MW, is given
    only 13.1 MW.

    Those two substations alone carried 17.0 of the 20.09 GWh of
    transformer-attributable shortfall, which is 77% of the S5-to-S4 difference
    that the corridor-versus-transformer attribution rests on.

    Why proportional to rating
    --------------------------
    A utility sizes a substation for the load it expects to serve, so the rating
    is the best available proxy for served load where no profile is published.
    Splitting a pool in proportion to rating therefore encodes "these substations
    are each loaded to the same fraction of their capacity", which is a weaker
    assumption than "load follows population density regardless of the equipment
    installed".

    How this differs from the rejected 1.4x cap
    ------------------------------------------
    `_rebalance_to_ratings` clipped each substation at a multiple of its rating
    and pushed the excess elsewhere. That bounded the overload the model exists to
    discover, and it produced a spike: p90, p95, p99 and the maximum all landed on
    the cap. This function sets no ceiling. The pool total is conserved exactly, so
    if a neighbourhood is genuinely short of transformer capacity every substation
    in it stays short -- SF's four still land at 1.44x after reallocation, because
    173.4 MW really does exceed 120.2 MW of rating. Only the *split* changes.

    Two guards
    ----------
    * **Only published or measured ratings count.** A derived rating is computed
      from the allocated peak, so allocating in proportion to it would be
      circular. Substations with derived ratings keep their allocated load.
    * **Only substations with no measured profile are moved.** Where a profile is
      published, that measurement is the answer and nothing should override it.

    Known consequence, recorded rather than hidden: within a pool every
    substation ends at the same loading ratio by construction, so this removes
    base-load variation among unmeasured siblings. All remaining variation in who
    overloads comes from the EV increment, which is allocated separately. That is
    the intended trade -- absent measurement, uniform loading is a defensible
    prior and proximity-weighted variation is a spurious one -- but it means the
    base layer no longer distinguishes between siblings.
    """
    pub = C.published_substation_ratings()
    if pub is None or pub.empty:
        print("  sibling reallocation: no published ratings available; skipped")
        return base_w

    r = pub.set_index("substation_id")["rating_W"]
    rating = np.array([float(r.get(s, 0.0)) for s in sub_ids])
    _, have_measured = _measured_base_profiles(sub_ids)

    # eligible = unmeasured, and carrying a rating that did not come from its own
    # allocated peak
    eligible = (rating > 0) & (~have_measured)
    if not eligible.any():
        print("  sibling reallocation: no eligible substations; skipped")
        return base_w

    pts = _substation_points(sub_ids, w_lookup)
    if pts is None:
        print("  sibling reallocation: no substation geometry; skipped")
        return base_w

    ba = np.array([str(w_lookup.loc[s, "parent_ba"]) if s in w_lookup.index else ""
                   for s in sub_ids])

    idx = np.where(eligible)[0]
    seen: set[int] = set()
    pools = []
    for i in idx:
        if i in seen:
            continue
        d = np.hypot(pts[idx, 0] - pts[i, 0], pts[idx, 1] - pts[i, 1])
        grp = [j for k, j in enumerate(idx)
               if d[k] <= SIBLING_RADIUS_M and ba[j] == ba[i] and j not in seen]
        if len(grp) < 2:
            seen.add(i)
            continue
        seen.update(grp)
        pools.append(np.array(grp))

    if not pools:
        print("  sibling reallocation: no multi-substation pools found; skipped")
        return base_w

    out_base = base_w.copy()
    moved = 0.0
    for grp in pools:
        share = rating[grp] / rating[grp].sum()
        # Conserve the pool's own hourly total exactly; only the split changes.
        pool_total = base_w[grp].sum(axis=0)
        out_base[grp] = share[:, None] * pool_total[None, :]
        moved += float(np.abs(out_base[grp].max(axis=1) - base_w[grp].max(axis=1)).sum()) / 2.0

    n = sum(len(g) for g in pools)
    print(f"  sibling reallocation: {len(pools):,} pools, {n:,} substations, "
          f"{moved / 1e6:,.0f} MW of peak moved between siblings "
          f"(pool totals conserved exactly)")
    return out_base


def _substation_points(sub_ids, w_lookup):
    """Projected coordinates for each substation, or None if unavailable."""
    try:
        import geopandas as gpd
        g = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg").to_crs(C.CA_ALBERS_CRS)
    except Exception as exc:  # noqa: BLE001 - geometry is optional here
        print(f"  (substation geometry unavailable: {exc})")
        return None
    g["substation_id"] = g["substation_id"].astype(str)
    lut = {str(s): (geom.x, geom.y) for s, geom in zip(g.substation_id, g.geometry)}
    pts = np.full((len(sub_ids), 2), np.nan)
    for i, s in enumerate(sub_ids):
        xy = lut.get(str(s))
        if xy is not None:
            pts[i] = xy
    if np.isnan(pts).all():
        return None
    # A substation with no geometry cannot be pooled; park it far away.
    pts[np.isnan(pts[:, 0])] = 1e12
    return pts


def _rebalance_to_ratings(sub_ids, base_w, ev_w, w_lookup):
    """Move allocated load off substations the allocation has overloaded.

    The allocation gets the totals right and the split wrong. Against PG&E's
    measured substation peaks the aggregate matches within 2% (8.2 GW
    allocated vs 8.4 GW measured), and the loading distribution tracks
    measurement up to the 90th percentile (median 0.78 vs 0.80, p90 1.06 vs
    1.03). It diverges only in the tail: our p99 is 5.4x rating against a
    measured 1.37x, with a worst case of 93x.

    The cause is geographic. In rural areas a TAZ is large and contains few
    mapped feeders, so one small substation absorbs the whole zone -- OREGON
    TRAIL takes 6.8 TAZ-equivalents and lands at 77 MW against a measured
    15 MW on an 18.5 MVA bank. Left alone this produces shortfall at those
    nodes in every scenario including the no-EV baseline, which would read as
    congestion rather than as a mapping artefact.

    Excess above the cap is redistributed within the same balancing area in
    proportion to remaining headroom, so BA totals are conserved exactly and
    only the split changes. Nodes whose rating is itself derived from their
    allocated peak are excluded, since capping those would be circular.
    """
    pub = C.published_substation_ratings()
    if pub is None or pub.empty:
        print("  rebalance: no published ratings available; skipped")
        return base_w, ev_w, base_w + ev_w

    rating = pd.Series(0.0, index=pd.Index(sub_ids, name="substation_id"))
    r = pub.set_index("substation_id")["rating_W"]
    rating.loc[rating.index.intersection(r.index)] = r.reindex(
        rating.index.intersection(r.index)
    ).to_numpy()

    ba = pd.Series(
        [str(w_lookup.loc[s, "parent_ba"]) if s in w_lookup.index else "" for s in sub_ids],
        index=rating.index,
    )
    total = base_w + ev_w
    peak = total.max(axis=1)
    cap = RATING_CAP * rating.to_numpy()
    has_rating = rating.to_numpy() > 0

    excess = np.where(has_rating, np.clip(peak - cap, 0, None), 0.0)
    if excess.sum() <= 0:
        return base_w, ev_w, total

    scale = np.ones(len(sub_ids))
    over = excess > 0
    scale[over] = cap[over] / np.maximum(peak[over], 1e-9)

    moved_by_ba: dict[str, float] = {}
    for b in pd.unique(ba):
        sel = (ba.to_numpy() == b) & over
        if sel.any():
            moved_by_ba[b] = float(excess[sel].sum())

    base_w = base_w * scale[:, None]
    ev_w = ev_w * scale[:, None]

    # Redistribute, proportional to headroom, within the same BA.
    for b, amount in moved_by_ba.items():
        sel = (ba.to_numpy() == b) & has_rating & ~over
        head = np.zeros(len(sub_ids))
        head[sel] = np.clip(cap[sel] - (base_w + ev_w).max(axis=1)[sel], 0, None)
        if head.sum() <= 0:
            print(f"  rebalance: {b} has no headroom; {amount / 1e6:.0f} MW left in place")
            continue
        share = head / head.sum()
        # Scale each receiver's own profile up so its shape is preserved.
        recv_peak = (base_w + ev_w).max(axis=1)
        factor = np.ones(len(sub_ids))
        nz = share > 0
        factor[nz] = 1.0 + (share[nz] * amount) / np.maximum(recv_peak[nz], 1e-9)
        base_w = base_w * factor[:, None]
        ev_w = ev_w * factor[:, None]

    total = base_w + ev_w
    print(f"  rebalance: {int(over.sum()):,} substations above {RATING_CAP:.1f}x rating; "
          f"{excess.sum() / 1e9:.2f} GW redistributed within BA")
    return base_w, ev_w, total


def main() -> None:
    C.require_file(C.TAZ_TO_SUBSTATION_CSV, hint="Run 08_01_map_taz_to_substation.py first.")
    mapping = pd.read_csv(C.TAZ_TO_SUBSTATION_CSV)
    mapping["TAZ"] = mapping["TAZ"].astype(int)
    mapping["substation_id"] = mapping["substation_id"].astype(str)

    taz_ids = np.load(C.MESO_DIR / "taz_ids.npy")
    socio = _taz_socio_weights(taz_ids)
    mapping = mapping.merge(socio, on="TAZ", how="left")
    knn = load_knn_mapping(mapping).merge(socio[["TAZ", "w_taz"]], on="TAZ", how="left")

    # Substation socio-economic weights (sum of TAZ w), then renormalize within BA
    stations = _station_geometries(mapping)
    mapped_ids = knn["substation_id"].unique()
    extra = [s for s in mapped_ids if s not in set(stations["substation_id"])]
    if extra:
        from shapely.geometry import Point

        gw = C.ba_gateway_points_gdf().set_index("substation_id")
        extra_rows = []
        n_unlocated = 0
        for sid in extra:
            if sid in gw.index:
                extra_rows.append(
                    {"substation_id": sid, "geometry": gw.loc[sid, "geometry"], "source": "ba_gateway"}
                )
            else:
                # Genuinely unresolvable id (not a known BA gateway proxy): a
                # Point(0,0) placeholder would make the nearest-BA join below
                # resolve to whichever BA centroid happens to be closest to
                # the CRS origin, silently mis-assigning parent_ba. Skip it
                # instead so it is excluded from parent-BA voting/geometry
                # rather than corrupting it.
                n_unlocated += 1
        if n_unlocated:
            print(f"  WARNING: {n_unlocated} substation_id(s) have no known geometry; excluded from parent_ba join")
        if extra_rows:
            stations = pd.concat(
                [stations, gpd.GeoDataFrame(extra_rows, geometry="geometry", crs=C.CA_ALBERS_CRS)],
                ignore_index=True,
            )
            stations = gpd.GeoDataFrame(stations, geometry="geometry", crs=C.CA_ALBERS_CRS)

    parent_ba = _assign_parent_ba(stations, knn)
    kt = knn[knn["TAZ"].isin(taz_ids)].assign(
        w_part=lambda d: d["weight"] * d["w_taz"].fillna(0.0)
    )
    wdf = (
        kt.groupby("substation_id")
        .agg(w_raw=("w_part", "sum"), n_taz=("TAZ", "nunique"), taz_equiv=("weight", "sum"))
        .reset_index()
    )
    wdf["parent_ba"] = wdf["substation_id"].map(parent_ba).fillna("WEC_CALN")
    wdf["parent_ba"] = wdf["parent_ba"].fillna("WEC_CALN")
    wdf["w_s"] = wdf.groupby("parent_ba")["w_raw"].transform(
        lambda s: s / s.sum() if s.sum() > 0 else np.zeros(len(s))
    )
    C.ensure_dir(C.MESO_DIR)
    wdf.to_csv(C.MESO_DIR / "substation_weights.csv", index=False)
    print(f"  weight source={socio['weight_source'].iloc[0]}; n_sub={len(wdf)}")

    # EV 8760
    ev_path = C.MESO_DIR / "taz_hourly_ev_8760.npy"
    if ev_path.is_file():
        taz_ev = np.load(ev_path)
    elif C.TAZ_HOURLY_EV_8760.is_file():
        ev_df = pd.read_parquet(C.TAZ_HOURLY_EV_8760)
        hour_cols = [c for c in ev_df.columns if c.startswith("h")]
        ev_df["TAZ"] = ev_df["TAZ"].astype(int)
        ev_df = ev_df.set_index("TAZ").reindex(taz_ids)
        taz_ev = ev_df[hour_cols].to_numpy(dtype=np.float32)
    else:
        raise FileNotFoundError("Run 08_02_hourly_ev_load_by_taz.py first.")

    sub_ids, ev_sub = aggregate_to_substations(taz_ev, taz_ids, knn)
    w_lookup = wdf.set_index("substation_id")

    base_path = C.MESO_DIR / "taz_hourly_base_8760.npy"
    base_ids_path = C.MESO_DIR / "taz_ids_base.npy"
    if base_path.is_file() and base_ids_path.is_file():
        # Measured county demand (NREL OEDI 8562) split within each county by
        # Census block population -- see 08_00_county_base_load.py. Preferred
        # over the w_s route below, whose weights fall back to EV charging
        # demand as a stand-in for base load (taz_id.shp carries no housing or
        # employment fields). That proxy is biased rural and, combined with
        # nearest-substation mapping, over-loaded sparse rural substations
        # badly enough to manufacture most of the model's shortfall.
        print("  base load: measured county demand x block population")
        base_taz = np.load(base_path)
        base_taz_ids = np.load(base_ids_path)
        order = pd.Series(np.arange(len(base_taz_ids)), index=base_taz_ids)
        pick = order.reindex(taz_ids)
        aligned = np.zeros((len(taz_ids), base_taz.shape[1]), dtype=np.float64)
        found = pick.notna().to_numpy()
        aligned[found] = base_taz[pick[found].to_numpy().astype(int)]
        n_missing = int((~found).sum())
        if n_missing:
            print(f"  WARNING: {n_missing} TAZs have no county base-load profile")
        _, base_sub = aggregate_to_substations(aligned, taz_ids, knn)
        if base_sub.shape[1] != ev_sub.shape[1]:
            n_h = min(base_sub.shape[1], ev_sub.shape[1])
            base_sub = base_sub[:, :n_h]
    else:
        print("  base load: WARNING falling back to w_s x BA baseload (run 08_00 first)")
        ba_load = _ba_base_load_w()
        base_sub = np.zeros_like(ev_sub, dtype=np.float64)
        for i, sid in enumerate(sub_ids):
            ba = str(w_lookup.loc[sid, "parent_ba"]) if sid in w_lookup.index else "WEC_CALN"
            ws = float(w_lookup.loc[sid, "w_s"]) if sid in w_lookup.index else 0.0
            ba_w = ba_load.get(ba, np.zeros(C.HOURS_YEAR))
            n_h = min(base_sub.shape[1], ba_w.size)
            base_sub[i, :n_h] = ws * ba_w[:n_h]
    # EV is kW; convert to W and add
    ev_sub_w = ev_sub * 1000.0
    base_sub = _substitute_measured_base(sub_ids, base_sub, w_lookup)
    # Split each local pool of unmeasured substations in proportion to rating.
    # The socio-economic weights take no account of installed capacity, which
    # concentrated 86.9 MW on SF K's 31.7 MW rating while leaving SF G, the
    # largest of its group, with 13.1 MW on 43.0 MW. Pool totals are conserved,
    # so this is a change of split and not a cap; see the function docstring for
    # why that distinction matters and what it costs.
    # Base load only. EV load is the quantity under study and comes from the
    # charging model: vehicles charge where they are parked, not where the
    # transformers are large, so moving EV demand toward capacity would erase
    # exactly the mismatch these scenarios exist to measure.
    base_sub = _reallocate_siblings_by_rating(sub_ids, base_sub, w_lookup)
    total_w = base_sub + ev_sub_w
    # NOTE: _rebalance_to_ratings is deliberately NOT applied. Capping
    # allocated load at a multiple of the transformer rating runs the physics
    # backwards -- load drives the rating, not the other way round -- and it
    # bounds the substation overload the model is meant to discover. It also
    # produced a spike rather than a distribution: p90, p95, p99 and the max
    # all landed on the cap. The allocation artefact it was papering over is
    # real (OREGON TRAIL: 77 MW allocated against 15 MW measured) and needs
    # fixing at the source, by using measured substation profiles where they
    # are published and by spreading rural zones that have sparse feeder
    # coverage. The function is kept only as a diagnostic.

    extra_cols = {
        "parent_ba": [str(w_lookup.loc[s, "parent_ba"]) if s in w_lookup.index else "" for s in sub_ids],
        "w_s": [float(w_lookup.loc[s, "w_s"]) if s in w_lookup.index else 0.0 for s in sub_ids],
        "ev_peak_kW": ev_sub.max(axis=1),
        "base_peak_W": base_sub.max(axis=1),
        "total_peak_W": total_w.max(axis=1),
    }
    # Store totals in kW for compactness in parquet hour columns
    total_kw = (total_w / 1000.0).astype(np.float32)
    ev_kw = ev_sub.astype(np.float32)
    _write_wide_parquet(C.SUB_HOURLY_LOADS_8760, sub_ids, total_kw, "substation_id", extra=extra_cols)
    np.save(C.MESO_DIR / "substation_ids.npy", sub_ids)
    np.save(C.MESO_DIR / "substation_hourly_total_kW_8760.npy", total_kw)
    np.save(C.MESO_DIR / "substation_hourly_ev_kW_8760.npy", ev_kw)
    print(
        f"Wrote {C.SUB_HOURLY_LOADS_8760}  n={len(sub_ids)}  "
        f"peak_EV={ev_kw.sum(0).max()/1e6:.2f} GW  "
        f"peak_total={total_kw.sum(0).max()/1e6:.2f} GW"
    )

    rank_frames = []
    for week in C.SEASONAL_WEEKS:
        name = week["name"]
        start = int(week["start_hour"])
        idx = (np.arange(C.NUM_HOURS_WEEK) + start) % C.HOURS_YEAR
        ev_week = ev_kw[:, idx]
        tot_week = total_kw[:, idx]
        wdir = C.ensure_dir(C.MESO_DIR / "seasonal" / name)
        np.save(wdir / "substation_ids.npy", sub_ids)
        np.save(wdir / "substation_hourly_kW.npy", ev_week)
        np.save(wdir / "substation_hourly_total_kW.npy", tot_week)
        rank = pd.DataFrame(
            {
                "substation_id": sub_ids,
                "week_kwh": ev_week.sum(axis=1),
                "peak_kW": ev_week.max(axis=1),
                "total_peak_kW": tot_week.max(axis=1),
                "season": name,
            }
        ).sort_values("week_kwh", ascending=False)
        rank.to_csv(wdir / "substation_ev_rank.csv", index=False)
        rank_frames.append(rank)
        print(f"  {name}: top={rank.iloc[0]['substation_id']} peak_EV={rank.iloc[0]['peak_kW']/1e3:.1f} MW")

    all_rank = pd.concat(rank_frames, ignore_index=True)
    summary = (
        all_rank.groupby("substation_id", as_index=False)
        .agg(mean_week_kwh=("week_kwh", "mean"), mean_peak_kW=("peak_kW", "mean"))
        .sort_values("mean_week_kwh", ascending=False)
    )
    summary["substation_id"] = summary["substation_id"].astype(str)
    meta = knn[["substation_id", "substation_name", "source"]].drop_duplicates("substation_id")
    summary = summary.merge(meta, on="substation_id", how="left")
    summary = summary.merge(wdf[["substation_id", "parent_ba", "w_s"]], on="substation_id", how="left")
    summary.to_csv(C.MESO_DIR / "substation_ev_rank_mean.csv", index=False)
    top10 = summary.head(10)
    top10.to_csv(C.MESO_DIR / "top10_substations.csv", index=False)
    summary.head(50).to_csv(C.MESO_DIR / "top50_substations.csv", index=False)
    print(f"Wrote top-10 substations -> {C.MESO_DIR / 'top10_substations.csv'}")

    C.ensure_dir(C.FIGURES_ASTR_DIR)
    plot_gdf = stations.merge(summary, on="substation_id", how="inner")
    plot_gdf = plot_gdf[~plot_gdf.geometry.is_empty]
    if not plot_gdf.empty:
        fig, ax = plt.subplots(figsize=(8, 9))
        vmax = max(float(plot_gdf["mean_week_kwh"].max()), 1.0)
        plot_gdf.plot(
            ax=ax,
            column="mean_week_kwh",
            markersize=np.clip(plot_gdf["mean_week_kwh"] / vmax * 80, 4, 80),
            legend=True,
            cmap="YlOrRd",
            alpha=0.85,
        )
        ax.set_title("EV charging concentration at substations\n(mean seasonal-week energy)")
        ax.set_axis_off()
        fig.tight_layout()
        fig_path = C.FIGURES_ASTR_DIR / "substation_ev_concentration.png"
        fig.savefig(fig_path, dpi=150)
        plt.close(fig)
        print(f"Wrote {fig_path}")
    else:
        print("WARNING: no station geometries to plot")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
