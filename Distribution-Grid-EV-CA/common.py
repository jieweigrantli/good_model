"""
common.py — Shared helpers used across the Distribution-Grid-EV-CA Python ports.

Notes on the R→Python translation:
- R data.table → pandas.DataFrame
- fread / fwrite → pd.read_csv / df.to_csv
- readRDS / saveRDS → pd.read_pickle / df.to_pickle (extension .pkl)
  (if you need to read native .rds files produced by R, install ``pyreadr``
   and replace _read_rds with pyreadr.read_r(...).)
- library(sf) → geopandas; st_read → gpd.read_file; st_transform → gdf.to_crs
- ggplot2 → matplotlib (and optionally seaborn)
- setnames(df, "a", "b") → df.rename(columns={"a": "b"}, inplace=True)
- dcast(df, A ~ B, value.var="v") → df.pivot_table(index=A, columns=B, values="v")
- melt (data.table) → pd.melt
"""

from __future__ import annotations

import glob
import json
import os
import pickle
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project roots / ASTR–GRIP paths
# ---------------------------------------------------------------------------

# Directory containing this module (Distribution-Grid-EV-CA/)
PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR / "data"
REPO_ROOT = PKG_DIR.parent

# Nested unzip layout from PG&E GRIP Shape download
GRIP_SHP_DIR = DATA_DIR / "GRIP_SHP" / "GRIP_SHP" / "GRIP_SHP" / "GRIP_SHP"

GRIP_ED_SUBSTATIONS = GRIP_SHP_DIR / "EDSubstations.shp"
GRIP_TRANSMISSION_LINES = GRIP_SHP_DIR / "TransmissionLines.shp"
GRIP_FEEDER_DETAIL = GRIP_SHP_DIR / "FeederDetail.shp"
GRIP_SUBSTATION_LOAD_PROFILE = GRIP_SHP_DIR / "SubstationLoadProfile.shp"
GRIP_FEEDER_LOAD_PROFILE = GRIP_SHP_DIR / "FeederLoadProfile.shp"

TAZ_CENTROID_GPKG = DATA_DIR / "shps" / "TAZ_centroid_sf.gpkg"
TAZ_POLYGON_SHP = DATA_DIR / "shps" / "taz_id.shp"
MULTIDAY_OUTPUT_DIR = PKG_DIR / "multiday_charging" / "outputs"
TAZ_TOTAL_DEMAND_CSV = MULTIDAY_OUTPUT_DIR / "taz_total_demand_kwh.csv"
FLEET_DAILY_HOURLY_NPY = MULTIDAY_OUTPUT_DIR / "daily_hourly_load_kW.npy"

MAPPING_DIR = DATA_DIR / "mapping"
MESO_DIR = DATA_DIR / "meso"
RESULTS_DIR = DATA_DIR / "results"
FIGURES_ASTR_DIR = PKG_DIR / "figures" / "ASTR_diagnostics"
ASTR_RESULTS_DIR = PKG_DIR / "astr_meso_results"

# Spatial caches (downloaded on first use if missing)
HIFLD_SUBSTATIONS_GPKG = DATA_DIR / "hifld" / "electric_substations_ca.gpkg"
CEC_UTILITY_GPKG = DATA_DIR / "hifld" / "ca_electric_utility_territories.gpkg"
HIFLD_TX_LINES_GPKG = DATA_DIR / "hifld" / "electric_transmission_lines_ca.gpkg"

# Manually-fetched CEC ArcGIS layers (the live CEC_UTILITY_QUERY_URL service was
# retired; these already sit on disk under the modern CEC service names).
CEC_LSE_IOU_POU_GPKG = DATA_DIR / "cec" / "ca_lse_iou_pou.gpkg"
CEC_TRANSMISSION_GPKG = DATA_DIR / "cec" / "ca_transmission_lines.gpkg"
CEC_BALANCING_AUTH_GPKG = DATA_DIR / "cec" / "ca_balancing_authorities.gpkg"

