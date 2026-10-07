"""
10_13_summary_figures.py

Maps and tables for the project summary (docs/astr2026_summary.html).

Draws the network the LP solves on -- one straight edge per substation-to-
substation corridor, as in 10_05 -- and lays the results of one run over it:
where load goes unserved, which corridors bind, where wind and solar are
spilled, and where each storage fleet sits.

Writes docs/figures/summary/*.png and summary_numbers.json beside them. The
HTML is assembled from docs/astr2026_summary.src.html with --assemble, which
inlines the figures and the generated tables into one file.

  python 10_13_summary_figures.py              # figures and numbers
  python 10_13_summary_figures.py --assemble   # then build the HTML
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from pyproj import Transformer

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

DOCS = PKG / "docs"
FIG_DIR = DOCS / "figures" / "summary"
SRC_HTML = DOCS / "astr2026_summary.src.html"
OUT_HTML = DOCS / "astr2026_summary.html"

ROOT_TAG = "four_week_WECC_SCE+WEC_CALN+WEC_SDGE"
MAP_VARIANT = ".fleet2023.rps_load"          # the run the maps are drawn from
VARIANTS = [
    ("", "IPM fleet", "published form, $50/MWh"),
    (".rps_hard", "IPM fleet", "published form, hard"),
    (".rps_load", "IPM fleet", "on load served"),
    (".fleet2023", "2023 fleet", "published form, $50/MWh"),
    (".fleet2023.rps_load", "2023 fleet", "on load served"),
]

NESTED = ["WEC_CALN", "WECC_SCE", "WEC_SDGE"]
UTIL = {"WEC_CALN": "PG&E", "WECC_SCE": "SCE", "WEC_SDGE": "SDG&E",
        "WEC_LADW": "LADWP", "WEC_BANC": "BANC", "WECC_IID": "IID"}
UTIL_COLOR = {"WEC_CALN": "#2B6CB0", "WECC_SCE": "#7B4EA3", "WEC_SDGE": "#1E8A7A"}
TERRITORY = {"Pacific Gas & Electric Company": "WEC_CALN",
             "Southern California Edison": "WECC_SCE",
             "San Diego Gas & Electric": "WEC_SDGE"}

# voltage classes: lower bound, label, colour, line width
KV = [(500, "500 kV", "#B5121B", 2.4),
      (200, "220–345 kV", "#E07B00", 1.5),
      (100, "115–161 kV", "#2F9461", 0.85),
      (50, "60–92 kV", "#5B93C9", 0.5),
      (0, "under 50 kV", "#A9A9BD", 0.35)]
PROV = [("confirmed", "line geometry and a published source agree", "#1F3B57", 1.1),
        ("asserted", "named by a published source only", "#4F8FBF", 0.8),
        ("inferred", "from line geometry only", "#C9CED6", 0.5),
        ("synthetic_feed", "synthetic radial feed", "#C0392B", 0.9)]
RATING = [("published", "published bank rating", "#1F3B57"),
          ("ica", "ICA capacity identity", "#4F8FBF"),
          ("derived", "derived from assigned peak", "#D7B56D"),
          ("derived_rating_not_usable", "derived, not usable", "#C0392B")]
FUEL = [("solar", "#F2B705"), ("wind", "#4BA3C3"), ("battery", "#2E7D4F"),
        ("natural gas", "#8A8F98"), ("hydro", "#1F5FA8"), ("pump hydro", "#1F5FA8"),
        ("nuclear", "#8E44AD"), ("geothermal", "#B5533C")]
FUEL_COLOR = dict(FUEL)
OTHER_FUEL = "#5C4A3A"

RED, AMBER, GREEN, INK, LAND, EDGE = "#C0392B", "#D99A1C", "#2E7D4F", "#16202B", "#F4F5F3", "#C9CED6"

T = Transformer.from_crs(4326, 3310, always_xy=True)
CITIES = [("San Francisco", -122.42, 37.77), ("Sacramento", -121.49, 38.58), ("San Jose", -121.89, 37.34),
          ("Fresno", -119.79, 36.74), ("Bakersfield", -119.02, 35.37), ("Los Angeles", -118.24, 34.05),
          ("San Diego", -117.16, 32.72), ("Redding", -122.39, 40.59)]
BOXES = {  # lon0, lon1, lat0, lat1
    "Bay Area": (-123.05, -121.45, 37.15, 38.45),
    "Los Angeles basin": (-119.0, -117.0, 33.45, 34.6),
    "San Diego": (-117.65, -116.55, 32.5, 33.65),
    "San Diego and Temecula": (-117.85, -116.45, 32.5, 33.65),
    "Tehachapi and Mojave": (-119.3, -116.7, 34.45, 35.75),
    "Southern San Joaquin Valley": (-121.0, -119.2, 35.5, 36.8),
}

plt.rcParams.update({"font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 9,
                     "axes.titlesize": 10.5, "axes.titleweight": "bold", "figure.dpi": 100,
                     "savefig.facecolor": "white", "legend.frameon": False})


def box(name: str) -> tuple[float, float, float, float]:
    lon0, lon1, lat0, lat1 = BOXES[name]
    x0, y0 = T.transform(lon0, lat0)
    x1, y1 = T.transform(lon1, lat1)
    return x0, x1, y0, y1


# ----------------------------------------------------------------- inputs
class Data:
    def __init__(self) -> None:
        self.hubs = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg")
        self.hubs["x"], self.hubs["y"] = self.hubs.geometry.x, self.hubs.geometry.y
        self.hubs = self.hubs.set_index("hub_id")
        self.xy = {h: (r.x, r.y) for h, r in self.hubs[["x", "y"]].iterrows()}
        self.ba = self.hubs["parent_ba"].to_dict()
        self.nested = self.hubs["parent_ba"].isin(NESTED)

        with open(C.CA_NETWORK_JSON, encoding="utf-8") as fh:
            self.net = json.load(fh)
        e = pd.DataFrame(self.net["edges"])
        e["ba_s"], e["ba_t"] = e["source"].map(self.ba), e["target"].map(self.ba)
        e["in_scope"] = e["ba_s"].isin(NESTED) & e["ba_t"].isin(NESTED)
        e["km"] = [np.hypot(self.xy[s][0] - self.xy[t][0], self.xy[s][1] - self.xy[t][1]) / 1e3
                   for s, t in zip(e["source"], e["target"])]
        e["MW"] = e["installed_capacity_W"] / 1e6
        self.edges = e

        self.outline = gpd.read_file(C.MESO_DIR / "ca_outline_26910.gpkg").to_crs(3310)
        terr = gpd.read_file(C.CEC_LSE_IOU_POU_GPKG).to_crs(3310)
        terr = terr[terr["Utility"].isin(TERRITORY)].copy()
        terr["ba"] = terr["Utility"].map(TERRITORY)
        terr["geometry"] = terr.geometry.simplify(400)
        self.terr = terr

        self.ratings = pd.read_csv(C.MESO_DIR / "substation_ratings.csv").set_index("hub_id")
        g = pd.DataFrame(self.net["generators"])
        g = g[~g["optional"] & g["hub_id"].notna()].copy()
        g["MW"] = g["installed_capacity"].astype(float).abs() / 1e6
        g["new"] = g["handle"].astype(str).str.startswith("installed_e23_")
        self.gens = g
        self.run = C.ASTR_RESULTS_DIR / f"{ROOT_TAG}{MAP_VARIANT}"

        # four-week load per hub, kW
        ids, ev, tot = None, [], []
        for wk in C.SEASONAL_WEEKS:
            d = C.MESO_DIR / "seasonal" / wk["name"]
            ids = [str(h) for h in np.load(d / "meso_hub_ids.npy", allow_pickle=True).tolist()]
            ev.append(np.load(d / "meso_hourly_kW.npy"))
            tot.append(np.load(d / "meso_hourly_total_kW.npy"))
        ev, tot = np.concatenate(ev, axis=-1), np.concatenate(tot, axis=-1)
        if ev.shape[0] != len(ids):
            ev, tot = ev.T, tot.T
        self.load = pd.DataFrame({"peak_MW": tot.max(axis=1) / 1e3, "ev_peak_MW": ev.max(axis=1) / 1e3,
                                  "energy_GWh": tot.sum(axis=1) / 1e6, "ev_GWh": ev.sum(axis=1) / 1e6}, index=ids)
        nest = [i for i, h in enumerate(ids) if self.ba.get(h) in NESTED]
        self.coincident_nested = (float(tot[nest].sum(axis=0).max() / 1e6), float(ev[nest].sum(axis=0).max() / 1e6),
                                  float(ev[nest].sum() / tot[nest].sum()))   # GW, GW, EV share of energy
        self.coincident_by_ba = {b: (tot[[i for i, h in enumerate(ids) if self.ba.get(h) == b]].sum(axis=0).max() / 1e6,
                                     ev[[i for i, h in enumerate(ids) if self.ba.get(h) == b]].sum(axis=0).max() / 1e6)
                                 for b in UTIL}

    def scen(self, name: str, file: str) -> pd.DataFrame:
        return pd.read_csv(self.run / name / file)


# ----------------------------------------------------------------- drawing
def canvas(ax, D: Data, extent=None, territories=True, cities=True, title=None):
    D.outline.plot(ax=ax, facecolor=LAND, edgecolor="#8E98A3", linewidth=0.7, zorder=0)
    if territories:
        for ba, col in UTIL_COLOR.items():
            D.terr[D.terr["ba"] == ba].plot(ax=ax, facecolor=col, edgecolor="none", alpha=0.09, zorder=0.5)
    ax.set_aspect("equal")
    ax.set_axis_off()
    if extent is not None:
        ax.set_xlim(extent[0], extent[1])
        ax.set_ylim(extent[2], extent[3])
        ax.add_patch(Rectangle((extent[0], extent[2]), extent[1] - extent[0], extent[3] - extent[2],
                               fill=False, edgecolor="#8E98A3", linewidth=0.8, zorder=20))
    if cities:
        for name, lon, lat in CITIES:
            x, y = T.transform(lon, lat)
            if extent is not None and not (extent[0] < x < extent[1] and extent[2] < y < extent[3]):
                continue
            ax.plot(x, y, marker="s", markersize=2.6, color="#3B4652", zorder=15)
            ax.annotate(name, (x, y), xytext=(4, 3), textcoords="offset points", fontsize=7.5,
                        color="#3B4652", zorder=15,
                        path_effects=[matplotlib.patheffects.withStroke(linewidth=2.2, foreground="white")])
    if title:
        ax.set_title(title, loc="left", pad=6)


def segs(D: Data, e: pd.DataFrame):
    return [(D.xy[s], D.xy[t]) for s, t in zip(e["source"], e["target"])]


def lines(ax, D: Data, e: pd.DataFrame, color, width, z=2, alpha=1.0, ls="-"):
    if len(e):
        ax.add_collection(LineCollection(segs(D, e), colors=color, linewidths=width, zorder=z, alpha=alpha,
                                         linestyles=ls, capstyle="round"))


def grey_net(ax, D: Data, scope_only=True, alpha=1.0):
    e = D.edges[D.edges["in_scope"]] if scope_only else D.edges
    lines(ax, D, e, EDGE, 0.45, z=1, alpha=alpha)


def kv_class(kv: float) -> int:
    for i, (lo, *_rest) in enumerate(KV):
        if kv >= lo:
            return i
    return len(KV) - 1


def draw_voltage(ax, D: Data, e: pd.DataFrame, scale=1.0):
    cls = e["rated_kv"].fillna(0).map(kv_class)
    for i in reversed(range(len(KV))):
        _, _, col, w = KV[i]
        sel = e[(cls == i) & (e["provenance"] != "synthetic_feed")]
        lines(ax, D, sel, col, w * scale, z=2 + (len(KV) - i))
    lines(ax, D, e[e["provenance"] == "synthetic_feed"], "#6B7682", 0.5 * scale, z=2, ls=(0, (2, 2)))


def bubbles(ax, xs, ys, values, k, color, edge="white", z=8, alpha=0.82, lw=0.5):
    order = np.argsort(-np.asarray(values))
    ax.scatter(np.asarray(xs)[order], np.asarray(ys)[order], s=np.asarray(values)[order] * k, c=color,
               edgecolors=edge, linewidths=lw, alpha=alpha, zorder=z)


def bubble_legend(ax, sizes, k, color, unit, loc="lower left", title=None, fmt="{:g}"):
    hs = [ax.scatter([], [], s=v * k, c=color, edgecolors="white", linewidths=0.5, alpha=0.82) for v in sizes]
    leg = ax.legend(hs, [f"{fmt.format(v)} {unit}" for v in sizes], loc=loc, title=title, labelspacing=1.1,
                    borderpad=0.6, handletextpad=1.0, fontsize=8, title_fontsize=8.5, scatterpoints=1)
    leg._legend_box.align = "left"
    ax.add_artist(leg)
    return leg


def save(fig, name: str, dpi: int = 150) -> Path:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    p = FIG_DIR / f"{name}.png"
    fig.savefig(p, dpi=dpi, bbox_inches="tight", pad_inches=0.06 if dpi > 150 else 0.12)
    plt.close(fig)
    print("  wrote", p.relative_to(PKG))
    return p


def state_plus_zooms(D: Data, zooms: list[str], width=13.6):
    """The state on the left and a column of detail panels on the right.

    Column widths and row heights follow the panels' own shapes, so the detail
    column is as tall as the state and no panel floats in white space.
    """
    b = D.outline.total_bounds
    a_main = (b[2] - b[0]) / (b[3] - b[1])
    inv = [(box(z)[3] - box(z)[2]) / (box(z)[1] - box(z)[0]) for z in zooms]
    ratio = a_main * sum(inv)                       # main width over detail width
    w_zoom = width / (1 + ratio)
    height = w_zoom * sum(inv) + 0.45 * len(zooms)
    fig = plt.figure(figsize=(width, height))
    gs = fig.add_gridspec(len(zooms), 2, width_ratios=[ratio, 1], height_ratios=inv, wspace=0.03, hspace=0.12)
    main = fig.add_subplot(gs[:, 0])
    canvas(main, D)
    zax = []
    for i, z in enumerate(zooms):
        ax = fig.add_subplot(gs[i, 1])
        canvas(ax, D, extent=box(z), title=z)
        x0, x1, y0, y1 = box(z)
        main.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor=INK, linewidth=0.9, zorder=18))
        main.annotate(z, (x0, y1), xytext=(0, 3), textcoords="offset points", fontsize=7.5, color=INK, zorder=18,
                      path_effects=[matplotlib.patheffects.withStroke(linewidth=2.2, foreground="white")])
        zax.append(ax)
    return fig, main, zax


# ----------------------------------------------------------------- figures
def fig_topology(D: Data):
    fig, ax = plt.subplots(figsize=(8.8, 9.8))
    canvas(ax, D)
    out = D.edges[~D.edges["in_scope"]]
    lines(ax, D, out, "#D9DCE1", 0.4, z=1)
    draw_voltage(ax, D, D.edges[D.edges["in_scope"]])
    h = D.hubs
    ax.scatter(h.loc[~D.nested, "x"], h.loc[~D.nested, "y"], s=3, facecolors="none", edgecolors="#A7AFB8",
               linewidths=0.4, zorder=9)
    ax.scatter(h.loc[D.nested, "x"], h.loc[D.nested, "y"], s=3.2, c=INK, linewidths=0, zorder=10, alpha=0.85)
    for it in D.net["interties"]:
        x, y = D.xy[it["hub_id"]]
        ax.plot(x, y, marker="D", markersize=6, markerfacecolor="white", markeredgecolor=INK, markeredgewidth=1.1, zorder=16)
        ax.annotate(it["intertie_id"].replace("_", " · "), (x, y), xytext=(8, 7), textcoords="offset points",
                    fontsize=7.5, color=INK, zorder=16,
                    path_effects=[matplotlib.patheffects.withStroke(linewidth=2.4, foreground="white")])
    for z in ("Bay Area", "Los Angeles basin", "San Diego"):
        x0, x1, y0, y1 = box(z)
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor=INK, linewidth=0.8, linestyle=(0, (4, 2)), zorder=18))
    e = D.edges[D.edges["in_scope"] & (D.edges["provenance"] != "synthetic_feed")]
    cls = e["rated_kv"].fillna(0).map(kv_class)
    hv = [Line2D([], [], color=c, lw=max(w, 0.8) * 1.3, label=f"{lab}  ({int((cls == i).sum()):,})")
          for i, (_, lab, c, w) in enumerate(KV)]
    hv.append(Line2D([], [], color="#6B7682", lw=0.9, ls=(0, (2, 2)),
                     label=f"synthetic radial feed  ({int((D.edges['in_scope'] & (D.edges['provenance'] == 'synthetic_feed')).sum()):,})"))
    l1 = ax.legend(handles=hv, loc="upper right", title="Corridors in the three nested areas", fontsize=8, title_fontsize=8.5)
    l1._legend_box.align = "left"
    ax.add_artist(l1)
    hu = [matplotlib.patches.Patch(facecolor=UTIL_COLOR[b], alpha=0.25, label=f"{UTIL[b]} territory  ({int((D.hubs['parent_ba'] == b).sum()):,} substations)")
          for b in NESTED]
    hu += [Line2D([], [], marker="o", ls="", markersize=3.5, color=INK, label="substation, solved as a node"),
           Line2D([], [], marker="o", ls="", markersize=3.5, markerfacecolor="none", markeredgecolor="#A7AFB8",
                  label=f"LADWP, BANC, IID substation, held as one bus each  ({int((~D.nested).sum()):,})"),
           Line2D([], [], marker="D", ls="", markersize=6, markerfacecolor="white", markeredgecolor=INK, label="named WECC path terminal")]
    l2 = ax.legend(handles=hu, loc="lower left", fontsize=8)
    l2._legend_box.align = "left"
    return save(fig, "01_topology")


def fig_zooms(D: Data):
    panels = [("Bay Area", "WEC_CALN"), ("Los Angeles basin", "WECC_SCE"), ("San Diego", "WEC_SDGE")]
    asp = [(box(z)[1] - box(z)[0]) / (box(z)[3] - box(z)[2]) for z, _ in panels]
    width = 15.2
    fig, axes = plt.subplots(1, 3, figsize=(width, width / sum(asp) * 0.97 + 1.15),
                             gridspec_kw={"width_ratios": asp, "wspace": 0.04})
    fig.subplots_adjust(left=0.01, right=0.99, top=0.93, bottom=0.2)
    peak = D.load["peak_MW"].reindex(D.hubs.index).fillna(0.0)
    gw = pd.DataFrame(D.net["ba_interfaces"])
    for ax, (z, ba) in zip(axes, panels):
        ext = box(z)
        canvas(ax, D, extent=ext, title=f"{UTIL[ba]} · {z}")
        lines(ax, D, D.edges[~D.edges["in_scope"]], "#D9DCE1", 0.5, z=1)
        draw_voltage(ax, D, D.edges[D.edges["in_scope"]], scale=1.6)
        h = D.hubs
        ax.scatter(h.loc[~D.nested, "x"], h.loc[~D.nested, "y"], s=8, facecolors="none", edgecolors="#A7AFB8", linewidths=0.5, zorder=9)
        for b in NESTED:
            sel = h["parent_ba"] == b
            ax.scatter(h.loc[sel, "x"], h.loc[sel, "y"], s=4 + peak[sel] * 0.5, c=UTIL_COLOR[b], edgecolors="white",
                       linewidths=0.4, alpha=0.9, zorder=10)
        g = gw[gw["hub_id"].isin(h.index[D.nested])]
        ax.scatter([D.xy[x][0] for x in g["hub_id"]], [D.xy[x][1] for x in g["hub_id"]], s=34, marker="s",
                   facecolors="none", edgecolors=INK, linewidths=0.9, zorder=12)
    hv = [Line2D([], [], color=c, lw=max(w, 0.6) * 1.8, label=lab) for _, lab, c, w in KV]
    hv.append(Line2D([], [], color="#6B7682", lw=1.0, ls=(0, (2, 2)), label="synthetic radial feed"))
    hu = [Line2D([], [], marker="o", ls="", markersize=6, color=UTIL_COLOR[b], label=f"{UTIL[b]} substation") for b in NESTED]
    hu += [Line2D([], [], marker="o", ls="", markersize=5, markerfacecolor="none", markeredgecolor="#A7AFB8", label="LADWP / BANC / IID substation"),
           Line2D([], [], marker="s", ls="", markersize=6, markerfacecolor="none", markeredgecolor=INK, label="gateway to the area bus")]
    sz = [Line2D([], [], marker="o", ls="", markersize=np.sqrt(4 + v * 0.5), color="#6B7682", label=f"{v} MW peak load") for v in (20, 100, 250)]
    fig.legend(handles=hv + hu + sz, loc="lower center", ncol=6, fontsize=8.2, bbox_to_anchor=(0.5, 0.0), columnspacing=1.6)
    return save(fig, "02_utility_detail")


def fig_provenance(D: Data):
    fig, axes = plt.subplots(1, 2, figsize=(13.6, 7.9), gridspec_kw={"wspace": 0.02})
    e = D.edges[D.edges["in_scope"]]
    ax = axes[0]
    canvas(ax, D, territories=False, cities=False, title="How each corridor is known")
    for key, _lab, col, w in reversed(PROV):
        lines(ax, D, e[e["provenance"] == key], col, w, z=2 + [p[0] for p in PROV][::-1].index(key))
    tot = len(e)
    hs = [Line2D([], [], color=c, lw=max(w, 0.8) * 1.6, label=f"{lab}  ({int((e['provenance'] == k).sum()):,}, {(e['provenance'] == k).mean() * 100:.0f}%)")
          for k, lab, c, w in PROV]
    ax.legend(handles=hs, loc="upper right", fontsize=8.2, title=f"{tot:,} corridors", title_fontsize=8.5)._legend_box.align = "left"

    ax = axes[1]
    canvas(ax, D, territories=False, cities=False, title="Where each transformer rating comes from")
    grey_net(ax, D, alpha=0.6)
    r = D.ratings.reindex(D.hubs.index[D.nested])
    hs = []
    for key, lab, col in reversed(RATING):
        sel = r.index[r["rating_source"] == key]
        ax.scatter(D.hubs.loc[sel, "x"], D.hubs.loc[sel, "y"], s=7, c=col, linewidths=0, zorder=8, alpha=0.9)
    for key, lab, col in RATING:
        n = int((r["rating_source"] == key).sum())
        hs.append(Line2D([], [], marker="o", ls="", markersize=5, color=col, label=f"{lab}  ({n:,}, {n / len(r) * 100:.0f}%)"))
    ax.legend(handles=hs, loc="upper right", fontsize=8.2, title=f"{len(r):,} substations", title_fontsize=8.5)._legend_box.align = "left"
    return save(fig, "03_provenance")


def fig_load_fleet(D: Data):
    fig, axes = plt.subplots(1, 2, figsize=(13.6, 7.9), gridspec_kw={"wspace": 0.02})
    ax = axes[0]
    canvas(ax, D, cities=False, title="Substation peak load in the four weeks, and the EV share of it")
    grey_net(ax, D, alpha=0.5)
    L = D.load.reindex(D.hubs.index[D.nested]).fillna(0.0)
    L = L[L["peak_MW"] > 0.5]
    share = (L["ev_peak_MW"] / L["peak_MW"]).clip(0, 0.3)
    k = 0.55
    order = np.argsort(-L["peak_MW"].to_numpy())
    sc = ax.scatter(D.hubs.loc[L.index, "x"].to_numpy()[order], D.hubs.loc[L.index, "y"].to_numpy()[order],
                    s=L["peak_MW"].to_numpy()[order] * k, c=share.to_numpy()[order], cmap="YlOrRd", vmin=0, vmax=0.3,
                    edgecolors="white", linewidths=0.3, alpha=0.88, zorder=8)
    bubble_legend(ax, [50, 200, 500], k, "#B8BEC6", "MW", loc="lower left", title="peak load")
    cax = ax.inset_axes([0.58, 0.9, 0.34, 0.018])
    cb = fig.colorbar(sc, cax=cax, orientation="horizontal", ticks=[0, 0.1, 0.2, 0.3])
    cb.ax.set_xticklabels(["0", "10%", "20%", "30%+"], fontsize=7.5)
    cb.set_label("EV peak over substation peak", fontsize=8, labelpad=3)
    cb.outline.set_linewidth(0.4)

    ax = axes[1]
    canvas(ax, D, cities=False, title="2023 generator fleet, as attached to substations")
    grey_net(ax, D, scope_only=False, alpha=0.5)
    g = D.gens.copy()
    g["grp"] = g["fuel"].where(g["fuel"].isin(FUEL_COLOR), "other")
    agg = g.groupby(["hub_id", "grp"]).agg(MW=("MW", "sum"), new=("new", "max")).reset_index()
    agg = agg[agg["MW"] >= 5]
    k = 0.42
    agg = agg.sort_values("MW", ascending=False)
    cols = agg["grp"].map(lambda f: FUEL_COLOR.get(f, OTHER_FUEL))
    ax.scatter([D.xy[h][0] for h in agg["hub_id"]], [D.xy[h][1] for h in agg["hub_id"]], s=agg["MW"] * k, c=cols,
               edgecolors=np.where(agg["new"], INK, "white"), linewidths=np.where(agg["new"], 0.7, 0.3), alpha=0.8, zorder=8)
    tot = g.groupby("grp")["MW"].sum()
    hs = [Line2D([], [], marker="o", ls="", markersize=6, color=c, label=f"{f}  ({tot.get(f, 0) / 1e3:.1f} GW)")
          for f, c in FUEL if f != "pump hydro"]
    hs[4] = Line2D([], [], marker="o", ls="", markersize=6, color=FUEL_COLOR["hydro"],
                   label=f"hydro and pumped hydro  ({(tot.get('hydro', 0) + tot.get('pump hydro', 0)) / 1e3:.1f} GW)")
    hs.append(Line2D([], [], marker="o", ls="", markersize=6, color=OTHER_FUEL, label=f"other  ({tot.get('other', 0) / 1e3:.1f} GW)"))
    hs.append(Line2D([], [], marker="o", ls="", markersize=6, markerfacecolor="white", markeredgecolor=INK,
                     label="dark outline: added from eGRID 2023"))
    l1 = ax.legend(handles=hs, loc="upper right", fontsize=8)
    l1._legend_box.align = "left"
    ax.add_artist(l1)
    bubble_legend(ax, [100, 500, 2000], k, "#B8BEC6", "MW", loc="lower left", title="capacity")
    return save(fig, "04_load_and_fleet")


def corridor_binding(D: Data, scenario: str) -> pd.DataFrame:
    lf = D.scen(scenario, "line_flows_summary.csv")
    lf = lf[lf["source"].astype(str).str.startswith("SUB_") & lf["target"].astype(str).str.startswith("SUB_")].copy()
    lf["a"] = np.where(lf["source"] < lf["target"], lf["source"], lf["target"])
    lf["b"] = np.where(lf["source"] < lf["target"], lf["target"], lf["source"])
    g = lf.groupby(["a", "b"]).agg(binding=("binding_hours", "max"), cap=("capacity_MW", "max"),
                                   peak=("peak_flow_MW", "max")).reset_index()
    return g.rename(columns={"a": "source", "b": "target"})


def fig_congestion(D: Data):
    zooms = ["San Diego and Temecula", "Los Angeles basin"]
    fig, main, zax = state_plus_zooms(D, zooms)
    b = corridor_binding(D, "S1")
    s = D.scen("S1", "shortfall_by_node.csv")
    s = s[s["node"].isin(D.xy) & (s["shortfall_GWh"] > 1e-6)]
    hot = b[b["binding"] >= 24].sort_values("binding")
    cmap = plt.get_cmap("YlOrRd")
    k = 46
    for ax, scale in [(main, 1.0)] + [(z, 1.7) for z in zax]:
        grey_net(ax, D)
        if len(hot):
            ax.add_collection(LineCollection(segs(D, hot), colors=cmap(0.35 + 0.65 * np.clip(hot["binding"] / 672, 0, 1)),
                                             linewidths=(0.9 + 2.2 * hot["binding"] / 672) * scale, zorder=5, capstyle="round"))
        bubbles(ax, [D.xy[n][0] for n in s["node"]], [D.xy[n][1] for n in s["node"]], s["shortfall_GWh"].to_numpy(),
                k * scale, RED, alpha=0.7)
    hs = [Line2D([], [], color=cmap(0.35 + 0.65 * v / 672), lw=0.9 + 2.2 * v / 672, label=f"{v} h") for v in (24, 168, 336, 672)]
    l1 = main.legend(handles=hs, loc="upper right", title=f"Corridor at its rating\n({len(hot):,} for a day or more of 672 h)",
                     fontsize=8, title_fontsize=8.5)
    l1._legend_box.align = "left"
    main.add_artist(l1)
    bubble_legend(main, [1, 5, 15], k, RED, "GWh", loc="lower left", title=f"Unserved load, S1\n({s['shortfall_GWh'].sum():.1f} GWh on {len(s)} substations)")
    return save(fig, "05_congestion_S1"), {"binding_corridors_ge_24h": int(len(hot)), "binding_all_672h": int((b["binding"] >= 672).sum()),
                                           "binding_any": int((b["binding"] > 0).sum())}


def over_nodes(D: Data) -> pd.DataFrame:
    """Substations whose attached plants exceed the lines and gateways leaving.

    A plant is attached to a nested substation only if its own region is nested
    as well (09_02), so only those are counted.
    """
    attached = [g for g in D.net["generators"]
                if g.get("parent_ba") in NESTED or D.ba.get(g.get("hub_id")) not in NESTED]
    inj = pd.Series(C.injection_capacity_by_hub(attached)) / 1e6
    e = D.edges
    out = pd.concat([e.groupby("source")["MW"].sum(), e.groupby("target")["MW"].sum()]).groupby(level=0).sum()
    gw = pd.DataFrame(D.net["ba_interfaces"]).groupby("hub_id")["interface_capacity_W"].sum() / 1e6
    d = pd.DataFrame({"inj": inj}).join(out.rename("lines")).join(gw.rename("gateway")).fillna(0.0)
    d["over"] = (d["inj"] - d["lines"] - d["gateway"]).clip(lower=0)
    return d


def fig_spill(D: Data):
    fig, main, zax = state_plus_zooms(D, ["Tehachapi and Mojave", "Southern San Joaquin Valley"])
    w = D.scen("S1", "wastage_by_node.csv")
    w = w[w["node"].isin(D.xy) & (w["wastage_GWh"] > 0.05)].copy()
    ov = over_nodes(D)
    w["over"] = w["node"].map(ov["over"]).fillna(0) > 1
    vre = D.gens[D.gens["fuel"].isin(["solar", "wind"])].groupby("hub_id")["MW"].sum()
    vre = vre[vre >= 20]
    k = 9.0
    for ax, scale in [(main, 1.0), (zax[0], 2.0), (zax[1], 2.0)]:
        grey_net(ax, D)
        ax.scatter([D.xy[h][0] for h in vre.index], [D.xy[h][1] for h in vre.index], s=vre.to_numpy() * 0.11 * scale,
                   facecolors="none", edgecolors="#7C8793", linewidths=0.5, zorder=6)
        for flag, edge, lw in ((False, "white", 0.5), (True, INK, 1.3)):
            sel = w[w["over"] == flag]
            bubbles(ax, [D.xy[n][0] for n in sel["node"]], [D.xy[n][1] for n in sel["node"]], sel["wastage_GWh"].to_numpy(),
                    k * scale, AMBER, edge=edge, lw=lw, alpha=0.78)
    hs = [Line2D([], [], marker="o", ls="", markersize=9, color=AMBER, markeredgecolor=INK, markeredgewidth=1.3,
                 label=f"attached plants exceed the lines leaving  ({w.loc[w['over'], 'wastage_GWh'].sum():.0f} GWh)"),
          Line2D([], [], marker="o", ls="", markersize=9, color=AMBER, markeredgecolor="white",
                 label=f"limit is further along  ({w.loc[~w['over'], 'wastage_GWh'].sum():.0f} GWh)"),
          Line2D([], [], marker="o", ls="", markersize=8, markerfacecolor="none", markeredgecolor="#7C8793",
                 label="wind and solar capacity at a substation")]
    l1 = main.legend(handles=hs, loc="upper right", fontsize=8, title="Wind and solar spilled in S1", title_fontsize=8.5)
    l1._legend_box.align = "left"
    main.add_artist(l1)
    bubble_legend(main, [10, 40, 80], k, AMBER, "GWh", loc="lower left", title="spilled in four weeks")
    return save(fig, "06_spill_S1"), {"spill_over_GWh": float(w.loc[w["over"], "wastage_GWh"].sum()),
                                      "spill_other_GWh": float(w.loc[~w["over"], "wastage_GWh"].sum()),
                                      "n_over_nodes_nested": int(((ov["over"] > 1) & ov.index.map(lambda h: D.ba.get(h) in NESTED)).sum()),
                                      "over_MW_nested": float(ov.loc[ov.index.map(lambda h: D.ba.get(h) in NESTED), "over"].sum())}


def fig_bess(D: Data):
    fig, axes = plt.subplots(1, 3, figsize=(15.4, 6.3), gridspec_kw={"wspace": 0.01})
    k = 1.15
    cand = pd.DataFrame(D.net["bess_candidates"])
    cand = cand[cand["hub_id"].map(D.ba).isin(NESTED)]
    cand["MW"] = cand["capex_capacity_W"] / 1e6
    pocket = pd.read_csv(D.run / "bess_shift_pocket.csv")
    curt = pd.read_csv(D.run / "bess_sized_to_curtailment.csv")
    out = {"default": (len(cand), cand["MW"].sum(), (cand["MW"] * cand["duration_h"]).sum()),
           "pocket": (len(pocket), pocket["power_MW"].sum(), pocket["energy_MWh"].sum()),
           "curtail": (len(curt), curt["power_MW"].sum(), curt["energy_MWh"].sum())}

    ax = axes[0]
    canvas(ax, D, cities=False, territories=False,
           title=f"S2 · on every EV substation\n{out['default'][0]:,} sites, {out['default'][1]:,.0f} MW, {out['default'][2]:,.0f} MWh")
    grey_net(ax, D, alpha=0.5)
    bubbles(ax, [D.xy[h][0] for h in cand["hub_id"]], [D.xy[h][1] for h in cand["hub_id"]], cand["MW"].to_numpy(), k * 2.2, GREEN,
            alpha=0.55, lw=0.15)
    bubble_legend(ax, [1, 5, 10], k * 2.2, GREEN, "MW", loc="lower left")

    ax = axes[1]
    canvas(ax, D, cities=False, territories=False,
           title=f"S2_shift_pocket · on the unserved load\n{out['pocket'][0]:,} sites, {out['pocket'][1]:,.0f} MW, {out['pocket'][2]:,.0f} MWh")
    grey_net(ax, D, alpha=0.5)
    nb = pocket["role"].astype(str).str.contains("neighbour")
    for sel, col in ((pocket[nb], "#7FB7A0"), (pocket[~nb], GREEN)):
        bubbles(ax, [D.xy[h][0] for h in sel["node"]], [D.xy[h][1] for h in sel["node"]], sel["power_MW"].to_numpy(), k, col, alpha=0.8)
    hs = [Line2D([], [], marker="o", ls="", markersize=7, color=GREEN, label=f"substation short of load  ({int((~nb).sum())})"),
          Line2D([], [], marker="o", ls="", markersize=7, color="#7FB7A0", label=f"neighbour inside the same pocket  ({int(nb.sum())})")]
    l1 = ax.legend(handles=hs, loc="upper right", fontsize=8)
    ax.add_artist(l1)
    bubble_legend(ax, [20, 100, 300], k, GREEN, "MW", loc="lower left")

    ax = axes[2]
    canvas(ax, D, cities=False, territories=False,
           title=f"S2_curtail · on the spill\n{out['curtail'][0]:,} sites, {out['curtail'][1]:,.0f} MW, {out['curtail'][2]:,.0f} MWh")
    grey_net(ax, D, alpha=0.5)
    bubbles(ax, [D.xy[h][0] for h in curt["node"]], [D.xy[h][1] for h in curt["node"]], curt["power_MW"].to_numpy(), k, GREEN, alpha=0.8)
    bubble_legend(ax, [100, 300, 600], k, GREEN, "MW", loc="lower left")
    return save(fig, "07_storage_fleets"), {k_: {"sites": int(v[0]), "MW": float(v[1]), "MWh": float(v[2])} for k_, v in out.items()}


def fig_remaining(D: Data):
    panels = [("S1", "S1 · EV load, grid as built"),
              ("S2_shift_pocket", "S2_shift_pocket · storage on the unserved load"),
              ("S3", "S3 · corridors ×10")]
    fig, axes = plt.subplots(1, 3, figsize=(15.4, 6.3), gridspec_kw={"wspace": 0.01})
    k = 30
    src = D.ratings["rating_source"]
    for ax, (sc, title) in zip(axes, panels):
        s = D.scen(sc, "shortfall_by_node.csv")
        s = s[s["node"].isin(D.xy) & (s["shortfall_GWh"] > 1e-6)]
        canvas(ax, D, cities=False, territories=False,
               title=f"{title}\n{s['shortfall_GWh'].sum():.1f} GWh unserved on {len(s)} substations")
        grey_net(ax, D, alpha=0.5)
        bubbles(ax, [D.xy[n][0] for n in s["node"]], [D.xy[n][1] for n in s["node"]], s["shortfall_GWh"].to_numpy(), k, RED, alpha=0.72)
    bubble_legend(axes[0], [1, 5, 15], k, RED, "GWh", loc="lower left", title="unserved in four weeks")
    return save(fig, "08_unserved_by_scenario")


# ----------------------------------------------------------------- tables
def utility_table(D: Data) -> pd.DataFrame:
    e = D.edges
    real = e[e["provenance"] != "synthetic_feed"]
    rows = {}
    ifc = pd.DataFrame(D.net["ba_interfaces"])
    measured = pd.read_csv(C.MESO_DIR / "measured_base_peak.csv") if (C.MESO_DIR / "measured_base_peak.csv").is_file() else None
    for ba in NESTED:
        h = D.hubs[D.hubs["parent_ba"] == ba]
        inside = real[(real["ba_s"] == ba) & (real["ba_t"] == ba)]
        feeds = e[(e["provenance"] == "synthetic_feed") & ((e["ba_s"] == ba) | (e["ba_t"] == ba))]
        r = D.ratings.reindex(h.index)
        g = D.gens[D.gens["hub_id"].isin(h.index) & D.gens["parent_ba"].isin(NESTED)]
        cls = inside["rated_kv"].fillna(0).map(kv_class)
        L = D.load.reindex(h.index).fillna(0.0)
        rows[UTIL[ba]] = {
            "substations": len(h),
            "source: GRIP / HIFLD": f"{int((h['source'] == 'grip').sum()):,} / {int((h['source'] != 'grip').sum()):,}",
            "corridors within the area": len(inside),
            "corridor length, km": inside["km"].sum(),
            "500 kV": int((cls == 0).sum()), "220–345 kV": int((cls == 1).sum()), "115–161 kV": int((cls == 2).sum()),
            "60–92 kV": int((cls == 3).sum()), "under 50 kV": int((cls == 4).sum()),
            "documented by a published source": (inside["provenance"].isin(["confirmed", "asserted"])).mean(),
            "synthetic radial feeds": len(feeds),
            "gateways to the area bus": int(ifc["hub_id"].isin(h.index).sum()),
            "gateway capacity, GW": ifc.loc[ifc["hub_id"].isin(h.index), "interface_capacity_W"].sum() / 1e9,
            "rating published or from ICA": r["rating_source"].isin(["published", "ica"]).mean(),
            "rating derived": r["rating_source"].astype(str).str.startswith("derived").mean(),
            "coincident peak load, GW": D.coincident_by_ba[ba][0],
            "coincident EV peak, GW": D.coincident_by_ba[ba][1],
            "EV share of four-week energy": L["ev_GWh"].sum() / L["energy_GWh"].sum(),
            "generation and storage attached, GW": g["MW"].sum() / 1e3,
            "of which solar": g.loc[g.fuel == "solar", "MW"].sum() / 1e3,
            "of which wind": g.loc[g.fuel == "wind", "MW"].sum() / 1e3,
            "of which battery": g.loc[g.fuel == "battery", "MW"].sum() / 1e3,
            "of which natural gas": g.loc[g.fuel == "natural gas", "MW"].sum() / 1e3,
        }
    return pd.DataFrame(rows)


def spill_total(run: Path, s: str) -> tuple[float, float]:
    f = run / s / "wastage_by_node.csv"
    if not f.is_file():
        return np.nan, np.nan
    w = pd.read_csv(f)
    return float(w["wastage_GWh"].sum()), float(w.loc[w["node"].astype(str).str.startswith("SUB_"), "wastage_GWh"].sum())


def scenario_tables() -> dict:
    out = {}
    for suffix, fleet, rps in VARIANTS:
        run = C.ASTR_RESULTS_DIR / f"{ROOT_TAG}{suffix}"
        f = run / "emission_accounting.csv"
        if not f.is_file():
            continue
        a = pd.read_csv(f)
        a = a[a["ef_set"] == "egrid2023_plant"].set_index("scenario")
        a["spill_GWh"] = [spill_total(run, s)[0] for s in a.index]
        a["spill_substations_GWh"] = [spill_total(run, s)[1] for s in a.index]
        rng = pd.read_csv(run / "emission_accounting_range.csv") if (run / "emission_accounting_range.csv").is_file() else None
        out[suffix] = {"fleet": fleet, "rps": rps, "table": a.reset_index().to_dict(orient="records"),
                       "range": rng.to_dict(orient="records") if rng is not None else None}
    return out


def residual_attribution(D: Data) -> dict:
    """What the unserved load left in S3 (corridors relaxed) sits on."""
    s = D.scen("S3", "shortfall_by_node.csv")
    s = s[s["shortfall_GWh"] > 0.005].set_index("node")
    r = D.ratings.reindex(s.index)
    L = D.load.reindex(s.index)
    pub = r["rating_source"].isin(["published", "ica"])
    below = pub & (r["rating_W"] / 1e6 < L["peak_MW"])
    return {"S3_GWh": float(s["shortfall_GWh"].sum()), "n": int(len(s)),
            "published_rating_below_peak_GWh": float(s.loc[below, "shortfall_GWh"].sum()), "published_rating_below_peak_n": int(below.sum()),
            "derived_GWh": float(s.loc[~pub, "shortfall_GWh"].sum()), "derived_n": int((~pub).sum()),
            "top": [(n, round(float(v), 2), UTIL.get(D.ba.get(n)), str(D.hubs.loc[n, "substation_name"]), str(r.loc[n, "rating_source"]),
                     round(float(r.loc[n, "rating_W"]) / 1e6, 1), round(float(L.loc[n, "peak_MW"]), 1))
                    for n, v in s["shortfall_GWh"].sort_values(ascending=False).head(8).items()]}


# ----------------------------------------------------------------- HTML tables
SCEN_LABEL = {
    "S0": "no EV load",
    "S1": "EV load, grid as built",
    "S2": "storage on every EV substation",
    "S2_shift_node": "storage on the unserved load, at the node",
    "S2_shift_pocket": "storage on the unserved load, node and neighbours",
    "S2_curtail": "storage on the spill",
    "S3": "corridors ×10",
    "S4": "corridors and transformers ×10",
}
SCEN_ORDER = ["S0", "S1", "S2", "S2_shift_node", "S2_shift_pocket", "S2_curtail", "S3", "S4"]


def _table(head: list[str], rows: list[list[str]], caption: str | None = None, cls: str = "") -> str:
    cap = f"<caption>{caption}</caption>" if caption else ""
    h = "".join(f"<th>{c}</th>" for c in head)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f"<td class=\"num\">{c}</td>" if i else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>"
    return f"<div class=\"tw\"><table class=\"{cls}\">{cap}<thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>"


def _kt(v) -> str:
    if v is None or pd.isna(v):
        return "–"
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v) / 1e3:,.1f}"


def _n(v, d=1) -> str:
    return "–" if v is None or pd.isna(v) else f"{v:,.{d}f}"


def _scen(s: str) -> str:
    return f"<span class=\"scen\">{s}</span><span class=\"what\">{SCEN_LABEL[s]}</span>"


def fragments(D: Data, nums: dict) -> dict[str, str]:
    import importlib.util

    out: dict[str, str] = {}

    # per-utility network detail
    u = pd.DataFrame(nums["utility"])
    count = lambda v: f"{int(v):,}"
    pct = lambda v: f"{v * 100:.0f}%"
    gw = lambda v: f"{v:.1f}"
    fmt = {
        "substations": count, "source: GRIP / HIFLD": str,
        "corridors within the area": count, "corridor length, km": lambda v: f"{v:,.0f}",
        "500 kV": count, "220–345 kV": count, "115–161 kV": count, "60–92 kV": count, "under 50 kV": count,
        "documented by a published source": pct, "synthetic radial feeds": count,
        "gateways to the area bus": count, "gateway capacity, GW": gw,
        "rating published or from ICA": pct, "rating derived": pct,
        "coincident peak load, GW": gw, "coincident EV peak, GW": lambda v: f"{v:.2f}",
        "EV share of four-week energy": lambda v: f"{v * 100:.1f}%",
        "generation and storage attached, GW": gw, "of which solar": gw, "of which wind": gw,
        "of which battery": gw, "of which natural gas": gw,
    }
    kv_rows = ("500 kV", "220–345 kV", "115–161 kV", "60–92 kV", "under 50 kV")
    groups = [("Substations", ["substations", "source: GRIP / HIFLD"]),
              ("Corridors", ["corridors within the area", "corridor length, km", *kv_rows,
                             "documented by a published source", "synthetic radial feeds"]),
              ("Link to the rest of the system", ["gateways to the area bus", "gateway capacity, GW"]),
              ("Transformer limits", ["rating published or from ICA", "rating derived"]),
              ("Load in the four weeks", ["coincident peak load, GW", "coincident EV peak, GW", "EV share of four-week energy"]),
              ("Plants on its substations", ["generation and storage attached, GW", "of which solar", "of which wind",
                                             "of which battery", "of which natural gas"])]
    body = ""
    for gname, keys in groups:
        body += f"<tr class=\"grp\"><td colspan=\"4\">{gname}</td></tr>"
        for k in keys:
            lab = k[0].upper() + k[1:]
            sub = " class=\"sub\"" if k.startswith("of which") or k in kv_rows else ""
            cells = "".join(f"<td class=\"num\">{fmt[k](u.loc[k, c])}</td>" for c in ("PG&E", "SCE", "SDG&E"))
            body += f"<tr><td{sub}>{lab}</td>{cells}</tr>"
    out["utility_table"] = ("<div class=\"tw\"><table><thead><tr><th></th><th>PG&amp;E</th><th>SCE</th><th>SDG&amp;E</th></tr></thead>"
                            f"<tbody>{body}</tbody></table></div>")

    # results per variant
    sc = nums["scenarios"]
    for suffix, key in ((".fleet2023.rps_load", "results_load"), (".fleet2023", "results_gen")):
        if suffix not in sc:
            continue
        t = pd.DataFrame(sc[suffix]["table"]).set_index("scenario")
        rows = []
        for s in SCEN_ORDER:
            if s not in t.index:
                continue
            r = t.loc[s]
            cmp_ = s not in ("S0", "S1")
            rows.append([_scen(s), _n(r["shortfall_GWh"], 2), _n(r["extra_load_GWh"], 2) if cmp_ else "–",
                         _n(r["spill_GWh"], 0), _n(r["co2_Mt"], 3),
                         _kt(r["consequential_t"]) if cmp_ else "–",
                         _kt(r["average_t"]) if cmp_ else "–",
                         _kt(r["marginal_t"]) if cmp_ else "–"])
        out[key] = _table(["Scenario", "Unserved load, GWh", "Extra load served, GWh", "Wind and solar spilled, GWh", "CO2, Mt",
                           "Consequential, kt", "Average, kt", "Marginal, kt"], rows, cls="wrap-head")
        out[key + "_rate"] = f"{t.loc['S1', 'marginal_rate_reference_kg_per_MWh']:.0f}"

    # unserved load as bars, primary variant
    t = pd.DataFrame(sc[MAP_VARIANT]["table"]).set_index("scenario")
    ref = float(t.loc["S1", "shortfall_GWh"])
    bars = ""
    for s in ["S1", "S2", "S2_curtail", "S2_shift_node", "S2_shift_pocket", "S3", "S4"]:
        v = float(t.loc[s, "shortfall_GWh"])
        bars += (f"<div class=\"bar-row\"><div class=\"bar-lab\">{_scen(s)}</div>"
                 f"<div class=\"bar-track\"><div class=\"bar-fill\" style=\"width:{v / ref * 100:.1f}%\"></div></div>"
                 f"<div class=\"bar-val\">{v:.1f}</div></div>")
    out["unserved_bars"] = f"<div class=\"bars\">{bars}</div>"

    # the same quantities across fleets and RPS treatments
    cols = [v for v in VARIANTS if v[0] in sc]
    tabs = {c[0]: pd.DataFrame(sc[c[0]]["table"]).set_index("scenario") for c in cols}

    def get(suffix, s, c, f):
        t_ = tabs[suffix]
        return f(t_.loc[s, c]) if s in t_.index else "–"

    metrics = [
        ("CO2 in S1, Mt", "S1", "co2_Mt", lambda v: _n(v, 2)),
        ("Unserved load in S1, GWh", "S1", "shortfall_GWh", lambda v: _n(v, 2)),
        ("Wind and solar spilled in S1, GWh", "S1", "spill_GWh", lambda v: _n(v, 0)),
        ("CO2 on each MWh of EV load, kg", "S1", "marginal_rate_reference_kg_per_MWh", lambda v: _n(v, 0)),
        ("Storage on the unserved load: load relieved, GWh", "S2_shift_pocket", "extra_load_GWh", lambda v: _n(v, 2)),
        ("Storage on the unserved load: consequential, kt", "S2_shift_pocket", "consequential_t", _kt),
        ("Storage on the unserved load: marginal, kt", "S2_shift_pocket", "marginal_t", _kt),
        ("Storage on the spill: consequential, kt", "S2_curtail", "consequential_t", _kt),
        ("Corridors ×10: load relieved, GWh", "S3", "extra_load_GWh", lambda v: _n(v, 2)),
        ("Corridors ×10: consequential, kt", "S3", "consequential_t", _kt),
        ("Corridors ×10: marginal, kt", "S3", "marginal_t", _kt),
        ("Unserved load left with corridors ×10, GWh", "S3", "shortfall_GWh", lambda v: _n(v, 2)),
    ]
    fleets = list(dict.fromkeys(c[1] for c in cols))
    head = "<tr><th rowspan=\"2\"></th>" + "".join(
        f"<th colspan=\"{sum(1 for c in cols if c[1] == fl)}\" class=\"span\">{fl}</th>" for fl in fleets) + "</tr>"
    head += "<tr>" + "".join(f"<th>RPS {c[2]}</th>" for c in sorted(cols, key=lambda c: fleets.index(c[1]))) + "</tr>"
    ordered = sorted(cols, key=lambda c: fleets.index(c[1]))
    body = ""
    for m in metrics:
        body += "<tr><td>" + m[0] + "</td>" + "".join(f"<td class=\"num\">{get(c[0], m[1], m[2], m[3])}</td>" for c in ordered) + "</tr>"
    out["sensitivity_table"] = f"<div class=\"tw\"><table class=\"wrap-head\"><thead>{head}</thead><tbody>{body}</tbody></table></div>"

    # dispatch against eGRID 2023
    spec = importlib.util.spec_from_file_location("dispatch_check", PKG / "10_12_dispatch_check.py")
    dc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dc)
    act = dc.actual_2023()
    dcols = [(s, f"{fl}, RPS {rp}") for s, fl, rp in VARIANTS if s in (".rps_load", ".fleet2023.rps_load", ".fleet2023")
             and (C.ASTR_RESULTS_DIR / f"{ROOT_TAG}{s}" / "S0" / "balance.json").is_file()]
    runs = {s: dc.modelled(C.ASTR_RESULTS_DIR / f"{ROOT_TAG}{s}", "S0") for s, _ in dcols}
    rows = []
    for region, states in (("West", dc.WEST), ("California", ["CA"])):
        fuels = ["coal", "natural gas", "wind", "solar", "hydro", "nuclear"] if region == "West" else ["natural gas", "wind", "solar"]
        for fuel in fuels:
            a = float(act.loc[states, fuel].sum())
            r = [f"{region} {fuel}", _n(a, 1)]
            for s, _ in dcols:
                v = float(runs[s][0].loc[states, fuel].sum()) if fuel in runs[s][0].columns else np.nan
                r.append(f"{_n(v, 1)} <span class=\"ratio\">{v / a:.2f}</span>")
            rows.append(r)
    demand = next((e["ca_demand_MWh"] for _, e in runs.values() if "ca_demand_MWh" in e), None)
    if demand is not None:
        r = ["California net imports, share of load", "17% or more"]
        for s, _ in dcols:
            e = runs[s][1]
            r.append(f"{(1 - (e['ca_generation_MWh'] - e['ca_wastage_MWh']) / (demand - e['ca_shortfall_MWh'])) * 100:.0f}%")
        rows.append(r)
    out["dispatch_table"] = _table(["TWh a year", "Actual 2023"] + [lab for _, lab in dcols], rows, cls="wrap-head")

    # what the unserved load left in S3 sits on
    res = nums["residual"]
    rows = []
    for _node, gwh, util, name, src, rating, peak in res["top"][:6]:
        nm = name.title() if name.isupper() else name
        rows.append([f"{nm} <span class=\"what\">{util}</span>", _n(gwh, 2), src.replace("ica", "ICA"), _n(rating, 1), _n(peak, 1)])
    out["residual_table"] = _table(["Substation", "Unserved in S3, GWh", "Rating source", "Rating, MW", "Peak load, MW"], rows)
    return out


# ----------------------------------------------------------------- the two-page brief
BRIEF_SRC = DOCS / "astr2026_brief.src.html"
BRIEF_OUT = DOCS / "astr2026_brief.html"
BRIEF_BOX = (-119.05, -116.35, 32.5, 34.65)      # Los Angeles basin to the border
BRIEF_RC = {"font.size": 7, "axes.titlesize": 8, "legend.fontsize": 6.6, "legend.title_fontsize": 6.8}


def _small(leg):
    """Legend text at the brief's size; bubble_legend sets the full-page size."""
    for txt in leg.get_texts():
        txt.set_fontsize(6.6)
    leg.get_title().set_fontsize(6.8)
    return leg


