"""
utils.py — Shared graph-manipulation and data-loading helpers.
"""

import os
import json
import numpy as np
import pandas as pd
from copy import deepcopy


# ---------------------------------------------------------------------------
# Profile slicing
# ---------------------------------------------------------------------------

def slice_graph_profiles(graph, start_hour=0, num_hours=168):
    """
    Return a deep-copy of *graph* with all profiles sliced to *num_hours*,
    starting at *start_hour*.  Uses wrap-around indexing so any start_hour
    works even when profiles are shorter than a full year.
    """
    graph = deepcopy(graph)

    def _slice_or_wrap(profile_data, start_hour, num_hours):
        arr = np.asarray(profile_data, dtype=float).flatten()
        if arr.size == 0:
            return np.zeros(num_hours, dtype=float)
        if start_hour >= 0 and (start_hour + num_hours) <= arr.size:
            return arr[start_hour:start_hour + num_hours]
        idx = (np.arange(num_hours) + int(start_hour)) % arr.size
        return arr[idx]

    for _, node_data in graph._node.items():
        profiles_dict = node_data.get('profiles', {})
        for profile_name, profile_data in list(profiles_dict.items()):
            if isinstance(profile_data, (list, np.ndarray)):
                profiles_dict[profile_name] = (
                    _slice_or_wrap(profile_data, start_hour, num_hours).tolist()
                )
        for _, asset_data in node_data.get('assets', {}).items():
            profile = asset_data.get('profile', None)
            if profile is None or isinstance(profile, str):
                continue
            if isinstance(profile, (list, np.ndarray)):
                asset_data['profile'] = (
                    _slice_or_wrap(profile, start_hour, num_hours).tolist()
                )
    return graph


# ---------------------------------------------------------------------------
# EV load injection
# ---------------------------------------------------------------------------

def add_ev_charging_load(
    graph,
    ev_charging_load,
    target_regions=None,
    start_hour=0,
    num_hours=168,
    california_regions=None,
):
    """
    Slice the graph profiles and inject *ev_charging_load* (W, 1-D array of
    length *num_hours*) proportionally into *target_regions*.

    If *target_regions* is None, defaults to *california_regions*.
    """
    graph = slice_graph_profiles(graph, start_hour, num_hours)

    if target_regions is None:
        if california_regions is None:
            california_regions = [
                'WEC_BANC', 'WEC_CALN', 'WEC_LADW', 'WEC_SDGE',
                'WECC_IID', 'WECC_SCE',
            ]
        target_regions = california_regions
    target_regions = set(target_regions)

    max_ev_load   = ev_charging_load.max()
    total_ev_gwh  = ev_charging_load.sum() / 1e9
    print(f"\nAdding EV charging load:")
    print(f"  Peak load:    {max_ev_load / 1e9:.2f} GW")
    print(f"  Total energy: {total_ev_gwh:.2f} GWh")
    print(f"  Target regions: {sorted(target_regions)}")

    # --- collect base-load capacities in target regions ---
    total_base_load = 0.0
    base_loads: dict = {}

    for node_name, node_data in graph._node.items():
        if node_name not in target_regions:
            continue
        for asset_name, asset_data in node_data.get('assets', {}).items():
            if asset_data.get('_class') == 'Load' and 'base_load' in asset_name.lower():
                cap = abs(asset_data.get('installed_capacity', 0))
                base_loads[node_name] = base_loads.get(node_name, 0.0) + cap
                total_base_load += cap

    if total_base_load == 0:
        print("  WARNING: No 'base_load' assets found; distributing to all Load assets.")
        for node_name, node_data in graph._node.items():
            if node_name not in target_regions:
                continue
            for asset_name, asset_data in node_data.get('assets', {}).items():
                if asset_data.get('_class') == 'Load':
                    cap = abs(asset_data.get('installed_capacity', 0))
                    base_loads[node_name] = base_loads.get(node_name, 0.0) + cap
                    total_base_load += cap

    print(f"  Total base-load capacity in target regions: {total_base_load / 1e9:.2f} GW")

    if total_base_load == 0:
        raise ValueError("No load assets found in target regions to attach EV load to.")

    # --- distribute EV load proportionally ---
    for node_name, node_data in graph._node.items():
        if node_name not in base_loads or total_base_load <= 0:
            continue

        proportion          = base_loads[node_name] / total_base_load
        ev_region_profile   = ev_charging_load * proportion

        target_found = False
        for asset_name, asset_data in node_data['assets'].items():
            is_base = 'base_load' in asset_name.lower()
            if asset_data.get('_class') != 'Load' or not is_base:
                continue

            existing_profile = asset_data.get('profile', None)
            if isinstance(existing_profile, str):
                existing_profile = node_data.get('profiles', {}).get(existing_profile)
            if existing_profile is None:
                continue

            existing_arr = np.array(existing_profile)
            if len(existing_arr) != len(ev_region_profile):
                n = min(len(existing_arr), len(ev_region_profile))
                existing_arr       = existing_arr[:n]
                ev_region_profile  = ev_region_profile[:n]

            existing_cap    = asset_data.get('installed_capacity', 0)
            existing_cap_abs = abs(existing_cap)
            existing_load   = existing_arr * existing_cap_abs
            new_load        = existing_load + ev_region_profile
            ev_peak_region  = ev_region_profile.max()
            new_cap_abs     = existing_cap_abs + ev_peak_region

            if new_cap_abs > 0:
                new_profile = new_load / new_cap_abs
            else:
                new_profile = existing_arr

            asset_data['profile']           = new_profile.tolist()
            asset_data['installed_capacity'] = (
                -new_cap_abs if existing_cap < 0 else new_cap_abs
            )
            target_found = True
            break

        if not target_found:
            print(f"  Warning: could not add EV load to node {node_name}")

    return graph