# Canonical ASTR meso artifacts
TAZ_TO_SUBSTATION_CSV = MAPPING_DIR / "taz_to_substation.csv"
TAZ_HOURLY_EV_8760 = MESO_DIR / "taz_hourly_ev_8760.parquet"
SUB_HOURLY_LOADS_8760 = MESO_DIR / "substation_hourly_loads_8760.parquet"
CA_NETWORK_JSON = MESO_DIR / "ca_substation_network.json"
NESTED_GRAPH_JSON = MESO_DIR / "wecc_ca_nested_graph.json"
SCENARIO_SCALES_JSON = MESO_DIR / "scenario_capacity_scales.json"
SUMMARY_METRICS_JSON = RESULTS_DIR / "summary_metrics_8760.json"
FOUR_WEEK_METRICS_JSON = RESULTS_DIR / "summary_metrics_four_week.json"
RUNS_8760_PARQUET = RESULTS_DIR / "S0_S3_8760_runs.parquet"
RUNS_FOUR_WEEK_PARQUET = RESULTS_DIR / "S0_S3_four_week_runs.parquet"

WEC_JSON = REPO_ROOT / "Examples" / "WEC.json"
WEC_MODIFIED_JSON = REPO_ROOT / "Examples" / "WEC_modified.json"
POLICIES_JSON = REPO_ROOT / "Examples" / "policies.json"

# California Albers Equal Area (meters) — required for ASTR spatial joins
CA_ALBERS_CRS = "EPSG:3310"

# Rated-kV → MVA heuristics when a line has no rated MVA
KV_TO_MVA = {
    500: 1500,
    345: 1000,
    230: 400,
    161: 250,
    138: 200,
    115: 150,
    69: 80,
    60: 60,
}

# Approximate geographic centroid per in-CA WECC BA (lon, lat WGS84). Used both
# as the nearest-BA reference point for parent_ba assignment and as the
# synthetic "BA_GW_<ba>" gateway-substation location when no real substation
# is available for a BA (e.g. before HIFLD/CEC data is loaded).
BA_CENTROIDS_LL = {
    "WEC_CALN": (-122.0, 38.0),
    "WEC_BANC": (-121.5, 38.6),
    "WECC_SCE": (-117.5, 34.0),
    "WEC_LADW": (-118.3, 34.1),
    "WEC_SDGE": (-117.1, 32.8),
    "WECC_IID": (-115.5, 33.0),
}

# Named CA intertie / path attachment points (lon, lat WGS84)
INTERTIE_POINTS = {
    "Path66_Malin": {
        "lon": -121.55,
        "lat": 42.00,
        "parent_ba": "WEC_CALN",
        "remote_ba": "WECC_PNW",
        "path": "Path 66 / COI",
    },
    "Path15_LosBanos": {
        "lon": -120.85,
        "lat": 37.05,
        "parent_ba": "WEC_CALN",
        "remote_ba": "WECC_SCE",
        "path": "Path 15",
    },
    "Path15_Midway": {
        "lon": -119.60,
        "lat": 35.40,
        "parent_ba": "WEC_CALN",
        "remote_ba": "WECC_SCE",
        "path": "Path 15",
    },
    "Path26_Vincent": {
        "lon": -118.38,
        "lat": 34.48,
        "parent_ba": "WECC_SCE",
        "remote_ba": "WEC_CALN",
        "path": "Path 26",
    },
    "PaloVerde_Devers": {
        "lon": -116.57,
        "lat": 33.93,
        "parent_ba": "WECC_SCE",
        "remote_ba": "WECC_AZ",
        "path": "Palo Verde",
    },
}