def fig_brief_unserved(D: Data):
    """Page-width map for the brief: the state, and the south where most of it is."""
    with plt.rc_context(BRIEF_RC):
        x0, y0 = T.transform(BRIEF_BOX[0], BRIEF_BOX[2])
        x1, y1 = T.transform(BRIEF_BOX[1], BRIEF_BOX[3])
        ext = (x0, x1, y0, y1)
        b = D.outline.total_bounds
        a_main, a_zoom = (b[2] - b[0]) / (b[3] - b[1]), (x1 - x0) / (y1 - y0)
        width = 7.4
        height = width / (a_main + a_zoom) * 0.985
        fig, (main, zoom) = plt.subplots(1, 2, figsize=(width, height + 0.22),
                                         gridspec_kw={"width_ratios": [a_main, a_zoom], "wspace": 0.02})
        fig.subplots_adjust(left=0.005, right=0.995, top=0.94, bottom=0.01)
        bind = corridor_binding(D, "S1")
        hot = bind[bind["binding"] >= 24].sort_values("binding")
        s = D.scen("S1", "shortfall_by_node.csv")
        s = s[s["node"].isin(D.xy) & (s["shortfall_GWh"] > 1e-6)]
        cmap = plt.get_cmap("YlOrRd")
        k = 17
        canvas(main, D, title="California")
        canvas(zoom, D, extent=ext, title="Los Angeles basin to the border")
        for ax, scale in ((main, 1.0), (zoom, 2.1)):
            lines(ax, D, D.edges[D.edges["in_scope"]], EDGE, 0.3 * scale ** 0.5, z=1)
            if len(hot):
                ax.add_collection(LineCollection(segs(D, hot), colors=cmap(0.35 + 0.65 * np.clip(hot["binding"] / 672, 0, 1)),
                                                 linewidths=(0.6 + 1.3 * hot["binding"] / 672) * scale ** 0.6, zorder=5, capstyle="round"))
            bubbles(ax, [D.xy[n][0] for n in s["node"]], [D.xy[n][1] for n in s["node"]], s["shortfall_GWh"].to_numpy(),
                    k * scale, RED, alpha=0.7, lw=0.4)
        main.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor=INK, linewidth=0.7, zorder=18))
        hs = [Line2D([], [], color=cmap(0.35 + 0.65 * v / 672), lw=0.9 + 1.6 * v / 672, label=f"{v} h") for v in (24, 168, 672)]
        l1 = main.legend(handles=hs, loc="upper right", title="Corridor at its rating,\nhours of 672", borderpad=0.3)
        l1._legend_box.align = "left"
        main.add_artist(l1)
        _small(bubble_legend(main, [1, 5, 15], k, RED, "GWh", loc="lower left", title="Unserved load"))
        return save(fig, "A_brief_unserved_S1", dpi=220)


