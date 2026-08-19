"""
09_02_nest_meso_in_good.py

Two-tier nest: keep WECC BA nodes (generation + base load), add California
delivery nodes (SUB_* substations by default, or aggregated MESO_* hubs)
that carry EV Load assets, linked to parent BA via interface Transmission and
to each other via meso corridors.

Writes:
  data/meso/wec_ca_meso_graph.json
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
WEC_PATH = REPO / "Examples" / "WEC_modified.json"


def _empty_profiles(n: int = 8760) -> dict:
    return {}


def make_ev_load_asset(hub_id: str, profile_kW: np.ndarray) -> dict:
    """GOOD Load asset: profile is fraction of installed_capacity; capacity in W."""
    profile_W = np.asarray(profile_kW, dtype=float) * 1000.0  # kW → W
    peak = float(profile_W.max()) if profile_W.size else 0.0
    cap = max(peak, 1.0)
    profile = (profile_W / cap).tolist()
    return {
        "_class": "Load",
        "type": "load",
        "name": f"ev_load_{hub_id}",
        "installed_capacity": -cap,  # loads often negative capacity in GOOD
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


def build_nested_graph(
    base: dict,
    hubs: pd.DataFrame,
    edges: pd.DataFrame,
    interfaces: pd.DataFrame,
    meso_hourly_kW: np.ndarray,
    hub_ids: list[str],
    include_bess: bool,
    capacity_scale_meso: float,
    capacity_scale_interface: float,
) -> dict:
    g = deepcopy(base)
    existing_ids = {n["id"] for n in g["nodes"]}
    hub_index = {h: i for i, h in enumerate(hub_ids)}

    # add meso nodes
    for _, row in hubs.iterrows():
        hid = row["hub_id"]
        if hid in existing_ids:
            continue
        i = hub_index[hid]
        profile = meso_hourly_kW[i]
        assets = {f"ev_load_{hid}": make_ev_load_asset(hid, profile)}
        if include_bess:
            # allow up to 50% of peak EV as BESS power
            peak_W = float(profile.max()) * 1000.0
            assets[f"bess_{hid}"] = make_store_asset(hid, max(peak_W * 0.5, 1e6))
        g["nodes"].append(
            {
                "id": hid,
                "_class": "Region",
                "assets": assets,
                "profiles": _empty_profiles(),
            }
        )
        existing_ids.add(hid)

    # hub–hub edges (bidirectional)
    for _, e in edges.iterrows():
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

    # BA ↔ hub interfaces (bidirectional)
    for _, e in interfaces.iterrows():
        hid = e["hub_id"]
        ba = e["parent_ba"]
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


def main() -> None:
    print(f"Loading base graph {WEC_PATH}")
    with open(WEC_PATH, encoding="utf-8") as fh:
        base = json.load(fh)

    hubs = pd.read_csv(C.MESO_DIR / "meso_nodes.csv")
    edges = pd.read_csv(C.MESO_DIR / "meso_edges.csv")
    interfaces = pd.read_csv(C.MESO_DIR / "meso_ba_interfaces.csv")

    # Use June week profile as the template embedded in the JSON;
    # 10_01 will overwrite profiles per season when solving.
    june_hubs = np.load(C.MESO_DIR / "seasonal" / "june" / "meso_hub_ids.npy")
    june_load = np.load(C.MESO_DIR / "seasonal" / "june" / "meso_hourly_kW.npy")
    hub_ids = [str(h) for h in june_hubs.tolist()]

    scales = {
        "S0": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": False},
        "S1": {"meso": 1.0, "interface": 1.0, "bess": False, "ev": True},
        "S2": {"meso": 1.0, "interface": 1.0, "bess": True, "ev": True},
        "S3": {"meso": 10.0, "interface": 10.0, "bess": False, "ev": True},
    }
    with open(C.MESO_DIR / "scenario_capacity_scales.json", "w", encoding="utf-8") as fh:
        json.dump(scales, fh, indent=2)

    # Default nested graph = S1 structure (EV, no BESS, tight caps) with June profiles
    nested = build_nested_graph(
        base,
        hubs,
        edges,
        interfaces,
        june_load,
        hub_ids,
        include_bess=False,
        capacity_scale_meso=1.0,
        capacity_scale_interface=1.0,
    )
    out_path = C.MESO_DIR / "wec_ca_meso_graph.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(nested, fh)
    print(
        f"Wrote {out_path} "
        f"(nodes={len(nested['nodes'])}, edges={len(nested['edges'])})"
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