# Substring match on CEC/HIFLD utility-territory names → WECC BA
UTILITY_TO_BA = (
    ("pacific gas", "WEC_CALN"),
    ("pg&e", "WEC_CALN"),
    ("pge", "WEC_CALN"),
    ("sacramento municipal", "WEC_BANC"),
    ("smud", "WEC_BANC"),
    ("modesto irrigation", "WEC_BANC"),
    ("turlock", "WEC_BANC"),
    ("roseville", "WEC_BANC"),
    ("redding", "WEC_BANC"),
    ("southern california edison", "WECC_SCE"),
    ("sce", "WECC_SCE"),
    ("los angeles", "WEC_LADW"),
    ("ladwp", "WEC_LADW"),
    ("san diego gas", "WEC_SDGE"),
    ("sdg&e", "WEC_SDGE"),
    ("sdge", "WEC_SDGE"),
    ("imperial irrigation", "WECC_IID"),
    ("iid", "WECC_IID"),
)

PGE_NAME_TOKENS = ("pacific gas", "pg&e", "pge")

HOUSING_COL_CANDIDATES = (
    "HH", "HOUSEHOLDS", "HOUSING", "TOTHH", "hh", "Households", "housing",
)
EMPLOYMENT_COL_CANDIDATES = (
    "EMP", "EMPLOY", "EMPLOYMENT", "TOTEMP", "emp", "Jobs", "employment",
)


def is_meso_delivery_node(node_id: str) -> bool:
    """True for nested CA delivery nodes (SUB_* substations or aggregated MESO_*)."""
    s = str(node_id)
    return s.startswith("SUB_") or s.startswith("MESO_")


def resolve_wec_json() -> Path:
    """Prefer WEC_modified.json when present; otherwise Examples/WEC.json."""
    if WEC_MODIFIED_JSON.is_file():
        return WEC_MODIFIED_JSON
    return WEC_JSON


def require_file(path: Path, hint: str = "") -> Path:
    if not path.is_file():
        extra = f" {hint}" if hint else ""
        raise FileNotFoundError(f"Required input missing: {path}.{extra}")
    return path


def _row_value_ci(row, candidates: Iterable[str]):
    """Case-insensitive lookup of the first present, truthy-numeric column value.

    Column names vary by data source (e.g. GRIP's "RATEDKV" vs CEC's "kV"), so
    a plain ``in`` membership check against a fixed-case candidate list misses
    real columns. This matches case-insensitively, like ``pick_column``.
    """
    idx = list(getattr(row, "index", ())) if not isinstance(row, dict) else list(row.keys())
    lower = {c.lower(): c for c in idx}
    for cand in candidates:
        col = lower.get(cand.lower())
        if col is None:
            continue
        val = row[col]
        try:
            if val is not None and float(val) > 0:
                return float(val)
        except (TypeError, ValueError):
            continue
    return None


def line_limit_mva(row, default_kv: float = 115.0) -> float:
    """Rated MVA if present, else voltage heuristic (500 kV≈1500 MVA, …)."""
    mva = _row_value_ci(
        row,
        ("RATEDMVA", "RATED_MVA", "MVA", "CAPACITY", "CAP_MVA", "RATE_MVA", "THERMALMVA"),
    )
    if mva is not None:
        return mva
    kv = _row_value_ci(row, ("RATEDKV", "VOLTAGE", "KV", "VOLT_CLASS", "VOLT")) or default_kv
    keys = np.array(list(KV_TO_MVA.keys()), dtype=float)
    nearest = int(keys[np.argmin(np.abs(keys - kv))])
    return float(KV_TO_MVA[nearest])


def line_rated_kv(row, default_kv: float = 115.0) -> float:
    """Rated kV if present on the row (case-insensitive column match), else default."""
    return _row_value_ci(row, ("RATEDKV", "VOLTAGE", "KV", "VOLT_CLASS", "VOLT")) or default_kv


def line_limit_w(row, default_kv: float = 115.0) -> float:
    return line_limit_mva(row, default_kv=default_kv) * 1e6