def fig_brief_remaining(D: Data):
    with plt.rc_context(BRIEF_RC):
        panels = [("S1", "EV load, grid as built"),
                  ("S2_shift_pocket", "With storage on the unserved load"),
                  ("S3", "With corridors ×10")]
        b = D.outline.total_bounds
        a_main = (b[2] - b[0]) / (b[3] - b[1])
        width = 7.4
        fig, axes = plt.subplots(1, 3, figsize=(width, width / 3 / a_main * 0.9 + 0.3), gridspec_kw={"wspace": 0.0})
        fig.subplots_adjust(left=0.005, right=0.995, top=0.9, bottom=0.01)
        k = 13
        for ax, (sc, title) in zip(axes, panels):
            s = D.scen(sc, "shortfall_by_node.csv")
            s = s[s["node"].isin(D.xy) & (s["shortfall_GWh"] > 1e-6)]
            canvas(ax, D, cities=False, territories=False, title=f"{title}\n{s['shortfall_GWh'].sum():.1f} GWh unserved")
            lines(ax, D, D.edges[D.edges["in_scope"]], EDGE, 0.3, z=1, alpha=0.6)
            bubbles(ax, [D.xy[n][0] for n in s["node"]], [D.xy[n][1] for n in s["node"]], s["shortfall_GWh"].to_numpy(), k, RED,
                    alpha=0.72, lw=0.4)
        _small(bubble_legend(axes[0], [1, 5, 15], k, RED, "GWh", loc="lower left"))
        return save(fig, "B_brief_what_is_left", dpi=220)


