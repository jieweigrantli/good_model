"""
09_02_nest_meso_in_good.py

Nest the unclustered CA substation graph inside the WECC BA GOOD model.

Tier 1: non-CA balancing areas unchanged.
Tier 2: SUB_* nodes carry mapped CA generators, disaggregated base load, EV load,
        and (optionally) endogenous BESS. CA BA nodes keep optional renewable CAPEX
        and WECC interties; plant-level CA assets with coordinates are moved onto
        substations. Pipe-flow / NTC constraints on substation corridors.

Writes:
  data/meso/wecc_ca_nested_graph.json
  data/meso/scenario_capacity_scales.json
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

import common as C

REPO = C.REPO_ROOT


def make_ev_load_asset(hub_id: str, profile_kW: np.ndarray) -> dict:
    profile_W = np.asarray(profile_kW, dtype=float) * 1000.0
    peak = float(profile_W.max()) if profile_W.size else 0.0
    cap = max(peak, 1.0)
    profile = (profile_W / cap).tolist()
    return {
        "_class": "Load",
        "type": "load",
        "name": f"ev_load_{hub_id}",
        "installed_capacity": -cap,
        "profile": profile,
        "dispatchable": False,
        "extensible": False,
        "capex_capacity": 0,
        "capex_cost": 0,
    }


def make_base_load_asset(hub_id: str, profile_W: np.ndarray) -> dict:
    profile_W = np.asarray(profile_W, dtype=float)
    peak = float(np.abs(profile_W).max()) if profile_W.size else 0.0
    cap = max(peak, 1.0)
    profile = (profile_W / cap).tolist()
    return {
        "_class": "Load",
        "type": "load",
        "name": f"base_load_{hub_id}",
        "installed_capacity": -cap,
        "profile": profile,
        "dispatchable": False,
        "extensible": False,
        "capex_capacity": 0,
        "capex_cost": 0,
    }


def make_store_asset(hub_id: str, power_W: float, duration_h: float = 4.0) -> dict:
    return {
        "_class": "Store",
        "type": "battery",
        "fuel": "battery",
        "name": f"bess_{hub_id}",
        "installed_capacity": 0.0,
        "capex_capacity": power_W,
        "capex_cost": 2.1,
        "storage_duration": duration_h * 3600.0,
        "efficiency": 0.9,
        "dispatchable": True,
        "extensible": True,
    }


def make_transmission_line(source: str, target: str, capacity_W: float, handle: str) -> dict:
    return {
        "source": source,
        "target": target,
        "type": "line",
        "_class": "Transmission",
        "installed_capacity": float(capacity_W),
        "operating_cost": 0.0,
        "efficiency": 0.97,
        "dispatchable": True,
        "extensible": False,
        "capex_limit": 0.0,
        "capex_capacity": 0.0,
        "capex_cost": 0.0,
        "name": handle,
    }


def _load_network() -> dict:
    C.require_file(C.CA_NETWORK_JSON, hint="Run 09_01_build_ca_meso_grid.py first.")
    with open(C.CA_NETWORK_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _strip_mapped_ca_assets(base: dict, generators: list[dict]) -> dict:
    """Remove plant-level CA assets that were snapped to substations; drop BA base_load."""
    mapped = {(g["parent_ba"], g["handle"]) for g in generators if g.get("hub_id")}
    g = deepcopy(base)
    for node in g["nodes"]:
        nid = node.get("id")
        if nid not in C.CALIFORNIA_REGIONS:
            continue
        assets = node.get("assets") or {}
        keep = {}
        for handle, asset in assets.items():
            if (nid, handle) in mapped:
                continue
            if asset.get("_class") == "Load" and str(asset.get("type", "")).lower() == "load":
                continue
            keep[handle] = asset
        node["assets"] = keep
    return g


def build_nested_graph(
    base: dict,
    network: dict,
    hub_ids: list[str],
    ev_kW: np.ndarray,
    total_kW: np.ndarray | None,
    include_bess: bool,
    include_ev: bool,
    capacity_scale_meso: float,
    capacity_scale_interface: float,
    bess_top_n: int = 0,
) -> dict:
    generators = network.get("generators") or []
    g = _strip_mapped_ca_assets(base, generators)
    existing_ids = {n["id"] for n in g["nodes"]}
    hub_index = {h: i for i, h in enumerate(hub_ids)}

    gen_by_hub: dict[str, list] = {}
    for rec in generators:
        hid = rec.get("hub_id")
        if not hid:
            continue
        gen_by_hub.setdefault(hid, []).append(rec)

    # Copy original CA assets so we can re-attach mapped plants onto substations
    orig_assets = {}
    orig_profiles = {}
    for node in base["nodes"]:
        if node.get("id") in C.CALIFORNIA_REGIONS:
            orig_assets[node["id"]] = node.get("assets") or {}
            orig_profiles[node["id"]] = node.get("profiles") or {}

    peaks = {
        hid: float(np.asarray(ev_kW[hub_index[hid]]).max()) if hid in hub_index else 0.0
        for hid in hub_ids
    }
    if bess_top_n and bess_top_n > 0:
        bess_hubs = set(sorted(peaks, key=peaks.get, reverse=True)[:bess_top_n])
    else:
        bess_hubs = {hid for hid, p in peaks.items() if p > 0}

    bess_lookup = {b["hub_id"]: b for b in (network.get("bess_candidates") or [])}

    for rec in network.get("nodes") or []:
        hid = rec["hub_id"]
        if hid in existing_ids:
            continue
        i = hub_index.get(hid)
        assets = {}
        profiles = {}
        if i is not None:
            ev_prof = ev_kW[i] if include_ev else np.zeros(ev_kW.shape[1], dtype=float)
            if total_kW is not None:
                ev_for_base = ev_kW[i] if include_ev else np.zeros_like(total_kW[i])
                base_kw = np.clip(np.asarray(total_kW[i], dtype=float) - np.asarray(ev_for_base, dtype=float), 0, None)
            else:
                base_kw = np.zeros_like(ev_prof)
            assets[f"base_load_{hid}"] = make_base_load_asset(hid, base_kw * 1000.0)
            if include_ev:
                assets[f"ev_load_{hid}"] = make_ev_load_asset(hid, ev_prof)
            if include_bess and hid in bess_hubs:
                spec = bess_lookup.get(hid, {})
                peak_w = max(peaks.get(hid, 0.0) * 1000.0 * 0.5, 1e6)
                assets[f"bess_{hid}"] = make_store_asset(
                    hid, float(spec.get("capex_capacity_W") or peak_w), float(spec.get("duration_h") or 4.0)
                )
        ba = rec.get("parent_ba")
        profiles.update(orig_profiles.get(ba, {}))
        for grec in gen_by_hub.get(hid, []):
            src = orig_assets.get(grec["parent_ba"], {}).get(grec["handle"])
            if not src:
                continue
            asset = deepcopy(src)
            handle = f"{grec['handle']}__{hid}"
            assets[handle] = asset
        g["nodes"].append(
            {
                "id": hid,
                "_class": "Region",
                "assets": assets,
                "profiles": profiles,
            }
        )
        existing_ids.add(hid)

    for e in network.get("edges") or []:
        cap = float(e["installed_capacity_W"]) * capacity_scale_meso
        s, t = e["source"], e["target"]
        for src, tgt, tag in ((s, t, "fwd"), (t, s, "rev")):
            handle = f"meso_{src}_{tgt}_{tag}"
            g["edges"].append(
                {
                    "id": handle,
                    "_class": "Link",
                    "source": src,
                    "target": tgt,
                    "lines": {handle: make_transmission_line(src, tgt, cap, handle)},
                }
            )

    for e in network.get("ba_interfaces") or []:
        hid, ba = e["hub_id"], e["parent_ba"]
        cap = float(e["interface_capacity_W"]) * capacity_scale_interface
        for src, tgt, tag in ((ba, hid, "to_hub"), (hid, ba, "to_ba")):
            handle = f"iface_{src}_{tgt}_{tag}"
            g["edges"].append(
                {
                    "id": handle,
                    "_class": "Link",
                    "source": src,
                    "target": tgt,
                    "lines": {handle: make_transmission_line(src, tgt, cap, handle)},
                }
            )

    return g


def _default_profiles():
    june_ids = C.MESO_DIR / "seasonal" / "june" / "meso_hub_ids.npy"
    june_ev = C.MESO_DIR / "seasonal" / "june" / "meso_hourly_kW.npy"
    june_tot = C.MESO_DIR / "seasonal" / "june" / "meso_hourly_total_kW.npy"
    if not june_ids.is_file():
        raise FileNotFoundError("Missing seasonal meso loads; run 08_03 and 09_01 first.")
    hub_ids = [str(h) for h in np.load(june_ids, allow_pickle=True).tolist()]
    ev = np.load(june_ev)
    tot = np.load(june_tot) if june_tot.is_file() else None
    return hub_ids, ev, tot


def main() -> None:
    wec_path = C.resolve_wec_json()
    print(f"Loading base graph {wec_path}")
    with open(wec_path, encoding="utf-8") as fh:
        base = json.load(fh)

    network = _load_network()
    hub_ids, ev, tot = _default_profiles()

    scales = {
        "S0": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": False},
        "S1": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": True},
        "S2": {"meso": 1.0, "interface": 1.0, "bess": True, "ev": True},
        "S3": {"meso": 10.0, "interface": 10.0, "bess": False, "ev": True},
    }
    C.ensure_dir(C.MESO_DIR)
    with open(C.SCENARIO_SCALES_JSON, "w", encoding="utf-8") as fh:
        json.dump(scales, fh, indent=2)

    nested = build_nested_graph(
        base,
        network,
        hub_ids,
        ev,
        tot,
        include_bess=False,
        include_ev=True,
        capacity_scale_meso=1.0,
        capacity_scale_interface=1.0,
    )
    with open(C.NESTED_GRAPH_JSON, "w", encoding="utf-8") as fh:
        json.dump(nested, fh)
    print(
        f"Wrote {C.NESTED_GRAPH_JSON} "
        f"(nodes={len(nested['nodes'])}, edges={len(nested['edges'])})"
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