# ---------------------------------------------------------------------------
# Graph preparation (nuclear, storage, transmission, CAPEX)
# ---------------------------------------------------------------------------

def prepare_graph(graph, config):
    """Apply all in-memory graph modifications from *config*."""

    # Nuclear: constant baseload
    nuclear_count = 0
    for _, node_data in graph._node.items():
        for _, asset_data in node_data.get('assets', {}).items():
            if (asset_data.get('_class') == 'Producer'
                    and str(asset_data.get('fuel', '')).lower() == 'nuclear'):
                asset_data['profile']         = None
                asset_data['capacity_factor'] = 1
                asset_data['dispatchable']    = False
                nuclear_count += 1
    print(f"  Nuclear baseload: {nuclear_count} assets set to constant dispatch")

    # Storage duration
    store_count = 0
    for _, node_data in graph._node.items():
        for _, asset_data in node_data.get('assets', {}).items():
            if asset_data.get('_class') == 'Store':
                fuel = str(asset_data.get('fuel', '')).lower()
                if 'pump' in fuel or 'hydro' in fuel:
                    asset_data['storage_duration'] = (
                        config.PUMP_HYDRO_DURATION_HOURS * 3600
                    )
                else:
                    asset_data['storage_duration'] = (
                        config.BATTERY_DURATION_HOURS * 3600
                    )
                store_count += 1
    print(f"  Storage duration set for {store_count} Store assets "
          f"(battery={config.BATTERY_DURATION_HOURS}h, "
          f"pump_hydro={config.PUMP_HYDRO_DURATION_HOURS}h)")

    # Transmission efficiency
    for source in graph._adj:
        for _, edge_data in graph._adj[source].items():
            for _, line_data in edge_data.get('lines', {}).items():
                line_data['efficiency'] = config.TRANSMISSION_EFFICIENCY
    print(f"  Transmission efficiency: {config.TRANSMISSION_EFFICIENCY}")

    # CAPEX expansion flags
    optional_load_off = optional_store_off = 0
    for _, node_data in graph._node.items():
        for asset_name, asset_data in node_data.get('assets', {}).items():
            if not asset_name.startswith('optional_'):
                continue
            if not config.ENABLE_CAPEX_EXPANSION:
                if asset_data.get('capex_capacity', 0) != 0:
                    asset_data['capex_capacity'] = 0
                    if asset_data.get('_class') == 'Load':
                        optional_load_off += 1
                    elif asset_data.get('_class') == 'Store':
                        optional_store_off += 1

    if config.ENABLE_CAPEX_EXPANSION:
        print("  Optional-asset CAPEX expansion: ENABLED")
    else:
        print(f"  CAPEX disabled: {optional_load_off} Load, "
              f"{optional_store_off} Store assets")

    # Transmission CAPEX
    line_count = line_enabled = 0
    for source in graph._adj:
        for _, edge_data in graph._adj[source].items():
            for _, line_data in edge_data.get('lines', {}).items():
                line_count += 1
                legacy_limit = line_data.get('capex_limit',
                                              line_data.get('capex_capacity', 0))
                if config.ENABLE_TRANSMISSION_CAPEX_EXPANSION:
                    if legacy_limit == 0:
                        legacy_limit = (
                            line_data.get('installed_capacity', 0)
                            * config.TRANSMISSION_CAPEX_LIMIT_MULTIPLIER
                        )
                    line_data['capex_limit'] = legacy_limit
                    line_data['extensible']  = legacy_limit > 0
                    if line_data['extensible']:
                        line_enabled += 1
                else:
                    line_data['capex_limit'] = 0
                    line_data['extensible']  = False
    print(f"  Transmission CAPEX enabled on {line_enabled}/{line_count} lines")

    # Quick CAPEX cost correction
    if config.APPLY_QUICK_CAPEX_FIX:
        changed  = {'wind': 0, 'solar': 0, 'battery': 0}
        samples  = {}
        for _, node_data in graph._node.items():
            for asset_name, asset_data in node_data.get('assets', {}).items():
                if not asset_name.startswith('optional_'):
                    continue
                fuel = (
                    asset_data.get('fuel') or asset_data.get('type') or ''
                ).lower()
                capex_capacity = asset_data.get('capex_capacity', 0)
                capex_cost     = asset_data.get('capex_cost', 0)
                if capex_capacity in (0, None) or capex_cost in (0, None):
                    continue
                if fuel in ('wind', 'solar'):
                    old = float(capex_cost)
                    asset_data['capex_cost'] = old * config.WIND_SOLAR_MULT
                    changed[fuel] += 1
                    samples.setdefault(fuel, (old, asset_data['capex_cost']))
                elif fuel == 'battery' or asset_data.get('_class') == 'Store':
                    old = float(capex_cost)
                    asset_data['capex_cost'] = old * config.BATTERY_MULT
                    changed['battery'] += 1
                    samples.setdefault('battery', (old, asset_data['capex_cost']))
        print("  Quick CAPEX fix applied:")
        for k, v in changed.items():
            print(f"    {k}: {v} assets")
        for k, (o, n) in samples.items():
            print(f"    sample {k}: {o:.4e} -> {n:.4e} $/W")

    return graph