def brief_numbers(D: Data, nums: dict) -> dict[str, str]:
    """Every figure quoted in the brief, formatted, so the page follows the results."""
    sc = nums["scenarios"]
    L = pd.DataFrame(sc[".fleet2023.rps_load"]["table"]).set_index("scenario")
    G = pd.DataFrame(sc[".fleet2023"]["table"]).set_index("scenario") if ".fleet2023" in sc else L
    s1 = float(L.loc["S1", "shortfall_GWh"])
    n: dict[str, str] = {}

    def left(s):
        return float(L.loc[s, "shortfall_GWh"])

    def pct(s):
        return f"{(s1 - left(s)) / s1 * 100:.0f}%"

    n["substations"] = f"{nums['network']['substations_nested']:,}"
    n["corridors"] = f"{nums['network']['corridors_in_scope']:,}"
    n["s0"] = f"{left('S0'):.1f}"
    n["s1"] = f"{s1:.1f}"
    n["ev_adds"] = f"{s1 - left('S0'):.1f}"
    n["s0_share"] = f"{left('S0') / s1 * 100:.0f}%"
    for key, s in (("s2", "S2"), ("pocket", "S2_shift_pocket"), ("node", "S2_shift_node"), ("s3", "S3"), ("s4", "S4"), ("curtail", "S2_curtail")):
        n[f"{key}_left"] = f"{left(s):.1f}"
        n[f"{key}_pct"] = pct(s)
        n[f"{key}_relief"] = f"{s1 - left(s):.1f}"
    n["pocket_vs_node"] = f"{abs(left('S2_shift_node') - left('S2_shift_pocket')):.1f}"
    n["storage_vs_corridor"] = f"{(s1 - left('S2_shift_pocket')) / (s1 - left('S3')) * 100:.0f}%"
    b = nums["bess"]
    for key in ("default", "pocket", "curtail"):
        n[f"{key}_MW"] = f"{b[key]['MW']:,.0f}"
        n[f"{key}_MWh"] = f"{b[key]['MWh']:,.0f}"
        n[f"{key}_sites"] = f"{b[key]['sites']:,}"
    n["rps_gap"] = f"{max(abs(float(G.loc[s, 'shortfall_GWh']) - left(s)) for s in L.index if s in G.index):.1f}"
    u = nums["S1_shortfall_by_utility"]
    n["sdge_gwh"], n["sce_gwh"], n["pge_gwh"] = (f"{u[k][1]:.1f}" for k in ("SDG&E", "SCE", "PG&E"))
    n["south_share"] = f"{(u['SDG&E'][1] + u['SCE'][1]) / s1 * 100:.0f}%"
    n["binding"] = f"{nums['congestion']['binding_corridors_ge_24h']:,}"

    r = nums["residual"]
    n["res_gwh"] = f"{r['published_rating_below_peak_GWh']:.1f}"
    n["res_n"] = f"{r['published_rating_below_peak_n']}"
    top = r["top"][0]
    n["res_top_name"], n["res_top_gwh"], n["res_top_rating"], n["res_top_peak"] = top[3], f"{top[1]:.1f}", f"{top[5]:.1f}", f"{top[6]:.0f}"

    for key, t in (("load", L), ("gen", G)):
        n[f"{key}_s1_mt"] = f"{float(t.loc['S1', 'co2_Mt']):.1f}"
        n[f"{key}_rate"] = f"{float(t.loc['S1', 'marginal_rate_reference_kg_per_MWh']):.0f}"
        n[f"{key}_s3_cons"] = _kt(t.loc["S3", "consequential_t"])
        n[f"{key}_pocket_cons"] = _kt(t.loc["S2_shift_pocket", "consequential_t"])
        n[f"{key}_pocket_marg"] = _kt(t.loc["S2_shift_pocket", "marginal_t"])
        n[f"{key}_curtail_cons"] = _kt(t.loc["S2_curtail", "consequential_t"])
    n["spill"] = f"{float(L.loc['S1', 'spill_GWh']):.0f}"
    n["spill_sub"] = f"{float(L.loc['S1', 'spill_substations_GWh']):.0f}"
    n["spill_over"] = f"{nums['spill']['spill_over_GWh']:.0f}"
    n["spill_over_pct"] = f"{nums['spill']['spill_over_GWh'] / float(L.loc['S1', 'spill_substations_GWh']) * 100:.0f}%"
    n["n_over"] = f"{nums['spill']['n_over_nodes_nested']}"
    n["over_gw"] = f"{nums['spill']['over_MW_nested'] / 1e3:.1f}"
    n["s3_spill"] = f"{float(L.loc['S3', 'spill_GWh']):.0f}"

    before = FIG_DIR / "summary_numbers.before_duplicate_fix.json"
    if before.is_file():
        old = json.loads(before.read_text(encoding="utf-8"))["scenarios"]
        o = pd.DataFrame(old[".fleet2023.rps_load"]["table"]).set_index("scenario")
        n["fix_s1_before"], n["fix_s1_after"] = f"{float(o.loc['S1', 'shortfall_GWh']):.1f}", n["s1"]
        n["fix_mt_before"], n["fix_mt_after"] = f"{float(o.loc['S1', 'co2_Mt']):.2f}", f"{float(L.loc['S1', 'co2_Mt']):.2f}"
        n["fix_s3_before"], n["fix_s3_after"] = _kt(o.loc["S3", "consequential_t"]), n["load_s3_cons"]
    return n


