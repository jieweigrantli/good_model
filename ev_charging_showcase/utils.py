"""Showcase helpers — load scaling, baseline copy, manifest."""

from __future__ import annotations

import glob
import json
import os
import shutil
from datetime import datetime
from typing import Any

import numpy as np

import ev_charging_project.config as base_config
from ev_charging_project.utils import load_ev_profile_for_hours


def load_scaled_ev_profile(
    scenario: dict,
    start_hour: int,
    num_hours: int,
) -> np.ndarray:
    """Return W-unit EV load slice for a showcase scenario."""
    ev = load_ev_profile_for_hours(base_config, start_hour, num_hours)
    scale = float(scenario.get("ev_scale", 1.0))
    if scale != 1.0:
        ev = ev * scale
    if scale == 0.0:
        ev = np.zeros_like(ev)
    print(
        f"  Scenario {scenario['name']}: scale={scale:g}, "
        f"fleet={scenario.get('fleet_millions', '?')}M, "
        f"peak={ev.max()/1e9:.3f} GW"
    )
    return ev


def copy_baseline_outputs(src_dir: str, dst_dir: str) -> None:
    """Copy baseline solution/objective files into a scenario folder."""
    os.makedirs(dst_dir, exist_ok=True)
    for pattern in ("baseline_solution_*.json", "baseline_objective_*.txt"):
        for path in glob.glob(os.path.join(src_dir, pattern)):
            shutil.copy2(path, os.path.join(dst_dir, os.path.basename(path)))


def mirror_baseline_as_ev(baseline_dir: str, ev_dir: str) -> str | None:
    """For the no-EV scenario: copy the baseline solution into the ev/ folder.

    Lets the no-EV reference share the uniform <scenario>/ev/ layout (so
    --postprocess-only and the manifest behave identically) without a solve.
    Returns the written ev_solution path, or None.
    """
    os.makedirs(ev_dir, exist_ok=True)
    written = None
    for path in glob.glob(os.path.join(baseline_dir, "baseline_solution_*.json")):
        name = os.path.basename(path).replace("baseline_solution_", "ev_solution_")
        dst = os.path.join(ev_dir, name)
        shutil.copy2(path, dst)
        written = dst
    for path in glob.glob(os.path.join(baseline_dir, "baseline_objective_*.txt")):
        name = os.path.basename(path).replace("baseline_objective_", "ev_objective_")
        shutil.copy2(path, os.path.join(ev_dir, name))
    return written


def lock_capex_to_baseline(graph, baseline_expansions: dict) -> Any:
    """Return a deep copy of *graph* with baseline CAPEX baked in and frozen.

    For every ``optional_`` asset:
      - add any baseline expansion (W) into ``installed_capacity`` (treated as
        already-built), and
      - set ``capex_capacity = 0`` and ``extensible = False`` so the EV solve
        cannot add ANY capacity on top of the baseline buildout.

    This makes the EV scenarios dispatch on the exact baseline grid; extra EV
    load is balanced via existing assets / shortfall, never new CAPEX.
    """
    from copy import deepcopy

    graph = deepcopy(graph)
    baked = frozen = 0
    for region, node_data in graph._node.items():
        for handle, asset_data in node_data.get("assets", {}).items():
            if not handle.startswith("optional_"):
                continue
            expansion = float(baseline_expansions.get((region, handle), 0.0))
            if expansion > 0:
                asset_data["installed_capacity"] = (
                    asset_data.get("installed_capacity", 0) + expansion
                )
                baked += 1
            if asset_data.get("capex_capacity", 0) not in (0, None):
                asset_data["capex_capacity"] = 0
                frozen += 1
            asset_data["extensible"] = False
    print(
        f"  Locked CAPEX to baseline: {baked} expanded assets baked in, "
        f"{frozen} optional assets frozen (no EV-side expansion)"
    )
    return graph


def scenario_iteration_cfg(scenario: dict, start_hour: int, num_hours: int) -> dict:
    """Map showcase scenario to postprocess iteration_cfg."""
    return {
        "name": scenario["name"],
        "month": scenario["label"],
        "year": scenario["year"],
        "start_hour": start_hour,
        "num_hours": num_hours,
        "fleet_millions": scenario["fleet_millions"],
        "ev_scale": scenario["ev_scale"],
        "description": scenario.get("description", ""),
        "window": "June week",
    }


def write_scenario_meta(
    scenario_dir: str,
    scenario: dict,
    baseline_obj: float,
    ev_obj: float,
    paths: dict[str, str],
) -> str:
    """Write per-scenario metadata JSON for interactive loading."""
    meta = {
        "scenario": scenario,
        "baseline_objective": baseline_obj,
        "ev_objective": ev_obj,
        "objective_diff": ev_obj - baseline_obj,
        "paths": paths,
        "generated_at": datetime.now().isoformat(),
    }
    out = os.path.join(scenario_dir, "scenario_meta.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return out


def write_showcase_manifest(
    output_root: str,
    scenarios: list[dict],
    baseline_shared_dir: str,
    results: list[dict[str, Any]],
) -> str:
    """Top-level manifest listing all scenarios and key artifact paths."""
    manifest = {
        "title": "EV Charging Interactive Showcase",
        "window": {
            "start_hour": scenarios[0].get("start_hour") if results else None,
            "num_hours": 168,
            "label": "June week (hours 3624–3791)",
        },
        "baseline_shared_dir": baseline_shared_dir,
        "scenarios": results,
        "generated_at": datetime.now().isoformat(),
    }
    out = os.path.join(output_root, "showcase_manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return out