# ---------------------------------------------------------------------------
# EV load loading
# ---------------------------------------------------------------------------

def load_ev_profile(config):
    """Return a W-unit numpy array of length NUM_HOURS."""
    if config.EV_CHARGING_LOAD_FILE is None:
        np.random.seed(42)
        pattern   = np.sin(np.arange(config.NUM_HOURS) * 2 * np.pi / 24) * 0.3 + 1.0
        variation = np.random.normal(1.0, 0.1, config.NUM_HOURS)
        ev        = np.maximum(pattern * variation, 0.0)
        print("  EV load: synthetic random profile generated")
        return ev

    df = pd.read_csv(config.EV_CHARGING_LOAD_FILE)
    if 'load' in df.columns:
        ev = df['load'].to_numpy(dtype=float)
    else:
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        if not numeric_cols:
            raise RuntimeError(
                f"No numeric column found in {config.EV_CHARGING_LOAD_FILE}."
            )
        ev = df[numeric_cols[0]].to_numpy(dtype=float)
        print(f"  EV load: using column '{numeric_cols[0]}'")

    if len(ev) != config.NUM_HOURS:
        raise RuntimeError(
            f"EV load length {len(ev)} != NUM_HOURS {config.NUM_HOURS}"
        )

    if config.EV_CHARGING_LOAD_UNIT.lower() == 'kw':
        ev = ev * 1000.0
    elif config.EV_CHARGING_LOAD_UNIT.lower() != 'w':
        raise RuntimeError(
            f"Unsupported EV_CHARGING_LOAD_UNIT={config.EV_CHARGING_LOAD_UNIT}"
        )

    # Optional total-energy guard
    max_total = config.NUM_HOURS * 1e9 * 3600
    total     = ev.sum() * 3600
    if total > max_total:
        ev = ev * (max_total / total)
        print(f"  EV load scaled to stay within total-energy guard.")

    print(f"  EV load: peak={ev.max()/1e9:.3f} GW, "
          f"mean={ev.mean()/1e9:.3f} GW, "
          f"total={ev.sum()/1e9:.1f} GWh")
    return ev