def pad_or_wrap_hours(arr, n: int) -> np.ndarray:
    """Return a 1-D float array of length n (pad last / wrap / truncate)."""
    a = np.asarray(arr, dtype=float).reshape(-1)
    if a.size == n:
        return a
    if a.size == 0:
        return np.zeros(n, dtype=float)
    if a.size > n:
        return a[:n]
    out = np.empty(n, dtype=float)
    reps = n // a.size
    out[: reps * a.size] = np.tile(a, reps)
    rem = n - reps * a.size
    if rem:
        out[reps * a.size :] = a[:rem]
    return out


def slice_hours(arr, start_hour: int, num_hours: int) -> np.ndarray:
    a = np.asarray(arr, dtype=float).reshape(-1)
    if a.size == 0:
        return np.zeros(num_hours, dtype=float)
    idx = (np.arange(num_hours) + int(start_hour)) % a.size
    return a[idx]


def concat_seasonal_weeks(arr, week_hours: int | None = None) -> np.ndarray:
    """Concatenate the four representative weeks into one chronology."""
    n = NUM_HOURS_WEEK if week_hours is None else int(week_hours)
    parts = [slice_hours(arr, int(w["start_hour"]), n) for w in SEASONAL_WEEKS]
    return np.concatenate(parts)


def download_arcgis_geojson(query_url: str, out_path: Path, where: str = "1=1"):
    """Page an ArcGIS FeatureServer /query endpoint into a GeoPackage."""
    import geopandas as gpd

    ensure_dir(out_path.parent)
    frames = []
    offset = 0
    page_size = 2000
    while True:
        params = (
            f"?where={urllib.parse.quote(where)}"
            f"&outFields=*"
            f"&returnGeometry=true&outSR=4326"
            f"&resultOffset={offset}&resultRecordCount={page_size}"
            f"&f=geojson"
        )
        url = query_url + params
        print(f"  ArcGIS download offset={offset} ...")
        with urllib.request.urlopen(url, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        feats = payload.get("features") or []
        if not feats:
            break
        frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326"))
        if len(feats) < page_size:
            break
        offset += page_size
    if not frames:
        raise RuntimeError(f"ArcGIS download returned no features: {query_url}")
    out = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs="EPSG:4326")
    out.to_file(out_path, driver="GPKG")
    print(f"  wrote {out_path} ({len(out)} features)")
    return out


