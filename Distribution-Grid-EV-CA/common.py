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
# Long format (TAZ, substation_id, weight, rank, ...): each TAZ's load split
# across its k nearest substations; weights sum to 1 per TAZ.
TAZ_TO_SUBSTATION_KNN_CSV = MAPPING_DIR / "taz_to_substation_knn.csv"
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
WEC_EGRID2023_JSON = REPO_ROOT / "Examples" / "WEC_egrid2023.json"
POLICIES_JSON = REPO_ROOT / "Examples" / "policies.json"
RPS_FRACTION_CSV = REPO_ROOT / "good_datasets-main" / "Data" / "US" / "Raw" / "rps_fraction.csv"

# The generator fleets the pipeline can read. "egrid2023" is the fleet that
# existed in 2023, written by 08_19. "ipm" is EPA's IPM v6.17 fleet with its
# projected plants, which the first sets of results were run on.
FLEETS = {"egrid2023": WEC_EGRID2023_JSON, "ipm": WEC_MODIFIED_JSON, "base": WEC_JSON}

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
# Named WECC transfer paths, with their published simultaneous transfer
# ratings (WECC Path Rating Catalog / CAISO transmission planning):
#   Path 66 (COI, Malin)        4,800 MW N->S
#   Path 15 (Los Banos-Midway)  5,400 MW N->S, split across its two terminals
#   Path 26 (Midway-Vincent)    4,000 MW N->S
#   Palo Verde - Devers         2,800 MW
# A flat 500 MW was previously used for all five, which understated the
# north-south paths by an order of magnitude and made CA-internal transfer
# look far more constrained than it is.
INTERTIE_POINTS = {
    "Path66_Malin": {
        "lon": -121.55,
        "lat": 42.00,
        "parent_ba": "WEC_CALN",
        "remote_ba": "WECC_PNW",
        "path": "Path 66 / COI",
        "rating_W": 4800e6,
    },
    "Path15_LosBanos": {
        "lon": -120.85,
        "lat": 37.05,
        "parent_ba": "WEC_CALN",
        "remote_ba": "WECC_SCE",
        "path": "Path 15",
        "rating_W": 2700e6,
    },
    "Path15_Midway": {
        "lon": -119.60,
        "lat": 35.40,
        "parent_ba": "WEC_CALN",
        "remote_ba": "WECC_SCE",
        "path": "Path 15",
        "rating_W": 2700e6,
    },
    "Path26_Vincent": {
        "lon": -118.38,
        "lat": 34.48,
        "parent_ba": "WECC_SCE",
        "remote_ba": "WEC_CALN",
        "path": "Path 26",
        "rating_W": 4000e6,
    },
    "PaloVerde_Devers": {
        "lon": -116.57,
        "lat": 33.93,
        "parent_ba": "WECC_SCE",
        "remote_ba": "WECC_AZ",
        "path": "Palo Verde",
        "rating_W": 2800e6,
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


def resolve_wec_json(fleet: str | None = None) -> Path:
    """The generator fleet file.

    ``fleet``, or failing that the ASTR_FLEET environment variable, names one of
    FLEETS. With neither, the 2023 fleet is used when it has been built, then
    WEC_modified.json, then Examples/WEC.json.
    """
    fleet = fleet or os.environ.get("ASTR_FLEET")
    if fleet:
        if fleet not in FLEETS:
            raise ValueError(f"Unknown fleet {fleet!r}; choose from {sorted(FLEETS)}")
        return require_file(FLEETS[fleet])
    for path in (WEC_EGRID2023_JSON, WEC_MODIFIED_JSON):
        if path.is_file():
            return path
    return WEC_JSON


def fleet_name(path: Path | None = None) -> str:
    """The name in FLEETS of a fleet file; of the one in use if none is given."""
    path = Path(path) if path is not None else resolve_wec_json()
    return next((name for name, p in FLEETS.items() if p == path), path.name)


def results_fleet_json(scenario_dir: Path) -> Path:
    """The fleet file a solved scenario was run on.

    A run records its fleet in settings.json. Results with no record predate the
    2023 fleet and were all run on WEC_modified.json.
    """
    settings = Path(scenario_dir) / "settings.json"
    name = None
    if settings.is_file():
        with open(settings, encoding="utf-8") as fh:
            name = json.load(fh).get("fleet")
    return resolve_wec_json(name or "ipm")


def rps_ratios(year: int) -> dict[str, float]:
    """State RPS ratios for a year, from the ReEDS table policies.json was built from.

    policies.json holds the 2025 column of this table. The ratios are shares of a
    state's electricity sales.
    """
    require_file(RPS_FRACTION_CSV)
    df = pd.read_csv(RPS_FRACTION_CSV)
    df = df[df["t"] == int(year)]
    if df.empty:
        raise ValueError(f"{RPS_FRACTION_CSV.name} has no year {year}")
    return {str(st): float(v) for st, v in zip(df["st"], df["rps_all"])}


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

# Seasonal week windows (hour-of-year start), mirrored in astr_v2.SEASONAL_WEEKS
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

def measured_substation_baseload_high():
    """PG&E's published substation load, per substation per month-hour, in kW.

    Returns columns (substation_id, Month, Hour, baseload_kW) from GRIP's
    ``SubstationLoadProfile``, using the ``high`` band. ``high`` rather than a
    midpoint because this feeds an adequacy test: measured against NREL county
    demand for PG&E territory, ``high`` recovers a 15.59 GW coincident peak
    against NREL's 20.41 GW and PG&E's real ~20-21 GW, while a midpoint gives
    13.02 GW and understates by a third.

    ``monthhour`` is formatted ``MM_HH``, and ``subid`` needs zero-padding to
    five digits to match the rest of the pipeline -- an earlier version of this
    join lost 271 of 638 substations to unpadded ids.
    """
    import pyogrio

    path = grip_layer("SubstationLoadProfile")
    cols = ["substation_id", "Month", "Hour", "baseload_kW"]
    if not path.is_file():
        return pd.DataFrame(columns=cols)
    d = pyogrio.read_dataframe(
        str(path), columns=["subid", "monthhour", "high"], read_geometry=False
    )
    d["high"] = pd.to_numeric(d["high"], errors="coerce")
    d = d.dropna(subset=["high"])
    d["substation_id"] = d["subid"].astype(str).str.strip().str.zfill(5)
    d["Month"] = d["monthhour"].str[:2].astype(int)
    d["Hour"] = d["monthhour"].str[3:].astype(int)
    d["baseload_kW"] = d["high"].clip(lower=0)
    return d[cols]


def published_substation_ratings():
    """Substation step-down ratings that a utility actually publishes.

    PG&E gives per-bank ratings in the GRIP distribution-forecast layer; SCE
    gives substation ratings in GNA and, failing that, projected load plus
    remaining capacity in ICA. Shared by 08_03 (to detect allocation
    artefacts) and 08_06 (to build the model's transformer limits), so both
    agree on what is measured and what is derived.
    """
    import geopandas as gpd
    import numpy as np
    import pandas as pd

    frames = []

    df_path = grip_layer("DFSubstationArea___PeakFacilityLoadingPercent")
    if df_path.is_file():
        import pyogrio

        df = pyogrio.read_dataframe(str(df_path), read_geometry=False)
        df["facilityra"] = pd.to_numeric(df["facilityra"], errors="coerce")
        pge = (
            df.dropna(subset=["facilityra"])
            .groupby("substation", as_index=False)["facilityra"]
            .sum()
            .rename(columns={"substation": "substation_id", "facilityra": "mva"})
        )
        pge["substation_id"] = pge["substation_id"].astype(str)
        pge["rating_W"] = pge["mva"] * 1e6
        frames.append(pge[["substation_id", "rating_W"]])

    sce_dir = DATA_DIR / "ica" / "sce"
    pts = sce_dir / "ica_substations_geom.gpkg"
    if pts.is_file() and HIFLD_SUBSTATIONS_GPKG.is_file():
        p = gpd.read_file(pts).to_crs(CA_ALBERS_CRS)
        hif = gpd.read_file(HIFLD_SUBSTATIONS_GPKG).to_crs(CA_ALBERS_CRS)
        hif["substation_id"] = "HIFLD_" + hif["OBJECTID"].astype(str)
        name_col = pick_column(p.columns, ("SUB_NAME", "sub_name", "NAME"))
        j = gpd.sjoin_nearest(
            p[[name_col, "geometry"]], hif[["substation_id", "geometry"]],
            how="left", distance_col="d",
        )
        j = j[j["d"] <= 2000.0]
        lut = {str(r[name_col]).strip().upper(): r["substation_id"] for _, r in j.iterrows()}

        rows: dict[str, float] = {}
        gna = sce_dir / "gna_substations.parquet"
        if gna.is_file():
            g = pd.read_parquet(gna)
            g["rating"] = pd.to_numeric(g.get("rating"), errors="coerce")
            for _, r in g.dropna(subset=["rating"]).iterrows():
                nid = lut.get(str(r.get("substation_name", "")).strip().upper())
                if nid and r["rating"] > 0:
                    rows[nid] = max(rows.get(nid, 0.0), float(r["rating"]) * 1e6)
        ica = sce_dir / "substations.parquet"
        if ica.is_file():
            s_ = pd.read_parquet(ica)
            cap = (
                pd.to_numeric(s_.get("PROJECTED_LOAD"), errors="coerce").fillna(0)
                + pd.to_numeric(s_.get("MAX_REMAIN_CAP"), errors="coerce").fillna(0)
            ) * 1e6
            for nm, cv in zip(s_.get("SUB_NAME", []), cap):
                nid = lut.get(str(nm).strip().upper())
                if nid and cv > 0 and nid not in rows:
                    rows[nid] = float(cv)
        if rows:
            frames.append(pd.DataFrame({"substation_id": list(rows),
                                        "rating_W": list(rows.values())}))

    if not frames:
        return pd.DataFrame(columns=["substation_id", "rating_W"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("substation_id")


def sce_substation_nodes():
    """SCE published substation name (upper-cased) -> model node id.

    Matched by location rather than name: SCE calls a yard "Universal
    69/12 kV" where HIFLD calls it "Universal City".
    """
    import geopandas as gpd

    pts = DATA_DIR / "ica" / "sce" / "ica_substations_geom.gpkg"
    if not (pts.is_file() and HIFLD_SUBSTATIONS_GPKG.is_file()):
        return {}
    p = gpd.read_file(pts).to_crs(CA_ALBERS_CRS)
    hif = gpd.read_file(HIFLD_SUBSTATIONS_GPKG).to_crs(CA_ALBERS_CRS)
    hif["substation_id"] = "HIFLD_" + hif["OBJECTID"].astype(str)
    name_col = pick_column(p.columns, ("SUB_NAME", "sub_name", "NAME"))
    j = gpd.sjoin_nearest(
        p[[name_col, "geometry"]], hif[["substation_id", "geometry"]],
        how="left", distance_col="d",
    )
    j = j[j["d"] <= 2000.0]
    return {str(r[name_col]).strip().upper(): r["substation_id"] for _, r in j.iterrows()}


# ---------------------------------------------------------------------------
# Injection capacity per substation
# ---------------------------------------------------------------------------
# A wire out of a substation has to carry whatever injects there, so anything
# that can push power onto the node counts. Getting this membership test wrong
# is easy, because the GOOD v1 schema this model inherits represents variable
# renewables as the ``Load`` class with a type of "solar" or "wind" rather than
# as ``Producer`` -- they are profile-driven assets, and the class name refers
# to how they enter the energy balance, not to which way the power flows. A
# filter of ``class == "Producer"`` therefore silently drops all 1,041 CA solar
# and wind records, which are exactly the assets sitting at the generation
# switchyards whose export path this is used to size.
#
# Stores count at their power rating: a pump-hydro unit can inject its full
# discharge capacity, and the limit is symmetric because it charges through the
# same wire. ``optional`` records are excluded -- a capex candidate may never be
# built, so sizing a wire to it would presume the expansion the model decides.
VRE_LOAD_TYPES = ("solar", "wind")


def injection_capacity_by_hub(records) -> dict[str, float]:
    """hub_id -> W of firm injection capacity mapped to that substation."""
    out: dict[str, float] = {}
    for rec in records or []:
        hid = rec.get("hub_id")
        if not hid or rec.get("optional"):
            continue
        cls = rec.get("class")
        if cls not in ("Producer", "Load", "Store"):
            continue
        if cls == "Load" and str(rec.get("type", "")).lower() not in VRE_LOAD_TYPES:
            continue  # genuine demand; handled in 08_03
        out[hid] = out.get(hid, 0.0) + float(rec.get("installed_capacity") or 0.0)
    return out


# ---------------------------------------------------------------------------
# Plant emission rates, read from eGRID itself
# ---------------------------------------------------------------------------
# WEC_modified.json was built from eGRID 2023, and its per-asset ``co2`` is wrong
# for every plant that emits 1,000 lb/MWh or more. The 2023 file writes large
# numbers with a thousands separator ("2,169.248"); the 2021 file did not, and
# the build's parser did not expect it. Measured on the fossil units in the WECC
# model: where the raw rate is below 1,000 lb/MWh the asset value equals it on
# 490 of 490 units, and where it is 1,000 or more it equals it on 0 of 720 --
# 62 of 63 coal units, 546 gas units, 112 oil units, 45 GW in all. Coal came out
# at 87 kg/MWh capacity-weighted against 1,048 in the build made from the 2021
# file. 67 of the file's 150 columns carry separators.
#
# Capacities and heat rates are untouched: they come from NEEDS, not eGRID, and
# are identical between the two builds. So this is an accounting fault, not a
# dispatch one, and the rates are read here directly rather than by rebuilding
# the dataset.
EGRID_PLANT_CSV = REPO_ROOT / "good_datasets-main" / "Data" / "US" / "Raw" / "egrid2023_data.csv"
EGRID_ORIS_COLUMN = "DOE/EIA ORIS plant or facility code"
EGRID_CO2_RATE_COLUMN = "Plant annual CO2 total output emission rate (lb/MWh)"
KG_PER_LB = 0.45359237

_EGRID_CO2: dict[str, float] | None = None


def oris_key(value) -> str | None:
    """ORIS plant code as a plain integer string, or None."""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return None


def egrid_plant_co2_kg_per_mwh() -> dict[str, float]:
    """ORIS code -> plant CO2 output emission rate in kg/MWh, from eGRID 2023.

    Plant-level, so every unit at a plant shares one rate. A plant with no rate
    is left out. A rate of zero is kept: for a geothermal or biomass plant it is
    the reported value, and the caller decides what it means for a fossil one.
    """
    global _EGRID_CO2
    if _EGRID_CO2 is not None:
        return _EGRID_CO2
    _EGRID_CO2 = {}
    if not EGRID_PLANT_CSV.is_file():
        print(f"  WARNING: {EGRID_PLANT_CSV} missing; emission rates fall back to heat rates")
        return _EGRID_CO2
    import pandas as pd

    df = pd.read_csv(EGRID_PLANT_CSV, dtype=str, low_memory=False,
                     usecols=[EGRID_ORIS_COLUMN, EGRID_CO2_RATE_COLUMN])
    rate = pd.to_numeric(df[EGRID_CO2_RATE_COLUMN].str.replace(",", "", regex=False), errors="coerce")
    for code, lb in zip(df[EGRID_ORIS_COLUMN], rate):
        key = oris_key(code)
        if key is not None and pd.notna(lb) and lb >= 0:
            _EGRID_CO2.setdefault(key, float(lb) * KG_PER_LB)
    return _EGRID_CO2


# kg CO2 per million Btu of fuel burned. EIA, "Carbon Dioxide Emissions
# Coefficients", released 18 September 2024.
CO2_KG_PER_MMBTU = {"coal": 95.99, "natural gas": 52.91, "oil": 74.14}

# Fuels that fall back to heat rate x carbon content when the plant has no
# usable eGRID rate, and for which a plant rate of zero means "no output
# reported that year" and so is not a rate.
PLANT_RATE_FALLBACK_FUELS = ("coal", "natural gas", "oil")

# Fuels that burn nothing. They take zero whatever eGRID lists for the plant
# code they share. Without this a solar field on the same ORIS code as a gas
# plant inherits the gas plant's rate, which makes the median for "solar"
# positive, and the median fallback then spreads it to every solar unit with no
# rate of its own: 0.49 Mt of CO2 from solar in a four-week run, caught in
# testing before it reached any result.
NON_EMITTING_FUELS = ("solar", "wind", "hydro", "nuclear", "battery", "pump hydro")


def emission_factor(fuel, oris, heat_rate_btu_per_kwh, published) -> tuple[float, str]:
    """kg CO2 per MWh for one asset, and where the number came from.

    In order: zero for a fuel that burns nothing; the plant's own rate in eGRID
    2023; for coal, gas and oil at a plant with no usable rate, the unit's heat
    rate times the fuel's carbon content; otherwise the value published on the
    asset, which may be missing and is then filled from the fuel's median by the
    caller.
    """
    fuel = str(fuel).lower()
    if fuel in NON_EMITTING_FUELS:
        return 0.0, "non_emitting"
    key = oris_key(oris)
    plant = egrid_plant_co2_kg_per_mwh().get(key) if key is not None else None
    if plant is not None and (plant > 0 or fuel not in PLANT_RATE_FALLBACK_FUELS):
        return float(plant), "egrid2023_plant"
    try:
        hr = float(heat_rate_btu_per_kwh)
    except (TypeError, ValueError):
        hr = float("nan")
    if fuel in PLANT_RATE_FALLBACK_FUELS and hr > 0:
        return hr / 1000.0 * CO2_KG_PER_MMBTU[fuel], "heat_rate"
    try:
        value = float(published)
    except (TypeError, ValueError):
        value = float("nan")
    return value, "published"


# Sources whose value is a reported number even when it is zero, so the fuel
# median must not overwrite it.
KNOWN_FACTOR_SOURCES = ("egrid2023_plant", "non_emitting", "heat_rate")