# ---------------------------------------------------------------------------
# CAPEX expansion helpers (used to enforce baseline floor on EV scenario)
# ---------------------------------------------------------------------------

def extract_capex_expansion(baseline_solution, original_graph):
    """
    For every optional_ asset in *baseline_solution*, return the solved
    CAPEX expansion (Watts added beyond installed_capacity).

    The ``solution()`` method of Producer/Store writes the capex Var value as a
    one-element list under key ``'capex'``.  If the asset is non-extensible the
    same key holds ``[0]`` (Param).

    Returns
    -------
    dict  {(region, handle): float}   — only entries where expansion > 0
    """
    expansions = {}
    for region, node in baseline_solution._node.items():
        for handle, asset in node.get('assets', {}).items():
            if not handle.startswith('optional_'):
                continue
            capex_val = asset.get('capex', [0])
            if isinstance(capex_val, (list, tuple)):
                expanded_w = float(capex_val[0]) if capex_val else 0.0
            else:
                expanded_w = float(capex_val)
            if expanded_w > 1.0:           # ignore sub-Watt solver noise
                expansions[(region, handle)] = expanded_w
    return expansions


def apply_capex_floor(graph, expansions):
    """
    Apply CAPEX floor constraints from *expansions* onto *graph* in-place.

    For each (region, handle) in *expansions*:
      - Increase ``installed_capacity`` by the expanded amount.
      - Decrease ``capex_capacity`` by the same amount (keeping total cap the same).
      - If capex_capacity would go to zero/negative, set it to 0 (no further expansion).

    This ensures the EV scenario cannot *under-invest* compared to the baseline.
    """
    from copy import deepcopy
    graph = deepcopy(graph)
    applied = 0
    for (region, handle), expansion_w in expansions.items():
        node_data = graph._node.get(region, {})
        asset_data = node_data.get('assets', {}).get(handle)
        if asset_data is None:
            continue
        orig_ic  = asset_data.get('installed_capacity', 0)
        orig_cap = asset_data.get('capex_capacity', 0)

        new_ic  = orig_ic + expansion_w
        new_cap = max(0.0, orig_cap - expansion_w)

        asset_data['installed_capacity'] = new_ic
        asset_data['capex_capacity']     = new_cap
        if new_cap == 0:
            asset_data['extensible'] = False
        applied += 1

    print(f"  CAPEX floor applied: {applied} assets locked ≥ baseline expansion")
    return graph


def load_ev_profile_for_hours(config, start_hour, num_hours):
    """
    Slice the full-year EV profile for a specific *start_hour* window.
    Returns a W-unit numpy array of length *num_hours*.
    """
    import pandas as pd
    if config.EV_CHARGING_LOAD_FILE is None:
        np.random.seed(start_hour % 8760)
        pattern   = np.sin(np.arange(num_hours) * 2 * np.pi / 24) * 0.3 + 1.0
        variation = np.random.normal(1.0, 0.1, num_hours)
        ev        = np.maximum(pattern * variation, 0.0)
        print("  EV load: synthetic random profile generated")
        return ev

    df = pd.read_csv(config.EV_CHARGING_LOAD_FILE)
    if 'load' in df.columns:
        ev_full = df['load'].to_numpy(dtype=float)
    else:
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        if not numeric_cols:
            raise RuntimeError(
                f"No numeric column found in {config.EV_CHARGING_LOAD_FILE}."
            )
        ev_full = df[numeric_cols[0]].to_numpy(dtype=float)
        print(f"  EV load: using column '{numeric_cols[0]}'")

    if config.EV_CHARGING_LOAD_UNIT.lower() == 'kw':
        ev_full = ev_full * 1000.0

    # Wrap-around slice
    n = len(ev_full)
    idx = (np.arange(num_hours) + int(start_hour)) % n
    ev  = ev_full[idx]

    print(f"  EV load slice [{start_hour}:{start_hour+num_hours}]: "
          f"peak={ev.max()/1e9:.3f} GW, mean={ev.mean()/1e9:.3f} GW")
    return ev