def ba_gateway_points_gdf():
    """Synthetic 'BA_GW_<ba>' substation-proxy points, one per in-CA WECC BA.

    Used as a last-resort stand-in only where no real substation exists for a
    BA (e.g. before HIFLD/CEC data is available). Shared across 08_01/08_03/
    09_01 so all three agree on the same id/geometry per BA.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    rows = [
        {
            "substation_id": f"BA_GW_{ba}",
            "substation_name": f"Gateway {ba}",
            "source": "ba_gateway",
            "geometry": Point(lon, lat),
        }
        for ba, (lon, lat) in BA_CENTROIDS_LL.items()
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(CA_ALBERS_CRS)


def utility_is_pge(name: str) -> bool:
    s = str(name).lower()
    return any(tok in s for tok in PGE_NAME_TOKENS)


def utility_to_ba(name: str) -> str | None:
    s = str(name).lower()
    for token, ba in UTILITY_TO_BA:
        if token in s:
            return ba
    return None


def pick_column(columns, candidates: Iterable[str]) -> str | None:
    cols = list(columns)
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in cols:
            return cand
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


CALIFORNIA_REGIONS = [
    "WEC_BANC",
    "WEC_CALN",
    "WEC_LADW",
    "WEC_SDGE",
    "WECC_IID",
    "WECC_SCE",
]

# Seasonal week windows (hour-of-year start), matching ev_charging_project/config.py
SEASONAL_WEEKS = [
    {"name": "march", "start_hour": 1416, "month": "March"},
    {"name": "june", "start_hour": 3624, "month": "June"},
    {"name": "september", "start_hour": 5832, "month": "September"},
    {"name": "december", "start_hour": 8016, "month": "December"},
]
NUM_HOURS_WEEK = 7 * 24
HOURS_YEAR = 8760
NUM_HOURS_FOUR_WEEK = len(SEASONAL_WEEKS) * NUM_HOURS_WEEK


def horizon_hours(horizon: str) -> int:
    h = str(horizon).lower().replace("-", "_")
    if h in {"8760", "year", "annual", "full"}:
        return HOURS_YEAR
    if h in {"four_week", "fourweek", "seasonal_concat", "4week"}:
        return NUM_HOURS_FOUR_WEEK
    if h in {"week", "weekly", "season"}:
        return NUM_HOURS_WEEK
    raise ValueError(f"Unknown horizon {horizon!r}")


def grip_layer(name: str) -> Path:
    """Return path to a GRIP shapefile layer by basename (with or without .shp)."""
    if not name.lower().endswith(".shp"):
        name = f"{name}.shp"
    return GRIP_SHP_DIR / name


def ensure_dir(path: Path | str) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------

def fread_all_in_dir(directory: str, pattern: str = "*.csv", **read_csv_kwargs) -> pd.DataFrame:
    """Read every matching file in a directory and rbind them (like R's
    ``rbindlist(lapply(list.files(), fread))``).
    """
    files = sorted(glob.glob(os.path.join(directory, pattern)))
    if not files:
        return pd.DataFrame()
    parts = [pd.read_csv(f, **read_csv_kwargs) for f in files]
    return pd.concat(parts, ignore_index=True)


def read_rds_like(path: str) -> pd.DataFrame:
    """Read a pickle file that mirrors R's ``readRDS``.

    In the Python port, we save/read intermediate tables as ``.pkl``.
    If ``path`` ends in .rds, this function will look for the matching .pkl
    next to it (same basename).
    """
    if path.endswith(".rds"):
        path = path[:-4] + ".pkl"
    if path.endswith(".pkl") or path.endswith(".pickle"):
        with open(path, "rb") as fh:
            obj = pickle.load(fh)
        return obj
    return pd.read_pickle(path)


def save_rds_like(df: pd.DataFrame, path: str) -> str:
    """Save a DataFrame mirroring R's ``saveRDS`` but as pickle.

    Returns the actual path written (always .pkl).
    """
    if path.endswith(".rds"):
        path = path[:-4] + ".pkl"
    elif not (path.endswith(".pkl") or path.endswith(".pickle")):
        path = path + ".pkl"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_pickle(path)
    return path


# ---------------------------------------------------------------------------
# data.table-ish helpers
# ---------------------------------------------------------------------------

def unique_rows(df: pd.DataFrame, subset: Iterable[str] | None = None) -> pd.DataFrame:
    """R's ``unique(dt)``."""
    return df.drop_duplicates(subset=list(subset) if subset else None).reset_index(drop=True)


def group_sample(
    df: pd.DataFrame,
    by: list[str],
    n_col: str,
    id_col: str,
    seed: int | None = None,
) -> pd.DataFrame:
    """Within each group ``by``, sample ``min(n_col)`` distinct values of ``id_col``.
    Returns one row per sampled id with the grouping columns plus the id."""
    rng = np.random.default_rng(seed)
    out_frames: list[pd.DataFrame] = []
    for keys, g in df.groupby(by, sort=False):
        if g.empty:
            continue
        n_take = int(g[n_col].min())
        n_take = max(0, min(n_take, len(g)))
        if n_take == 0:
            continue
        idx = rng.choice(len(g), size=n_take, replace=False)
        picked = g.iloc[idx][[id_col]].copy()
        if not isinstance(keys, tuple):
            keys = (keys,)
        for k, v in zip(by, keys):
            picked[k] = v
        out_frames.append(picked)
    return (
        pd.concat(out_frames, ignore_index=True)
        if out_frames
        else pd.DataFrame(columns=list(by) + [id_col])
    )


def weighted_sample(values, weights, n, seed=None, replace=True):
    """R's ``sample(values, n, prob=weights, replace=TRUE)``."""
    rng = np.random.default_rng(seed)
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()
    return rng.choice(np.asarray(values), size=n, replace=replace, p=w)
