"""
10_05_plot_meso_maps.py

Map diagnostics for the ASTR meso layer:
  1) Meso hubs on a California outline (IDs + parent BA)
  2) Substation EV concentration with California silhouette
  3) Per-scenario before/after maps with per-hub mitigation magnitude
  4) Planned BESS capacity (S2 Store capex_capacity) by season

Writes PNGs to figures/ASTR_diagnostics/ and ASTR/.../figures/.
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
from matplotlib.colors import Normalize
from matplotlib.lines import Line2D
from shapely.ops import unary_union

import common as C

ASTR_FIG = C.PKG_DIR / "ASTR" / "ASTR2026_Li_Jenn" / "figures"
TRACT_SHP = C.DATA_DIR / "tl_2021_06_tract" / "tl_2021_06_tract.shp"

BA_COLORS = {
    "WEC_CALN": "#1f77b4",
    "WEC_BANC": "#ff7f0e",
    "WECC_SCE": "#2ca02c",
    "WEC_LADW": "#d62728",
    "WEC_SDGE": "#9467bd",
    "WECC_IID": "#8c564b",
}


def _ensure_out() -> Path:
    C.ensure_dir(C.FIGURES_ASTR_DIR)
    C.ensure_dir(ASTR_FIG)
    return C.FIGURES_ASTR_DIR


def _save(fig: plt.Figure, name: str) -> None:
    out = _ensure_out()
    for folder in (out, ASTR_FIG):
        path = folder / name
        fig.savefig(path, dpi=160, bbox_inches="tight")
        print(f"Wrote {path}")


def load_ca_outline() -> gpd.GeoDataFrame:
    """Dissolve CA census tracts to a state silhouette (EPSG:3310)."""
    cache = C.MESO_DIR / "ca_outline_3310.gpkg"
    if cache.is_file():
        return gpd.read_file(cache)
    tracts = gpd.read_file(TRACT_SHP)
    if tracts.crs is None:
        tracts = tracts.set_crs(4269)
    tracts = tracts.to_crs(C.CA_ALBERS_CRS)
    outline = gpd.GeoDataFrame(
        {"name": ["California"]},
        geometry=[unary_union(tracts.geometry)],
        crs=tracts.crs,
    )
    outline.to_file(cache, driver="GPKG")
    return outline


def load_hubs() -> gpd.GeoDataFrame:
    hubs = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg").to_crs(C.CA_ALBERS_CRS)
    nodes = pd.read_csv(C.MESO_DIR / "meso_nodes.csv")
    hubs = hubs.drop(columns=[c for c in hubs.columns if c in nodes.columns and c != "hub_id"], errors="ignore")
    hubs = hubs.merge(nodes, on="hub_id", how="left")
    return hubs


def _style_ca_ax(ax, outline: gpd.GeoDataFrame, title: str = "") -> None:
    outline.boundary.plot(ax=ax, color="#555555", linewidth=0.9)
    outline.plot(ax=ax, color="#f4f4f0", alpha=0.35)
    ax.set_axis_off()
    if title:
        ax.set_title(title, fontsize=11)
    ax.set_aspect("equal")


def _short_hub_label(hid: str) -> str:
    s = str(hid)
    if s.startswith("SUB_"):
        return s.replace("SUB_", "")[:10]
    if s.startswith("MESO_"):
        return s.replace("MESO_", "")
    return s[:10]


def plot_meso_hubs_map() -> None:
    outline = load_ca_outline()
    hubs = load_hubs()
    # mean EV load for sizing
    energy = hubs["mean_week_kwh"].fillna(0).to_numpy(dtype=float)
    size = 20 + 180 * (energy / max(energy.max(), 1.0))

    fig, ax = plt.subplots(figsize=(8.5, 10))
    if "n_substations" in hubs.columns and int(hubs["n_substations"].max()) == 1:
        title = "Substation-level meso nodes (SUB_*) on California"
    else:
        title = "Aggregated meso hubs (MESO_*) on California"
    _style_ca_ax(ax, outline, title)
    for ba, color in BA_COLORS.items():
        sub = hubs[hubs["parent_ba"] == ba]
        if sub.empty:
            continue
        s = size[hubs["parent_ba"] == ba]
        ax.scatter(
            sub.geometry.x,
            sub.geometry.y,
            s=s,
            c=color,
            alpha=0.85,
            edgecolors="white",
            linewidths=0.4,
            zorder=3,
            label=ba,
        )
    # label top EV nodes only (dense at substation resolution)
    top = set(hubs.nlargest(20, "mean_week_kwh")["hub_id"])
    for _, r in hubs.iterrows():
        hid = r["hub_id"]
        if hid not in top:
            continue
        ax.annotate(
            _short_hub_label(hid),
            (r.geometry.x, r.geometry.y),
            textcoords="offset points",
            xytext=(3, 3),
            fontsize=7,
            fontweight="bold",
            color="#222222",
            zorder=4,
        )
    ax.legend(loc="lower left", frameon=True, fontsize=8, title="Parent BA")
    ax.text(
        0.02,
        0.98,
        f"n={len(hubs)} nodes. Labels = top-20 by EV energy.\n"
        "Marker size ∝ mean seasonal-week EV energy.",
        transform=ax.transAxes,
        va="top",
        fontsize=8,
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.85, edgecolor="#cccccc"),
    )
    _save(fig, "meso_hubs_map.png")
    plt.close(fig)


def plot_substation_concentration_with_silhouette() -> None:
    outline = load_ca_outline()
    grip = gpd.read_file(C.GRIP_ED_SUBSTATIONS).to_crs(C.CA_ALBERS_CRS)
    grip["substation_id"] = grip["Substati00"].astype(str)
    rank = pd.read_csv(C.MESO_DIR / "substation_ev_rank_mean.csv")
    rank["substation_id"] = rank["substation_id"].astype(str)
    gdf = grip.merge(rank, on="substation_id", how="inner")
    if gdf.empty:
        print("WARNING: no substations to plot")
        return

    # also plot BA gateways from mapping if present
    mapping = pd.read_csv(C.MAPPING_DIR / "taz_to_substation.csv")
    mapping["substation_id"] = mapping["substation_id"].astype(str)
    gw_ids = mapping.loc[mapping["source"] == "ba_gateway", "substation_id"].unique()
    gw = rank[rank["substation_id"].isin(gw_ids)].copy()

    fig, ax = plt.subplots(figsize=(8.5, 10))
    _style_ca_ax(
        ax,
        outline,
        "EV charging concentration at substations\n(with California silhouette)",
    )
    vals = gdf["mean_week_kwh"].to_numpy(dtype=float)
    sizes = np.clip(8 + 70 * vals / max(vals.max(), 1.0), 8, 90)
    sc = ax.scatter(
        gdf.geometry.x,
        gdf.geometry.y,
        c=vals,
        s=sizes,
        cmap="YlOrRd",
        alpha=0.9,
        edgecolors="#333333",
        linewidths=0.2,
        zorder=3,
    )
    if not gw.empty and C.MESO_DIR.joinpath("meso_hubs.gpkg").is_file():
        # place gateways near BA centroids already used in 08_01
        ba_ll = {
            "BA_GW_WEC_CALN": (-122.0, 38.0),
            "BA_GW_WEC_BANC": (-121.5, 38.6),
            "BA_GW_WECC_SCE": (-117.5, 34.0),
            "BA_GW_WEC_LADW": (-118.3, 34.1),
            "BA_GW_WEC_SDGE": (-117.1, 32.8),
            "BA_GW_WECC_IID": (-115.5, 33.0),
        }
        from shapely.geometry import Point

        rows = []
        for _, r in gw.iterrows():
            lon, lat = ba_ll.get(r["substation_id"], (-120.0, 37.0))
            rows.append({"mean_week_kwh": r["mean_week_kwh"], "geometry": Point(lon, lat)})
        gw_g = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(C.CA_ALBERS_CRS)
        ax.scatter(
            gw_g.geometry.x,
            gw_g.geometry.y,
            c=gw_g["mean_week_kwh"],
            s=np.clip(12 + 80 * gw_g["mean_week_kwh"] / max(vals.max(), 1.0), 12, 100),
            cmap="YlOrRd",
            marker="D",
            edgecolors="black",
            linewidths=0.5,
            zorder=4,
            vmin=vals.min(),
            vmax=vals.max(),
            label="BA gateway proxies",
        )
        ax.legend(loc="lower left", fontsize=8)
    cbar = fig.colorbar(sc, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label("Mean seasonal-week EV energy (kWh)")
    _save(fig, "substation_ev_concentration.png")
    plt.close(fig)


def _as_array(v) -> np.ndarray:
    if v is None:
        return np.zeros(0, dtype=float)
    if isinstance(v, dict):
        if not v:
            return np.zeros(0, dtype=float)
        parts = []
        for vv in v.values():
            parts.append(np.asarray(vv, dtype=float).ravel())
        return np.concatenate(parts) if parts else np.zeros(0, dtype=float)
    return np.asarray(v, dtype=float).ravel()


def extract_hub_metrics(season: str, scenario: str) -> pd.DataFrame:
    """Per-hub EV energy, BESS discharge, and inbound BA→hub interface energy."""
    path = C.ASTR_RESULTS_DIR / season / scenario / "solution.json"
    if not path.is_file():
        return pd.DataFrame()
    with open(path, encoding="utf-8") as fh:
        sol = json.load(fh)

    rows = {}
    for hid, node in sol.get("nodes", {}).items():
        if not C.is_meso_delivery_node(hid):
            continue
        ev = 0.0
        bess = 0.0
        for name, asset in node.get("assets", {}).items():
            if "ev_load" in str(name):
                ev += -float(_as_array(asset.get("net")).sum())
            if str(name).startswith("bess"):
                bess += float(_as_array(asset.get("production")).sum())
        rows[hid] = {
            "hub_id": hid,
            "ev_Wh": max(ev, 0.0),
            "bess_discharge_Wh": max(bess, 0.0),
            "inbound_Wh": 0.0,
            "inbound_peak_W": 0.0,
            "inbound_cap_W": 0.0,
        }

    for edge in sol.get("edges", []):
        src, tgt = edge.get("source"), edge.get("target")
        if not (C.is_meso_delivery_node(tgt) and str(src).startswith("W")):
            continue
        for _, line in edge.get("lines", {}).items():
            flow = _as_array(line.get("transmission"))
            if flow.size == 0:
                continue
            rows.setdefault(
                tgt,
                {
                    "hub_id": tgt,
                    "ev_Wh": 0.0,
                    "bess_discharge_Wh": 0.0,
                    "inbound_Wh": 0.0,
                    "inbound_peak_W": 0.0,
                    "inbound_cap_W": 0.0,
                },
            )
            rows[tgt]["inbound_Wh"] += float(flow.sum())
            rows[tgt]["inbound_peak_W"] = max(
                rows[tgt]["inbound_peak_W"], float(flow.max())
            )
            cap = float(line.get("installed_capacity") or 0.0)
            rows[tgt]["inbound_cap_W"] = max(rows[tgt]["inbound_cap_W"], cap)

    return pd.DataFrame(list(rows.values()))


def _comparison_spec(scenario: str) -> tuple[str, str, str, str]:
    """Return (before_scen, after_scen, metric_label, title)."""
    if scenario == "S1":
        return (
            "S0",
            "S1",
            "EV delivery to hub (inbound interface energy)",
            "S0 → S1: introducing EV load (impact magnitude)",
        )
    if scenario == "S2":
        return (
            "S1",
            "S2",
            "Mitigation = Δ inbound + BESS discharge",
            "S1 → S2: BESS mitigation magnitude",
        )
    return (
        "S1",
        "S3",
        "Mitigation = inbound relief (S1 − S3)",
        "S1 → S3: relaxed-delivery mitigation magnitude",
    )


def compute_mitigation(before: pd.DataFrame, after: pd.DataFrame, scenario: str) -> pd.DataFrame:
    b = before.set_index("hub_id") if not before.empty else pd.DataFrame()
    a = after.set_index("hub_id") if not after.empty else pd.DataFrame()
    hubs = sorted(set(b.index) | set(a.index))
    rows = []
    for hid in hubs:
        bv = b.loc[hid] if hid in b.index else None
        av = a.loc[hid] if hid in a.index else None
        before_val = float(bv["inbound_Wh"]) if bv is not None else 0.0
        after_val = float(av["inbound_Wh"]) if av is not None else 0.0
        ev = float(av["ev_Wh"]) if av is not None else (
            float(bv["ev_Wh"]) if bv is not None else 0.0
        )
        bess = float(av["bess_discharge_Wh"]) if av is not None else 0.0
        if scenario == "S1":
            # impact: energy delivered into hub after EV is added
            mit = after_val
            before_plot = 0.0
            after_plot = after_val
        elif scenario == "S2":
            mit = (before_val - after_val) + bess
            before_plot = before_val
            after_plot = after_val
        else:  # S3
            mit = before_val - after_val
            before_plot = before_val
            after_plot = after_val
        rows.append(
            {
                "hub_id": hid,
                "before_Wh": before_plot,
                "after_Wh": after_plot,
                "mitigation_Wh": mit,
                "ev_Wh": ev,
                "bess_discharge_Wh": bess,
            }
        )
    return pd.DataFrame(rows)


def plot_scenario_before_after(season: str, scenario: str, outline, hubs_gdf) -> None:
    before_scen, after_scen, metric_label, title = _comparison_spec(scenario)
    before = extract_hub_metrics(season, before_scen)
    after = extract_hub_metrics(season, after_scen)
    if after.empty and before.empty:
        print(f"  skip {season}/{scenario}: missing solutions")
        return
    mit = compute_mitigation(before, after, scenario)
    gdf = hubs_gdf.merge(mit, on="hub_id", how="left").fillna(0.0)

    fig, axes = plt.subplots(1, 3, figsize=(14, 6.5))
    panels = [
        ("before_Wh", f"Before ({before_scen})", "Blues"),
        ("after_Wh", f"After ({after_scen})", "Oranges"),
        ("mitigation_Wh", "Mitigation / impact magnitude", "RdYlGn"),
    ]
    # shared abs scale for before/after; separate for mitigation (can be signed)
    ba_max = max(float(gdf["before_Wh"].max()), float(gdf["after_Wh"].max()), 1.0)
    mit_abs = max(float(np.abs(gdf["mitigation_Wh"]).max()), 1.0)

    for ax, (col, panel_title, cmap) in zip(axes, panels):
        _style_ca_ax(ax, outline, panel_title)
        if col == "mitigation_Wh":
            norm = Normalize(vmin=-mit_abs, vmax=mit_abs)
            sizes = 25 + 120 * np.abs(gdf[col]) / mit_abs
        else:
            norm = Normalize(vmin=0.0, vmax=ba_max)
            sizes = 25 + 120 * gdf[col] / ba_max
        sc = ax.scatter(
            gdf.geometry.x,
            gdf.geometry.y,
            c=gdf[col],
            s=sizes,
            cmap=cmap,
            norm=norm,
            alpha=0.9,
            edgecolors="#333333",
            linewidths=0.25,
            zorder=3,
        )
        # label top hubs by |value|
        top = gdf.reindex(gdf[col].abs().sort_values(ascending=False).index).head(8)
        for _, r in top.iterrows():
            ax.annotate(
                _short_hub_label(r["hub_id"]),
                (r.geometry.x, r.geometry.y),
                textcoords="offset points",
                xytext=(2, 2),
                fontsize=6,
                color="#111111",
            )
        cbar = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.02)
        cbar.set_label("Wh over week" if col != "mitigation_Wh" else "Wh (signed)")

    fig.suptitle(f"{season.capitalize()}: {title}\n{metric_label}", fontsize=12)
    fig.tight_layout()
    _save(fig, f"meso_mitigation_{season}_{scenario}.png")
    plt.close(fig)

    # save numeric table
    out_csv = C.ensure_dir(C.ASTR_RESULTS_DIR / season) / f"meso_mitigation_{scenario}.csv"
    mit.sort_values("mitigation_Wh", ascending=False).to_csv(out_csv, index=False)
    print(f"Wrote {out_csv}")


def plot_all_scenario_maps() -> None:
    outline = load_ca_outline()
    hubs = load_hubs()
    for week in C.SEASONAL_WEEKS:
        season = week["name"]
        for scen in ("S1", "S2", "S3"):
            print(f"Mapping {season} / {scen} ...")
            plot_scenario_before_after(season, scen, outline, hubs)


def _planned_bess_from_s2(season: str) -> pd.DataFrame:
    """Read S2 solution Store assets: planned power = capex_capacity (W)."""
    path = C.ASTR_RESULTS_DIR / season / "S2" / "solution.json"
    if not path.is_file():
        return pd.DataFrame(columns=["hub_id", "planned_MW", "built_MW", "discharge_Wh"])
    sol = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for nid, node in (sol.get("nodes") or {}).items():
        if not C.is_meso_delivery_node(nid):
            continue
        for aname, asset in (node.get("assets") or {}).items():
            if not str(aname).startswith("bess_"):
                continue
            planned_w = float(asset.get("capex_capacity") or 0.0)
            built_w = float(np.asarray(asset.get("capex", [0.0]), dtype=float).sum())
            disc = float(np.asarray(asset.get("production", [0.0]), dtype=float).sum())
            rows.append(
                {
                    "hub_id": str(nid),
                    "planned_MW": planned_w / 1e6,
                    "built_MW": built_w / 1e6,
                    "discharge_Wh": max(disc, 0.0),
                }
            )
    return pd.DataFrame(rows)


def plot_bess_capacity_planned() -> None:
    """Map planned BESS power capacity (S2 Store capex_capacity) by season."""
    outline = load_ca_outline()
    hubs = load_hubs()
    seasons = [w["name"] for w in C.SEASONAL_WEEKS]

    fig, axes = plt.subplots(1, 4, figsize=(16, 6.5))
    all_rows: list[pd.DataFrame] = []
    # shared color scale across seasons
    vmax = 1.0
    panels: list[tuple] = []
    for season in seasons:
        bess = _planned_bess_from_s2(season)
        bess["season"] = season
        all_rows.append(bess)
        gdf = hubs.merge(bess, on="hub_id", how="inner")
        if not gdf.empty:
            vmax = max(vmax, float(gdf["planned_MW"].max()))
        panels.append((season, gdf))

    norm = Normalize(vmin=0.0, vmax=vmax)
    for ax, (season, gdf) in zip(axes, panels):
        _style_ca_ax(ax, outline, season.capitalize())
        # faint all hubs for context
        ax.scatter(
            hubs.geometry.x,
            hubs.geometry.y,
            s=8,
            c="#bbbbbb",
            alpha=0.45,
            edgecolors="none",
            zorder=2,
        )
        if gdf.empty:
            ax.text(0.5, 0.5, "no S2 BESS", transform=ax.transAxes, ha="center")
            continue
        sizes = 40 + 220 * (gdf["planned_MW"] / vmax)
        sc = ax.scatter(
            gdf.geometry.x,
            gdf.geometry.y,
            c=gdf["planned_MW"],
            s=sizes,
            cmap="YlOrRd",
            norm=norm,
            alpha=0.92,
            edgecolors="#333333",
            linewidths=0.35,
            zorder=3,
        )
        top = gdf.nlargest(min(5, len(gdf)), "planned_MW")
        for _, r in top.iterrows():
            ax.annotate(
                f"{_short_hub_label(r['hub_id'])}\n{r['planned_MW']:.0f} MW",
                (r.geometry.x, r.geometry.y),
                textcoords="offset points",
                xytext=(3, 3),
                fontsize=6.5,
                color="#111111",
                fontweight="bold",
            )
        n = len(gdf)
        ax.text(
            0.02,
            0.02,
            f"{n} hub{'s' if n != 1 else ''}",
            transform=ax.transAxes,
            fontsize=8,
            color="#444444",
        )

    fig.subplots_adjust(left=0.02, right=0.90, top=0.82, bottom=0.06, wspace=0.08)
    cbar = fig.colorbar(sc, ax=axes.ravel().tolist(), fraction=0.025, pad=0.02)
    cbar.set_label("Planned BESS power capacity (MW)")
    fig.suptitle(
        "S2 planned BESS capacity by meso hub\n"
        "(Store capex_capacity = 0.5 × peak hub EV load; top hubs only in later seasons)",
        fontsize=12,
    )
    _save(fig, "meso_bess_capacity_planned.png")
    plt.close(fig)

    # combined CSV
    table = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if not table.empty:
        out_csv = C.ASTR_RESULTS_DIR / "meso_bess_capacity_planned.csv"
        table.sort_values(["season", "planned_MW"], ascending=[True, False]).to_csv(
            out_csv, index=False
        )
        print(f"Wrote {out_csv}")


def main() -> None:
    print("1) Meso hub locator map...")
    plot_meso_hubs_map()
    print("2) Substation concentration + CA silhouette...")
    plot_substation_concentration_with_silhouette()
    print("3) Scenario before/after mitigation maps...")
    plot_all_scenario_maps()
    print("4) Planned BESS capacity map...")
    plot_bess_capacity_planned()
    print(f"Done. See {C.FIGURES_ASTR_DIR} and {ASTR_FIG}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