# ---------------------------------------------------------------------------
# Lightweight graph reconstruction (for standalone postprocessing from JSON)
# ---------------------------------------------------------------------------

class SimpleGraph:
    """
    Minimal NetworkX-DiGraph-like wrapper built from a solution JSON.
    Supports _node, _adj, and edges(data=True) — enough for all postprocess functions.
    """
    def __init__(self):
        self._node = {}   # region → node_dict
        self._adj  = {}   # source → {target → edge_dict}

    def edges(self, data=False):
        for src, adj in self._adj.items():
            for tgt, edge_data in adj.items():
                yield (src, tgt, edge_data) if data else (src, tgt)


def load_solution_json(path):
    """
    Load a solution JSON saved by *run_baseline* / *run_ev* and return
    (SimpleGraph, metadata_dict).

    metadata_dict keys: timestamp, objective, scenario, start_hour, num_hours.

    Supports both the new list-of-dicts edges format and the old
    "source_target"-keyed dict format (best-effort for the latter).
    """
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    g = SimpleGraph()
    g._node = {str(k): v for k, v in data.get('nodes', {}).items()}

    edges_raw = data.get('edges', [])
    if isinstance(edges_raw, list):
        # New format: list of {source, target, ...}
        for e in edges_raw:
            src  = str(e['source'])
            tgt  = str(e['target'])
            edge = {k: v for k, v in e.items() if k not in ('source', 'target')}
            g._adj.setdefault(src, {})[tgt] = edge
    elif isinstance(edges_raw, dict):
        # Old format: "SRC_TGT" keys — try to split using known node names
        known_nodes = set(g._node.keys())
        for key, edge in edges_raw.items():
            matched = False
            # Try all split points
            for i in range(1, len(key)):
                src = key[:i]
                tgt = key[i+1:] if i + 1 < len(key) else ''
                if src in known_nodes and tgt in known_nodes:
                    g._adj.setdefault(src, {})[tgt] = edge
                    matched = True
                    break
            if not matched:
                # Fallback: split on first '_' that separates two non-empty parts
                parts = key.split('_', 1)
                if len(parts) == 2:
                    g._adj.setdefault(parts[0], {})[parts[1]] = edge

    meta = {
        'timestamp':  data.get('timestamp'),
        'objective':  data.get('objective', 0.0),
        'scenario':   data.get('scenario', 'unknown'),
        'start_hour': int(data.get('start_hour', 0)),
        'num_hours':  int(data.get('num_hours', 168)),
    }
    return g, meta


# ---------------------------------------------------------------------------
# Result serialisation helpers
# ---------------------------------------------------------------------------

class _NpEncoder(json.JSONEncoder):
    def default(self, obj):
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def solution_to_dict(solution):
    """
    Convert a GOOD Network solution graph to a plain serialisable dict.

    Edges are stored as a *list* of dicts with explicit 'source' and 'target'
    keys so that load_solution_json() can reconstruct the graph unambiguously.
    """
    nodes = {}
    for region, node in solution._node.items():
        node_serial = {}
        for k, v in node.items():
            if k == 'assets':
                node_serial['assets'] = {
                    h: {kk: _to_json_safe(vv) for kk, vv in a.items()}
                    for h, a in v.items()
                }
            else:
                node_serial[k] = _to_json_safe(v)
        nodes[region] = node_serial

    # Edges as list (source + target explicit) for reliable reconstruction
    edges = []
    for source, adj in solution._adj.items():
        for target, edge_data in adj.items():
            edge_serial = {'source': source, 'target': target}
            for k, v in edge_data.items():
                if k == 'lines':
                    edge_serial['lines'] = {
                        lh: {kk: _to_json_safe(vv) for kk, vv in ld.items()}
                        for lh, ld in v.items()
                    }
                else:
                    edge_serial[k] = _to_json_safe(v)
            edges.append(edge_serial)

    return {'nodes': nodes, 'edges': edges}


def _to_json_safe(obj):
    import numpy as np
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_json_safe(x) for x in obj]
    return obj
