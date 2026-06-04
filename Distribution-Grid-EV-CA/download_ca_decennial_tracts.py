"""
Download California tract-level Decennial Census data for 2010 and 2020.

This script uses:
- 2010 Decennial SF1 endpoint: /data/2010/dec/sf1
- 2020 Decennial PL endpoint:  /data/2020/dec/pl

API key handling:
- Reads CENSUS_API_KEY from environment variables.
- If missing, loads key from a local .env file next to this script.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DOTENV_PATH = BASE_DIR / ".env"

# Output defaults (2010 path matches existing pipeline conventions)
OUTPUT_PATHS = {
    2010: BASE_DIR
    / "data"
    / "mapping"
    / "census bg to tract 2010"
    / "Census Tract Population_2010_short.csv",
    2020: BASE_DIR
    / "data"
    / "mapping"
    / "census tract 2020"
    / "Census Tract Population_2020_short.csv",
}

DATASETS = {
    2010: ("2010/dec/sf1", "P001001"),
    2020: ("2020/dec/pl", "P1_001N"),
}


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_api_key() -> str:
    load_dotenv(DOTENV_PATH)
    key = os.getenv("CENSUS_API_KEY", "").strip()
    if not key or key == "YOUR_CENSUS_API_KEY_HERE":
        raise SystemExit(
            "Missing Census API key. Set CENSUS_API_KEY in environment or "
            f"update {DOTENV_PATH}."
        )
    return key


def fetch_census_rows(url: str, params: Iterable[tuple[str, str]]) -> list[list[str]]:
    query = urlencode(list(params))
    full_url = f"{url}?{query}"
    with urlopen(full_url, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def download_tract_file(year: int, api_key: str, out_path: Path) -> None:
    dataset, population_var = DATASETS[year]
    api_url = f"https://api.census.gov/data/{dataset}"
    params = [
        ("get", f"NAME,GEO_ID,{population_var}"),
        ("for", "tract:*"),
        ("in", "state:06"),
        ("in", "county:*"),
        ("key", api_key),
    ]

    rows = fetch_census_rows(api_url, params)
    if len(rows) <= 1:
        raise RuntimeError(f"No rows returned for {year} ({api_url}).")

    header = rows[0]
    data = rows[1:]
    df = pd.DataFrame(data, columns=header)

    # Keep a GEOID column compatible with the existing scripts.
    df["GEOID"] = df["GEO_ID"]
    df["Population"] = pd.to_numeric(df[population_var], errors="coerce")
    df["year"] = year

    df = df[["GEOID", "Population", "NAME", "state", "county", "tract", "year"]]
    df = df.sort_values(["county", "tract"]).reset_index(drop=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"[{year}] wrote {len(df):,} rows -> {out_path}")


def main() -> None:
    api_key = get_api_key()
    for year, out_path in OUTPUT_PATHS.items():
        download_tract_file(year, api_key, out_path)


if __name__ == "__main__":
    main()
