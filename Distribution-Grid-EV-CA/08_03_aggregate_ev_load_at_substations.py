"""
08_03_aggregate_ev_load_at_substations.py

Aggregate TAZ hourly EV load to substations via taz_to_substation.csv.
Writes ranking tables and a concentration map.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import common as C


def aggregate_week(taz_hourly: np.ndarray, taz_ids: np.ndarray, mapping: pd.DataFrame):
    """Return (sub_ids, sub_hourly [n_sub, 168], annual_proxy_kwh by sub)."""
    map_idx = mapping.set_index("TAZ")
    # align mapping to taz_ids order
    sub_for_taz = map_idx.loc[taz_ids, "substation_id"].to_numpy()
    unique_subs = pd.Index(sub_for_taz).unique()
    sub_to_i = {s: i for i, s in enumerate(unique_subs)}
    n_sub = len(unique_subs)
    n_h = taz_hourly.shape[1]
    out = np.zeros((n_sub, n_h), dtype=np.float64)
    for ti, sid in enumerate(sub_for_taz):
        out[sub_to_i[sid]] += taz_hourly[ti]
    # energy proxy over the week (kWh ≈ kW * 1h)
    week_kwh = out.sum(axis=1)
    return unique_subs.to_numpy(), out, week_kwh


def main() -> None:
    mapping = pd.read_csv(C.MAPPING_DIR / "taz_to_substation.csv")
    mapping["TAZ"] = mapping["TAZ"].astype(int)
    mapping["substation_id"] = mapping["substation_id"].astype(str)
    taz_ids = np.load(C.MESO_DIR / "taz_ids.npy")

    grip = gpd.read_file(C.GRIP_ED_SUBSTATIONS).to_crs(26910)
    grip["substation_id"] = grip["Substati00"].astype(str)
    stations = grip[["substation_id", "Substation", "geometry"]].copy()

    rank_frames = []
    for week in C.SEASONAL_WEEKS:
        name = week["name"]
        path = C.MESO_DIR / "seasonal" / name / "taz_hourly_kW.npy"
        taz_hourly = np.load(path)
        sub_ids, sub_hourly, week_kwh = aggregate_week(taz_hourly, taz_ids, mapping)

        wdir = C.ensure_dir(C.MESO_DIR / "seasonal" / name)
        np.save(wdir / "substation_ids.npy", sub_ids)
        np.save(wdir / "substation_hourly_kW.npy", sub_hourly.astype(np.float32))

        rank = pd.DataFrame(
            {
                "substation_id": sub_ids,
                "week_kwh": week_kwh,
                "peak_kW": sub_hourly.max(axis=1),
                "season": name,
            }
        ).sort_values("week_kwh", ascending=False)
        rank.to_csv(wdir / "substation_ev_rank.csv", index=False)
        rank_frames.append(rank)
        print(f"  {name}: {len(sub_ids)} substations; "
              f"top={rank.iloc[0]['substation_id']} "
              f"peak={rank.iloc[0]['peak_kW']/1e3:.1f} MW")

    # Mean ranking across seasons
    all_rank = pd.concat(rank_frames, ignore_index=True)
    summary = (
        all_rank.groupby("substation_id", as_index=False)
        .agg(mean_week_kwh=("week_kwh", "mean"), mean_peak_kW=("peak_kW", "mean"))
        .sort_values("mean_week_kwh", ascending=False)
    )
    summary["substation_id"] = summary["substation_id"].astype(str)
    summary = summary.merge(
        mapping[["substation_id", "substation_name", "source"]].drop_duplicates(),
        on="substation_id",
        how="left",
    )
    C.ensure_dir(C.MESO_DIR)
    summary_path = C.MESO_DIR / "substation_ev_rank_mean.csv"
    summary.to_csv(summary_path, index=False)
    print(f"Wrote {summary_path}")

    # Map of top concentration (GRIP stations only for basemap points)
    top = summary.head(50).copy()
    plot_gdf = stations.merge(summary, on="substation_id", how="inner")
    if plot_gdf.empty:
        print("WARNING: no GRIP stations matched ranking for map")
        return

    C.ensure_dir(C.FIGURES_ASTR_DIR)
    fig, ax = plt.subplots(figsize=(8, 9))
    plot_gdf.plot(
        ax=ax,
        column="mean_week_kwh",
        markersize=np.clip(plot_gdf["mean_week_kwh"] / plot_gdf["mean_week_kwh"].max() * 80, 4, 80),
        legend=True,
        cmap="YlOrRd",
        alpha=0.85,
    )
    ax.set_title("EV charging concentration at PG&E substations\n(mean seasonal-week energy)")
    ax.set_axis_off()
    fig.tight_layout()
    fig_path = C.FIGURES_ASTR_DIR / "substation_ev_concentration.png"
    fig.savefig(fig_path, dpi=150)
    plt.close(fig)
    print(f"Wrote {fig_path}")

    top_path = C.MESO_DIR / "top50_substations.csv"
    top.to_csv(top_path, index=False)
    print(f"Wrote {top_path}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
