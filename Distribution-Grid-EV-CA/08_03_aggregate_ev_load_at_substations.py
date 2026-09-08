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
    """Parent BA per substation: mapping vote, else nearest BA centroid."""
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


def aggregate_to_substations(
    taz_hourly: np.ndarray, taz_ids: np.ndarray, mapping: pd.DataFrame, col: str = "substation_id"
):
    map_idx = mapping.set_index("TAZ")
    sub_for_taz = map_idx.loc[taz_ids, col].astype(str).to_numpy()
    unique_subs = pd.Index(sub_for_taz).unique()
    sub_to_i = {s: i for i, s in enumerate(unique_subs)}
    out = np.zeros((len(unique_subs), taz_hourly.shape[1]), dtype=np.float64)
    for ti, sid in enumerate(sub_for_taz):
        out[sub_to_i[sid]] += taz_hourly[ti]
    return unique_subs.to_numpy(), out


def main() -> None:
    C.require_file(C.TAZ_TO_SUBSTATION_CSV, hint="Run 08_01_map_taz_to_substation.py first.")
    mapping = pd.read_csv(C.TAZ_TO_SUBSTATION_CSV)
    mapping["TAZ"] = mapping["TAZ"].astype(int)
    mapping["substation_id"] = mapping["substation_id"].astype(str)

    taz_ids = np.load(C.MESO_DIR / "taz_ids.npy")
    socio = _taz_socio_weights(taz_ids)
    mapping = mapping.merge(socio, on="TAZ", how="left")

    # Substation socio-economic weights (sum of TAZ w), then renormalize within BA
    stations = _station_geometries(mapping)
    mapped_ids = mapping["substation_id"].unique()
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

    parent_ba = _assign_parent_ba(stations, mapping)
    w_taz = mapping.set_index("TAZ").reindex(taz_ids)["w_taz"].fillna(0.0).to_numpy(dtype=float)
    sub_for_taz = mapping.set_index("TAZ").reindex(taz_ids)["substation_id"].astype(str).to_numpy()
    weight_rows = []
    for sid in pd.Index(sub_for_taz).unique():
        mask = sub_for_taz == sid
        weight_rows.append(
            {
                "substation_id": sid,
                "w_raw": float(w_taz[mask].sum()),
                "parent_ba": parent_ba.get(sid, "WEC_CALN"),
                "n_taz": int(mask.sum()),
            }
        )
    wdf = pd.DataFrame(weight_rows)
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

    sub_ids, ev_sub = aggregate_to_substations(taz_ev, taz_ids, mapping)
    ba_load = _ba_base_load_w()
    w_lookup = wdf.set_index("substation_id")
    base_sub = np.zeros_like(ev_sub, dtype=np.float64)
    for i, sid in enumerate(sub_ids):
        ba = str(w_lookup.loc[sid, "parent_ba"]) if sid in w_lookup.index else "WEC_CALN"
        ws = float(w_lookup.loc[sid, "w_s"]) if sid in w_lookup.index else 0.0
        ba_w = ba_load.get(ba, np.zeros(C.HOURS_YEAR))
        n_h = min(base_sub.shape[1], ba_w.size)
        base_sub[i, :n_h] = ws * ba_w[:n_h]
    # EV is kW; convert to W and add
    ev_sub_w = ev_sub * 1000.0
    total_w = base_sub + ev_sub_w

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
    meta = mapping[["substation_id", "substation_name", "source"]].drop_duplicates()
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
