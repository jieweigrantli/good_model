"""
10_05_plot_network_map.py

Map the transmission corridors the model actually solves on, for visual
inspection. Draws the reconstructed corridor graph -- not the raw source
geometry -- so what is plotted is exactly what the LP sees: one straight
edge per substation-to-substation corridor.

Outputs (under docs/figures/):
  ca_model_network.png   statewide overview, corridors coloured by voltage
  ca_model_network.html  self-contained interactive map (zoom / pan / hover)
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

import common as C

OUT_DIR = C.REPO_ROOT / "Distribution-Grid-EV-CA" / "docs" / "figures"

# Voltage classes, coarse enough to read at statewide zoom.
KV_BANDS = [
    (500, 1e9, "500 kV", "#d62728", 3.4),
    (230, 500, "220-345 kV", "#ff7f0e", 2.2),
    (100, 230, "100-161 kV", "#2ca02c", 1.3),
    (50, 100, "60-92 kV (sub-transmission)", "#1f77b4", 0.7),
    (0, 50, "<50 kV", "#9467bd", 0.45),
]


def kv_width(kv: float) -> float:
    """Line width as a continuous function of voltage.

    Banded widths hide the ordering inside a band -- a 230 kV and a 345 kV
    corridor drew identically. Scaling with sqrt(kV) keeps 500 kV visually
    dominant without making 60 kV invisible.
    """
    return float(np.clip(np.sqrt(max(kv, 10.0)) / 6.0, 0.25, 4.2))


# Which quantity sets arc width. Capacity is the default because it is what the
# LP actually constrains: a corridor binds on its MW limit, not on its voltage.
# Voltage remains the colour, so both are legible at once -- and where the two
# disagree (a 230 kV corridor carrying only 400 MW because one circuit was
# resolved, against another carrying 2,400 MW) the map shows it directly.
WIDTH_BY = "capacity"


def mw_width(mw: float) -> float:
    """Arc width from corridor capacity in MW.

    sqrt, like the voltage scale it replaces, so a 4,500 MW 500 kV corridor is
    visually dominant without a 60 MW sub-transmission tie vanishing. Capacities
    in this network span 14 MW to 4,500 MW.
    """
    return float(np.clip(np.sqrt(max(mw, 5.0)) / 11.0, 0.25, 4.2))


def arc_width(kv: float, mw: float) -> float:
    return mw_width(mw) if WIDTH_BY == "capacity" else kv_width(kv)


def node_size(rating_W: float, peak_W: float) -> float:
    """Marker area from transformer rating, falling back to assigned peak."""
    mva = (rating_W if rating_W and rating_W > 0 else (peak_W or 0.0)) / 1e6
    return float(np.clip(1.0 + 2.6 * np.sqrt(max(mva, 0.0)), 1.0, 120.0))


def _band(kv: float):
    for lo, hi, label, colour, width in KV_BANDS:
        if lo <= kv < hi:
            return label, colour, width
    return KV_BANDS[-1][2], KV_BANDS[-1][3], KV_BANDS[-1][4]


def load_model() -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    hubs = gpd.read_file(C.MESO_DIR / "meso_hubs.gpkg")
    edges = pd.read_csv(C.MESO_DIR / "meso_edges.csv")
    ratings = C.MESO_DIR / "substation_ratings.csv"
    if ratings.is_file():
        r = pd.read_csv(ratings)[["hub_id", "rating_W", "rating_source"]]
        hubs = hubs.merge(r, on="hub_id", how="left")
    for col, default in (("rating_W", 0.0), ("rating_source", "unknown")):
        if col not in hubs.columns:
            hubs[col] = default
    hubs["rating_W"] = pd.to_numeric(hubs["rating_W"], errors="coerce").fillna(0.0)
    if "provenance" not in edges.columns:
        edges["provenance"] = "inferred"
    if "rated_kv" not in edges.columns:
        edges["rated_kv"] = np.nan
    edges["rated_kv"] = pd.to_numeric(edges["rated_kv"], errors="coerce").fillna(60.0)

    g = nx.Graph()
    g.add_nodes_from(hubs["hub_id"])
    g.add_edges_from(zip(edges["source"], edges["target"]))
    comps = sorted(nx.connected_components(g), key=len, reverse=True)
    main = comps[0] if comps else set()
    size = {n: len(c) for c in comps for n in c}
    hubs["in_main"] = hubs["hub_id"].isin(main)
    hubs["comp_size"] = hubs["hub_id"].map(size).fillna(1).astype(int)
    return hubs, edges


def ca_outline(crs) -> gpd.GeoDataFrame | None:
    """State outline dissolved from the census tract layer, if present."""
    src = C.DATA_DIR / "tl_2021_06_tract" / "tl_2021_06_tract.shp"
    if not src.is_file():
        return None
    tr = gpd.read_file(src, columns=["GEOID"])
    return gpd.GeoDataFrame(geometry=[tr.union_all()], crs=tr.crs).to_crs(crs)


def segments(hubs: gpd.GeoDataFrame, edges: pd.DataFrame):
    xy = {r.hub_id: (r.geometry.x, r.geometry.y) for r in hubs.itertuples()}
    for r in edges.itertuples():
        a, b = xy.get(r.source), xy.get(r.target)
        if a and b:
            yield a, b, float(r.rated_kv), float(r.installed_capacity_W)


def plot_png(hubs, edges, outline, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(19, 13))

    for ax in axes:
        if outline is not None:
            outline.boundary.plot(ax=ax, color="0.55", linewidth=0.7, zorder=0)
        ax.set_aspect("equal")
        ax.set_axis_off()

    # --- left: corridors by voltage -------------------------------------
    ax = axes[0]
    from matplotlib.collections import LineCollection

    rows = sorted(segments(hubs, edges), key=lambda r: r[2])  # draw low kV first
    ax.add_collection(
        LineCollection(
            [[r[0], r[1]] for r in rows],
            colors=[_band(r[2])[1] for r in rows],
            linewidths=[arc_width(r[2], r[3] / 1e6) for r in rows],
            alpha=0.85,
            zorder=2,
        )
    )
    sizes = [node_size(w, p) for w, p in zip(hubs["rating_W"], hubs["total_peak_W"])]
    ax.scatter(hubs.geometry.x, hubs.geometry.y, s=sizes, c="0.18",
               alpha=0.55, linewidths=0, zorder=3)
    ax.autoscale_view()
    ax.set_title(
        f"Model network: arc width by {'corridor capacity (MW)' if WIDTH_BY == 'capacity' else 'voltage'}, "
        f"colour by voltage class, node size by substation capacity\n"
        f"{len(edges):,} corridors between {len(hubs):,} substation nodes",
        fontsize=13,
    )
    handles = [Line2D([], [], color=c, lw=w, label=l)
               for _lo, _hi, l, c, w in KV_BANDS]
    handles.append(Line2D([], [], color="none", label=" "))
    for mva in (25, 100, 400):
        handles.append(
            Line2D([], [], marker="o", linestyle="none", color="0.18",
                   markersize=float(np.sqrt(node_size(mva * 1e6, 0))),
                   label=f"{mva} MVA substation")
        )
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=9)

    # --- right: connectivity --------------------------------------------
    ax = axes[1]
    ax.add_collection(
        LineCollection(
            [[a, b] for a, b, _kv, _c in segments(hubs, edges)],
            colors="0.75",
            linewidths=0.4,
            zorder=2,
        )
    )
    # Now that synthetic feeds connect everything, the useful second view is
    # not "what is connected" but "what is the evidence for each connection".
    PROV = {
        "confirmed": ("#1a9850", "confirmed by a published source"),
        "asserted": ("#4575b4", "asserted by a published source"),
        "inferred": ("#bdbdbd", "inferred from geometry"),
        "synthetic_feed": ("#d73027", "synthetic radial feed (interpolated)"),
    }
    xy = {r.hub_id: (r.geometry.x, r.geometry.y) for r in hubs.itertuples()}
    for prov in ("inferred", "asserted", "confirmed", "synthetic_feed"):
        sel = edges[edges["provenance"] == prov]
        if not len(sel):
            continue
        segs, wids = [], []
        for r in sel.itertuples():
            a, b = xy.get(r.source), xy.get(r.target)
            if a and b:
                segs.append([a, b])
                wids.append(arc_width(float(r.rated_kv), float(r.installed_capacity_W) / 1e6))
        ax.add_collection(LineCollection(
            segs, colors=PROV[prov][0], linewidths=wids,
            alpha=0.5 if prov == "inferred" else 0.9,
            zorder=2 if prov == "inferred" else 4))

    derived = hubs[hubs["rating_source"] != "published"]
    ax.scatter(derived.geometry.x, derived.geometry.y, s=2.0, c="#d73027",
               alpha=0.35, linewidths=0, zorder=5)
    ax.autoscale_view()
    backed = edges["provenance"].isin(["asserted", "confirmed"]).mean()
    n_derived = int((hubs["rating_source"] != "published").sum())
    ax.set_title(
        f"Evidence behind each corridor\n"
        f"{100 * backed:.0f}% of corridors backed by a published source; "
        f"{n_derived:,} nodes on a derived rating",
        fontsize=13,
    )
    ax.legend(
        handles=[Line2D([], [], color=c, lw=2.2, label=l) for c, l in PROV.values()]
        + [Line2D([], [], marker="o", linestyle="none", color="#d73027",
                  markersize=4, alpha=0.5, label="derived transformer rating")],
        loc="upper right", frameon=False, fontsize=9)

    fig.tight_layout()
    fig.savefig(path, dpi=165, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_html(hubs, edges, outline, path: Path) -> None:
    """Self-contained interactive SVG map: no tiles, no external requests."""
    ll = hubs.to_crs(4326)
    xy = {r.hub_id: (r.geometry.x, r.geometry.y) for r in ll.itertuples()}

    segs = []
    for r in edges.itertuples():
        a, b = xy.get(r.source), xy.get(r.target)
        if not (a and b):
            continue
        kv = float(r.rated_kv)
        label, colour, width = _band(kv)
        segs.append({
            "x1": round(a[0], 5), "y1": round(a[1], 5),
            "x2": round(b[0], 5), "y2": round(b[1], 5),
            "kv": kv, "c": colour,
            "w": round(arc_width(kv, float(r.installed_capacity_W) / 1e6), 2),
            "b": label,
            "pv": str(getattr(r, "provenance", "inferred")),
            "mw": round(float(r.installed_capacity_W) / 1e6, 1),
            "s": r.source.replace("SUB_", ""), "t": r.target.replace("SUB_", ""),
        })

    nodes = [
        {"x": round(xy[r.hub_id][0], 5), "y": round(xy[r.hub_id][1], 5),
         "n": str(r.substation_name)[:40], "ba": str(r.parent_ba),
         "kv": None if pd.isna(r.max_kv) else float(r.max_kv),
         "mw": round(float(r.total_peak_W) / 1e6, 1),
         "mva": round(float(r.rating_W) / 1e6, 1),
         "rs": str(r.rating_source),
         "r": round(float(np.sqrt(node_size(float(r.rating_W), float(r.total_peak_W)))) / 2.4, 2),
         "m": bool(r.in_main), "cs": int(r.comp_size)}
        for r in ll.itertuples()
    ]

    outline_paths = []
    if outline is not None:
        geom = outline.to_crs(4326).geometry.iloc[0]
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            c = np.asarray(poly.exterior.coords)
            if len(c) > 4000:
                c = c[:: max(1, len(c) // 4000)]
            if poly.area > 1e-3:
                outline_paths.append([[round(x, 4), round(y, 4)] for x, y in c])

    payload = json.dumps({"segs": segs, "nodes": nodes, "outline": outline_paths},
                         separators=(",", ":"))
    bands = json.dumps([{"label": l, "colour": c} for _lo, _hi, l, c, _w in KV_BANDS])

    path.write_text(_HTML.replace("__DATA__", payload).replace("__BANDS__", bands),
                    encoding="utf-8")


_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>ASTR2026 model transmission network</title>
<style>
 :root{color-scheme:light}
 body{margin:0;font:13px system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f7f7f5;color:#1a1a1a}
 header{padding:10px 16px;border-bottom:1px solid #ddd;background:#fff}
 h1{margin:0 0 2px;font-size:15px}
 .sub{color:#666;font-size:12px}
 #wrap{display:flex;height:calc(100vh - 62px)}
 #map{flex:1;position:relative;overflow:hidden;background:#eef1f4;cursor:grab}
 #map.drag{cursor:grabbing}
 aside{width:260px;border-left:1px solid #ddd;background:#fff;padding:12px 14px;overflow:auto}
 .row{display:flex;align-items:center;gap:7px;margin:5px 0;font-size:12px}
 .sw{width:22px;height:3px;border-radius:2px}
 .k{color:#666}
 button{font:inherit;padding:4px 9px;border:1px solid #ccc;background:#fff;border-radius:5px;cursor:pointer}
 #tip{position:absolute;pointer-events:none;background:rgba(20,20,20,.9);color:#fff;padding:6px 8px;
      border-radius:5px;font-size:11.5px;line-height:1.45;display:none;max-width:250px;z-index:9}
 label{display:flex;align-items:center;gap:6px;margin:4px 0;font-size:12px}
 hr{border:0;border-top:1px solid #eee;margin:11px 0}
</style></head><body>
<header>
  <h1>ASTR2026 &mdash; transmission corridors used by the model</h1>
  <div class="sub">Each line is one substation-to-substation corridor as the LP sees it. Scroll to zoom, drag to pan, hover for detail.</div>
</header>
<div id="wrap">
  <div id="map"><canvas id="cv"></canvas><div id="tip"></div></div>
  <aside>
    <b>Voltage</b><div id="legend"></div>
    <hr>
    <b>Display</b>
    <label><input type="checkbox" id="showNodes" checked> substations</label>
    <label><input type="checkbox" id="showIso" checked> highlight isolated</label>
    <label><input type="checkbox" id="byCap"> width &prop; capacity</label>
    <hr>
    <div id="stats" class="k"></div>
    <hr>
    <button id="reset">Reset view</button>
  </aside>
</div>
<script>
const D = __DATA__, BANDS = __BANDS__;
const cv=document.getElementById('cv'), ctx=cv.getContext('2d'), map=document.getElementById('map'), tip=document.getElementById('tip');
let view={s:1,x:0,y:0}, base=null;

const lons=D.nodes.map(n=>n.x), lats=D.nodes.map(n=>n.y);
const bb={x0:Math.min(...lons),x1:Math.max(...lons),y0:Math.min(...lats),y1:Math.max(...lats)};

function fit(){
  const w=map.clientWidth,h=map.clientHeight;
  cv.width=w*devicePixelRatio; cv.height=h*devicePixelRatio;
  cv.style.width=w+'px'; cv.style.height=h+'px';
  const mx=(bb.x1-bb.x0)*0.04, my=(bb.y1-bb.y0)*0.04;
  const sx=w/((bb.x1-bb.x0)+2*mx), sy=h/((bb.y1-bb.y0)+2*my);
  base=Math.min(sx,sy);
  view={s:1,x:(w-(bb.x1-bb.x0)*base)/2,y:(h-(bb.y1-bb.y0)*base)/2};
  draw();
}
const px=n=>[(n-bb.x0)*base*view.s+view.x, (bb.y1-n)*0]; // placeholder
function PX(x,y){return [ (x-bb.x0)*base*view.s+view.x, (bb.y1-y)*base*view.s+view.y ];}

function draw(){
  const w=cv.width/devicePixelRatio,h=cv.height/devicePixelRatio;
  ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle='#eef1f4'; ctx.fillRect(0,0,w,h);

  ctx.lineWidth=1; ctx.strokeStyle='#b9c2cb'; ctx.fillStyle='#ffffff';
  for(const ring of D.outline){
    ctx.beginPath();
    for(let i=0;i<ring.length;i++){const p=PX(ring[i][0],ring[i][1]); i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]);}
    ctx.closePath(); ctx.fill(); ctx.stroke();
  }

  const byCap=document.getElementById('byCap').checked;
  const order=[...D.segs].sort((a,b)=>a.kv-b.kv);
  for(const s of order){
    const p=PX(s.x1,s.y1), q=PX(s.x2,s.y2);
    ctx.beginPath(); ctx.moveTo(p[0],p[1]); ctx.lineTo(q[0],q[1]);
    ctx.strokeStyle=s.c;
    ctx.lineWidth=(byCap? Math.max(0.35,Math.sqrt(s.mw)/12) : s.w)*Math.min(2.6,Math.sqrt(view.s));
    ctx.globalAlpha=0.88; ctx.stroke();
  }
  ctx.globalAlpha=1;

  if(document.getElementById('showNodes').checked){
    const rs=Math.max(0.35,0.7*Math.sqrt(view.s));
    for(const n of D.nodes){
      const p=PX(n.x,n.y);
      ctx.beginPath(); ctx.arc(p[0],p[1],Math.max(0.5,(n.r||1)*rs),0,6.284);
      ctx.fillStyle=n.m?'#33444f':'#f0a030'; ctx.fill();
    }
    if(document.getElementById('showIso').checked){
      ctx.strokeStyle='#d73027'; ctx.lineWidth=1.1;
      for(const n of D.nodes){ if(n.cs>1) continue;
        const p=PX(n.x,n.y),d=2.6*Math.min(2,Math.sqrt(view.s));
        ctx.beginPath(); ctx.moveTo(p[0]-d,p[1]-d); ctx.lineTo(p[0]+d,p[1]+d);
        ctx.moveTo(p[0]+d,p[1]-d); ctx.lineTo(p[0]-d,p[1]+d); ctx.stroke();
      }
    }
  }
}

let drag=null;
map.addEventListener('mousedown',e=>{drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y};map.classList.add('drag');});
addEventListener('mouseup',()=>{drag=null;map.classList.remove('drag');});
map.addEventListener('mousemove',e=>{
  if(drag){view.x=drag.vx+(e.clientX-drag.x);view.y=drag.vy+(e.clientY-drag.y);draw();tip.style.display='none';return;}
  const r=map.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  let best=null,bd=11;
  for(const n of D.nodes){const p=PX(n.x,n.y);const d=Math.hypot(p[0]-mx,p[1]-my);if(d<bd){bd=d;best=n;}}
  if(best){
    tip.style.display='block'; tip.style.left=(mx+13)+'px'; tip.style.top=(my+13)+'px';
    tip.innerHTML='<b>'+best.n+'</b><br>'+best.ba+(best.kv?' &middot; '+best.kv+' kV':'')+
      '<br>peak '+best.mw+' MW &middot; '+best.mva+' MVA ('+best.rs+')<br>'+(best.m?'main network':(best.cs>1?('sub-network of '+best.cs):'<span style="color:#ff9d9d">isolated</span>'));
  } else tip.style.display='none';
});
map.addEventListener('wheel',e=>{
  e.preventDefault();
  const r=map.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  const k=e.deltaY<0?1.18:1/1.18, ns=Math.min(120,Math.max(1,view.s*k)), f=ns/view.s;
  view.x=mx-(mx-view.x)*f; view.y=my-(my-view.y)*f; view.s=ns; draw();
},{passive:false});

document.getElementById('legend').innerHTML=BANDS.map(b=>
  '<div class="row"><div class="sw" style="background:'+b.colour+'"></div>'+b.label+'</div>').join('');
const nMain=D.nodes.filter(n=>n.m).length, pk=D.nodes.reduce((a,n)=>a+n.mw,0),
      pkMain=D.nodes.filter(n=>n.m).reduce((a,n)=>a+n.mw,0);
document.getElementById('stats').innerHTML=
  D.segs.length.toLocaleString()+' corridors<br>'+D.nodes.length.toLocaleString()+' substation nodes<br>'+
  nMain.toLocaleString()+' in main network ('+(100*nMain/D.nodes.length).toFixed(1)+'%)<br>'+
  (100*pkMain/pk).toFixed(1)+'% of peak demand connected<br>'+
  D.nodes.filter(n=>n.cs===1).length+' isolated nodes';
for(const id of ['showNodes','showIso','byCap']) document.getElementById(id).onchange=draw;
document.getElementById('reset').onclick=fit;
addEventListener('resize',fit); fit();
</script></body></html>
"""


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--width-by", choices=["capacity", "voltage"], default="capacity",
                    help="What arc thickness encodes. Colour is always voltage class.")
    ap.add_argument("--suffix", default="", help="Append to the output filenames.")
    args = ap.parse_args()
    global WIDTH_BY
    WIDTH_BY = args.width_by

    C.ensure_dir(OUT_DIR)
    hubs, edges = load_model()
    outline = ca_outline(hubs.crs)

    png = OUT_DIR / f"ca_model_network{args.suffix}.png"
    plot_png(hubs, edges, outline, png)
    print(f"wrote {png}")

    html = OUT_DIR / f"ca_model_network{args.suffix}.html"
    plot_html(hubs, edges, outline, html)
    print(f"wrote {html}")

    n_main = int(hubs["in_main"].sum())
    print(
        f"  {len(edges):,} corridors | {len(hubs):,} nodes | "
        f"{n_main:,} in main ({100 * n_main / len(hubs):.1f}%) | "
        f"{int((hubs['comp_size'] == 1).sum()):,} isolated"
    )


if __name__ == "__main__":
    main()