# ----------------------------------------------------------------- assemble
def assemble(src: Path = SRC_HTML, out: Path = OUT_HTML, page_name: str = "artifact_page.html") -> None:
    """Inline figures, tables and numbers into a template and write one file.

    ``{{fig:name}}`` becomes the figure as a data URI, ``{{frag:key}}`` a
    generated table, ``{{n:key}}`` a number from the results.
    """
    from PIL import Image

    html = src.read_text(encoding="utf-8")
    frag_file = FIG_DIR / "fragments.json"
    frags = json.loads(frag_file.read_text(encoding="utf-8")) if frag_file.is_file() else {}
    numbers = frags.get("n", {})

    def img(m):
        im = Image.open(FIG_DIR / f"{m.group(1)}.png").convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=92, method=6)
        return f"data:image/webp;base64,{base64.b64encode(buf.getvalue()).decode()}\" width=\"{im.width}\" height=\"{im.height}"

    html = re.sub(r"\{\{fig:([\w\-]+)\}\}", img, html)
    missing = [k for k in re.findall(r"\{\{frag:([\w\-]+)\}\}", html) if k not in frags]
    missing += [k for k in re.findall(r"\{\{n:([\w\-]+)\}\}", html) if k not in numbers]
    if missing:
        raise SystemExit(f"not generated: {missing}")
    html = re.sub(r"\{\{frag:([\w\-]+)\}\}", lambda m: frags[m.group(1)], html)
    html = re.sub(r"\{\{n:([\w\-]+)\}\}", lambda m: numbers[m.group(1)], html)
    head, body = html.split("<!--BODY-->")
    page = ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            + head + "</head>\n<body>\n" + body + "\n</body>\n</html>\n")
    out.write_text(page, encoding="utf-8")
    (FIG_DIR / page_name).write_text(head + body, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assemble", action="store_true", help="Build the HTML from the template and the figures.")
    ap.add_argument("--brief", action="store_true", help="Build the two-page brief instead of the full summary.")
    ap.add_argument("--only", nargs="*", default=None, help="Figure names to redraw.")
    args = ap.parse_args()
    if args.brief:
        assemble(BRIEF_SRC, BRIEF_OUT, "brief_page.html")
        return
    if args.assemble:
        assemble()
        return

    D = Data()
    nums: dict = {"map_variant": MAP_VARIANT}
    todo = {"topology": fig_topology, "zooms": fig_zooms, "provenance": fig_provenance, "load_fleet": fig_load_fleet,
            "congestion": fig_congestion, "spill": fig_spill, "bess": fig_bess, "remaining": fig_remaining,
            "brief_unserved": fig_brief_unserved, "brief_remaining": fig_brief_remaining}
    prev = FIG_DIR / "summary_numbers.json"
    kept = json.loads(prev.read_text(encoding="utf-8")) if prev.is_file() else {}
    for name, fn in todo.items():
        if args.only is not None and name not in args.only:
            if name in kept:
                nums[name] = kept[name]
            continue
        res = fn(D)
        if isinstance(res, tuple):
            nums[name] = res[1]
    e = D.edges
    nums["network"] = {
        "substations_all": int(len(D.hubs)), "substations_nested": int(D.nested.sum()),
        "corridors_all": int(len(e)), "corridors_in_scope": int(e["in_scope"].sum()),
        "synthetic_feeds_all": int((e["provenance"] == "synthetic_feed").sum()),
        "provenance_in_scope": e.loc[e["in_scope"], "provenance"].value_counts().to_dict(),
        "rating_source_nested": D.ratings.reindex(D.hubs.index[D.nested])["rating_source"].value_counts().to_dict(),
        "generators_mapped": int(len(D.gens)),
        "generation_GW_nested": float(D.gens[D.gens["hub_id"].map(D.ba).isin(NESTED) & D.gens["parent_ba"].isin(NESTED)]["MW"].sum() / 1e3),
        "coincident_peak_GW_nested": D.coincident_nested[0], "coincident_ev_peak_GW_nested": D.coincident_nested[1],
        "ev_share_of_energy_nested": D.coincident_nested[2],
    }
    nums["utility"] = utility_table(D).to_dict()
    nums["scenarios"] = scenario_tables()
    nums["residual"] = residual_attribution(D)
    s1 = D.scen("S1", "shortfall_by_node.csv")
    nums["S1_shortfall_by_utility"] = {UTIL[b]: [int(((s1["node"].map(D.ba) == b) & (s1["shortfall_GWh"] > 0.005)).sum()),
                                                 float(s1.loc[s1["node"].map(D.ba) == b, "shortfall_GWh"].sum())] for b in NESTED}
    w1 = D.scen("S1", "wastage_by_node.csv")
    nums["S1_spill_by_utility"] = {UTIL[b]: float(w1.loc[w1["node"].map(D.ba) == b, "wastage_GWh"].sum()) for b in NESTED}
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    (FIG_DIR / "summary_numbers.json").write_text(json.dumps(nums, indent=2, default=float), encoding="utf-8")
    print("  wrote", (FIG_DIR / "summary_numbers.json").relative_to(PKG))
    frags = fragments(D, nums)
    frags["n"] = brief_numbers(D, nums)
    (FIG_DIR / "fragments.json").write_text(json.dumps(frags, indent=1), encoding="utf-8")
    print("  wrote", (FIG_DIR / "fragments.json").relative_to(PKG))


if __name__ == "__main__":
    main()
