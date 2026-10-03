"""
08_18_size_bess_to_shift_shortfall.py

Storage sited to move shortfall between hours, and between substations, sized so
that neither power nor within-day energy binds. The run it feeds is an upper
bound on what storage can do about unserved load, not a proposed fleet.

Why this replaces sizing against curtailment
--------------------------------------------
08_14 put batteries where energy is spilled. No substation has both spill and
shortfall, and the spill sits in pockets whose own load is already met and whose
exit is the binding limit, so that fleet recovered 2.0 of 229.4 GWh.

A battery relieves shortfall from the other end. `Region-transformer` caps net
import over lines and a Store is an injection rather than an import, so a battery
at a short substation sits behind both its transformer and its corridors: it
charges through them in an hour with headroom and discharges to local load
without passing through either. 127 of the 128 short substations in S1 have such
hours.

Where a battery can help, and where it cannot
---------------------------------------------
Away from the short substation it helps only if its discharge can reach that
substation in the short hour, which means it must be on the LOAD side of whatever
limit binds. From the solved S1 flows an arc has slack in an hour if it is below
its rating; the nodes with a directed path of slack arcs into a short node are
its *pocket* for that hour.

* Transformer binds (34.35 GWh): nothing arriving over lines can help. At the
  node only.
* Every inbound line saturated (6.25 GWh): the pocket is empty. At the node only.
* A limit further out binds (49.49 GWh): the short node sits in a pocket of about
  ten other substations, any of which could deliver to it.

A battery on the sending side of a full line can absorb surplus but cannot push
more through it, so it is not a site here.

Sizing rule
-----------
Each short substation's *need* is P = its peak hourly shortfall and E = its
largest single-day shortfall energy, so duration E/P runs up to 24 h. A battery of
that size can cover the node's worst hour and its worst day if the network has
the spare capacity to charge it; more would only carry energy across days.

That matters on this horizon. State of charge is continuous and cyclic over all
672 hours, so it carries across the seams between the four concatenated seasonal
weeks. A battery with unlimited energy would move energy from the March week to
the December week. Capping energy at one day's need keeps any carry across a seam
to what would cross an ordinary midnight.

Two fleets, equal in total power and energy, differing only in location:

  node    every short substation at 2x its need
  pocket  every short substation at 1x its need, and for each group of
          path-bound short nodes a further 1x placed at the non-short
          substations in their pockets, weighted by the shortfall each could
          have reached. Short substations outside any pocket keep 2x at the node.

`pocket` is the upper bound on shifting between hours and between stations.
`pocket - node` isolates what location adds, with the amount of storage held
fixed. Sizing the comparison any other way confounds "storage elsewhere helps"
with "more storage helps".

Writes data/meso/bess_shift_pocket.csv and data/meso/bess_shift_node.csv
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

TOL = 0.99            # an arc or transformer at or above this share of its rating is binding
MIN_SITE_MW = 0.05    # neighbour allocations below this are dust; their share is redistributed


def _transformer_ratings_mw() -> dict[str, float]:
    spec = importlib.util.spec_from_file_location("nest", PKG / "09_02_nest_meso_in_good.py")
    nest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nest)
    return {h: w / 1e6 for h, w in nest._load_transformer_ratings().items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="four_week_WECC_SCE+WEC_CALN+WEC_SDGE")
    ap.add_argument("--scenario", default="S1")
    args = ap.parse_args()

    run = C.ASTR_RESULTS_DIR / args.tag / args.scenario
    sol_path = run / "solution.json"
    if not sol_path.is_file():
        raise SystemExit(f"{sol_path} missing. Re-run {args.scenario} with --save-json; "
                         f"the hourly flows are what locate the binding limits.")
    sol = json.load(open(sol_path, encoding="utf-8"))
    caps = pd.read_csv(run / "line_flows_summary.csv").set_index("line")["capacity_MW"].to_dict()
    t_mw = _transformer_ratings_mw()

    names = list(sol["nodes"])
    ix = {n: i for i, n in enumerate(names)}
    n_nodes = len(names)
    src, tgt, cap, flows, recv = [], [], [], [], []
    for e in sol["edges"]:
        for handle, ln in (e.get("lines") or {}).items():
            f = np.asarray(ln.get("flow") or [], dtype=float)
            if not f.size or e["source"] not in ix or e["target"] not in ix:
                continue
            src.append(ix[e["source"]])
            tgt.append(ix[e["target"]])
            cap.append(float(caps.get(handle, 0.0)))
            flows.append(f)
            recv.append(np.asarray(ln.get("received") or f, dtype=float))
    src, tgt, cap = np.array(src), np.array(tgt), np.array(cap)
    F, R = np.vstack(flows), np.vstack(recv)
    n_arcs, H = F.shape
    slack = (cap[:, None] > 0) & (F < TOL * cap[:, None])

    sf = np.zeros((n_nodes, H))
    for n, d in sol["nodes"].items():
        s = np.asarray(d.get("shortfall") or [], dtype=float)
        if s.size == H:
            sf[ix[n]] = s
    is_sub = np.array([n.startswith("SUB_") for n in names])
    ni = np.zeros((n_nodes, H))
    np.add.at(ni, tgt, R)
    np.subtract.at(ni, src, F)
    T = np.array([t_mw.get(n, np.inf) for n in names])
    t_bind = np.isfinite(T)[:, None] & (ni >= TOL * T[:, None])
    short = (sf > 1e-6) & is_sub[:, None]
    short_nodes = np.flatnonzero(short.any(axis=1))

    in_arcs = defaultdict(list)
    for a in range(n_arcs):
        in_arcs[tgt[a]].append(a)

    # Pockets: for each short node-hour whose transformer has slack, every node
    # with a directed path of slack arcs into it.
    reach = defaultdict(Counter)   # short node -> {site: MWh of its shortfall the site could have reached}
    linked = defaultdict(set)      # short node -> other short nodes in its pockets
    for t in range(H):
        ok = slack[:, t]
        for n in np.flatnonzero(short[:, t] & ~t_bind[:, t]):
            seen, stack = {n}, [n]
            while stack:
                v = stack.pop()
                for a in in_arcs.get(v, ()):
                    if ok[a] and src[a] not in seen:
                        seen.add(src[a])
                        stack.append(src[a])
            for m in seen:
                if m != n and is_sub[m]:
                    reach[n][m] += sf[n, t]
                    if short[m, t]:
                        linked[n].add(m)

    # Need at each short substation.
    days = H // 24
    peak = sf.max(axis=1)
    day_e = sf[:, :days * 24].reshape(n_nodes, days, 24).sum(axis=2).max(axis=1)
    need = {int(n): (float(peak[n]), float(day_e[n])) for n in short_nodes if peak[n] >= 1e-3}

    # Groups of path-bound short nodes that share a pocket.
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for n in sorted(reach):
        find(n)
        for m in sorted(linked[n]):
            parent[find(n)] = find(m)
    groups = defaultdict(list)
    for n in sorted(parent):
        if n in need:
            groups[find(n)].append(n)

    is_short = set(need)
    pocket_fleet = defaultdict(lambda: [0.0, 0.0])   # site -> [MW, MWh]
    role = {}
    grouped = set()
    n_neighbour_groups = 0
    for g, members in sorted(groups.items()):
        p_tot = sum(need[n][0] for n in members)
        e_tot = sum(need[n][1] for n in members)
        w = Counter()
        for n in members:
            for m, v in reach[n].items():
                if m not in is_short:
                    w[m] += v
        # drop dust, then renormalise
        tot_w = sum(w.values())
        w = {m: v for m, v in w.items() if tot_w > 0 and p_tot * v / tot_w >= MIN_SITE_MW}
        tot_w = sum(w.values())
        for n in members:
            mult = 1.0 if tot_w > 0 else 2.0
            pocket_fleet[n][0] += mult * need[n][0]
            pocket_fleet[n][1] += mult * need[n][1]
            role[n] = "short node"
            grouped.add(n)
        if tot_w > 0:
            n_neighbour_groups += 1
            for m in sorted(w):
                pocket_fleet[m][0] += p_tot * w[m] / tot_w
                pocket_fleet[m][1] += e_tot * w[m] / tot_w
                role.setdefault(m, "pocket neighbour")
    for n in sorted(is_short - grouped):
        pocket_fleet[n][0] += 2.0 * need[n][0]
        pocket_fleet[n][1] += 2.0 * need[n][1]
        role[n] = "short node, no pocket"

    node_fleet = {n: [2.0 * need[n][0], 2.0 * need[n][1]] for n in sorted(is_short)}

    def frame(fleet: dict, roles: dict | None) -> pd.DataFrame:
        rows = []
        for m in sorted(fleet, key=lambda k: names[k]):
            p, e = fleet[m]
            if p <= 0:
                continue
            rows.append({
                "node": names[m],
                "substation_id": names[m].replace("SUB_", "", 1),
                "power_MW": p,
                "duration_h": e / p,
                "energy_MWh": e,
                "capex_capacity_W": p * 1e6,
                "role": (roles or {}).get(m, "short node"),
                "s1_shortfall_MWh": float(sf[m].sum()),
                "s1_peak_shortfall_MW": float(peak[m]),
            })
        return pd.DataFrame(rows)

    pocket = frame(pocket_fleet, role)
    node = frame(node_fleet, None)
    C.ensure_dir(C.MESO_DIR)
    pocket.to_csv(C.MESO_DIR / "bess_shift_pocket.csv", index=False)
    node.to_csv(C.MESO_DIR / "bess_shift_node.csv", index=False)

    tb = (sf * (short & t_bind)).sum() / 1e3
    print(f"{args.scenario} substation shortfall: {sf[is_sub].sum() / 1e3:,.2f} GWh on {len(short_nodes)} nodes")
    print(f"  transformer binds in the short hour: {tb:,.2f} GWh (a battery at the node only)")
    print(f"  short nodes with need >= 1 kW: {len(need)}; in a shared pocket: {len(grouped)} "
          f"across {len(groups)} groups, {n_neighbour_groups} of which have non-short neighbours")
    print(f"  need: {sum(v[0] for v in need.values()):,.0f} MW, {sum(v[1] for v in need.values()):,.0f} MWh; "
          f"duration median {np.median([v[1] / v[0] for v in need.values()]):.1f} h, "
          f"max {max(v[1] / v[0] for v in need.values()):.1f} h")
    print()
    for label, df in (("pocket", pocket), ("node", node)):
        print(f"  fleet '{label}': {len(df):4d} sites, {df.power_MW.sum():8,.0f} MW, {df.energy_MWh.sum():9,.0f} MWh"
              f"   (mean duration {df.energy_MWh.sum() / df.power_MW.sum():.1f} h)")
    print()
    print(pocket.groupby("role").agg(sites=("node", "count"), MW=("power_MW", "sum"),
                                     MWh=("energy_MWh", "sum")).round(0).to_string())
    print(f"\n  wrote {C.MESO_DIR / 'bess_shift_pocket.csv'}")
    print(f"  wrote {C.MESO_DIR / 'bess_shift_node.csv'}")


if __name__ == "__main__":
    main()
