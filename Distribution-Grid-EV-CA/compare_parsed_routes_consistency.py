"""
compare_parsed_routes_consistency.py

Compare the generated parsed TAZ path files (parsed_100000_LDPTM/ and
parsed_100000_ETM/) against reference route tables:

- data/all_routes.csv           (LD / internal TAZ routing; includes `v`)
- data/parsed_100000_ext.csv   (ETM / external-to-CA routing)

Outputs CSV reports under:
  data/route_consistency_reports/

It focuses first on (from,to) OD-pairs + total `distance` consistency.
Optionally, for a limited subset of inconsistent pairs, it compares the
multiset of waypoint TAZs (`TAZ12,`) between our output and the reference.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from collections import Counter, defaultdict

import numpy as np
import pandas as pd


BASE_DIR = Path(__file__).resolve().parent

OURS_LD_DIR = BASE_DIR / "data" / "mobility_data" / "TAZ_distance" / "parsed_100000_LDPTM"
OURS_ETM_DIR = BASE_DIR / "data" / "mobility_data" / "TAZ_distance" / "parsed_100000_ETM"

REF_ALL_ROUTES = BASE_DIR / "data" / "all_routes.csv"
REF_PARSED_EXT = BASE_DIR / "data" / "parsed_100000_ext.csv"

OUT_DIR = BASE_DIR / "data" / "route_consistency_reports"

PAIR_MULT = 10_000  # safe because TAZ12 range in these files is <= 6910


def _pair_key(from_zone: int | np.ndarray, to_zone: int | np.ndarray) -> np.ndarray:
    return np.asarray(from_zone, dtype=np.int64) * PAIR_MULT + np.asarray(
        to_zone, dtype=np.int64
    )


def _iter_part_csvs(dir_path: Path) -> list[Path]:
    parts = sorted(dir_path.glob("part_*.csv"))
    if not parts:
        raise SystemExit(f"No part_*.csv found under: {dir_path}")
    return parts


@dataclass(frozen=True)
class Inconsistency:
    from_zone: int
    to_zone: int
    ours_distance: float | None
    ref_distance: float | None
    diff_m: float | None
    reason: str


def build_ours_distance_map(parts: list[Path]) -> tuple[dict[int, float], set[int]]:
    """
    Returns:
      - dict key=(from*PAIR_MULT+to) -> distance
      - set of all pair keys seen
    Also detects cases where multiple distances appear for same pair in our output.
    """
    dist_map: dict[int, float] = {}
    seen: set[int] = set()

    for fp in parts:
        df = pd.read_csv(fp, usecols=["from", "to", "distance"])
        keys = _pair_key(df["from"].to_numpy(), df["to"].to_numpy())
        dists = df["distance"].to_numpy(dtype=float)

        # group within file to avoid per-row dict churn
        tmp = pd.DataFrame({"key": keys, "distance": dists})
        file_pair_dist = tmp.groupby("key", as_index=False)["distance"].min()

        for k, d in zip(file_pair_dist["key"].to_numpy(dtype=np.int64), file_pair_dist["distance"].to_numpy()):
            seen.add(int(k))
            if int(k) not in dist_map:
                dist_map[int(k)] = float(d)

    return dist_map, seen


def build_ref_distance_map(
    ref_csv: Path, pair_keys: set[int], *, chunksize: int = 500_000
) -> tuple[dict[int, float], set[int], dict[int, set[float]]]:
    """
    Scans reference CSV in chunks and extracts distance per key
    for keys present in `pair_keys`.

    Returns:
      - ref_dist_map: key -> distance
      - ref_seen_keys: set of keys found
      - ref_distance_values: key -> set(distances) when multiple distances encountered
    """
    ref_dist_map: dict[int, float] = {}
    ref_seen: set[int] = set()
    ref_distance_values: dict[int, set[float]] = defaultdict(set)
    pair_keys_arr = np.fromiter(pair_keys, dtype=np.int64)

    usecols = None
    if ref_csv.name == "all_routes.csv":
        usecols = ["from", "to", "distance"]
    else:
        # parsed_100000_ext.csv
        usecols = ["from", "to", "distance"]

    for chunk in pd.read_csv(ref_csv, usecols=usecols, chunksize=chunksize):
        keys = _pair_key(chunk["from"].to_numpy(), chunk["to"].to_numpy())
        mask = np.isin(keys, pair_keys_arr)
        if not mask.any():
            continue
        sub = chunk.loc[mask, ["from", "to", "distance"]].copy()
        sub_keys = _pair_key(sub["from"].to_numpy(), sub["to"].to_numpy()).astype(np.int64)
        sub["key"] = sub_keys

        # multiple rows per pair in reference; distance should be constant
        # but if not, record distinct values.
        grouped = sub.groupby("key")["distance"].agg(lambda s: set(np.round(s.astype(float), 6)))

        for k, dset in grouped.items():
            k_int = int(k)
            ref_seen.add(k_int)
            for dv in dset:
                ref_distance_values[k_int].add(float(dv))
                if k_int not in ref_dist_map:
                    ref_dist_map[k_int] = float(dv)

    return ref_dist_map, ref_seen, ref_distance_values


def compare_distances(
    ours_dist_map: dict[int, float],
    ref_dist_map: dict[int, float],
    ref_distance_values: dict[int, set[float]],
    *,
    rel_tol: float,
    abs_tol_m: float,
) -> list[Inconsistency]:
    inconsistencies: list[Inconsistency] = []

    for k, ours_d in ours_dist_map.items():
        if k not in ref_dist_map:
            from_zone = k // PAIR_MULT
            to_zone = k % PAIR_MULT
            inconsistencies.append(
                Inconsistency(
                    from_zone=int(from_zone),
                    to_zone=int(to_zone),
                    ours_distance=float(ours_d),
                    ref_distance=None,
                    diff_m=None,
                    reason="missing_in_reference",
                )
            )
            continue

        ref_d = float(ref_dist_map[k])
        diff = float(ours_d - ref_d)
        adiff = abs(diff)
        if adiff > max(abs_tol_m, rel_tol * abs(ref_d)):
            from_zone = k // PAIR_MULT
            to_zone = k % PAIR_MULT
            inconsistencies.append(
                Inconsistency(
                    from_zone=int(from_zone),
                    to_zone=int(to_zone),
                    ours_distance=float(ours_d),
                    ref_distance=float(ref_d),
                    diff_m=diff,
                    reason="distance_mismatch",
                )
            )

        # if reference itself has multiple distances for the same pair, flag it
        # (this is rare but useful for debugging).
        if k in ref_distance_values and len(ref_distance_values[k]) > 1:
            from_zone = k // PAIR_MULT
            to_zone = k % PAIR_MULT
            inconsistencies.append(
                Inconsistency(
                    from_zone=int(from_zone),
                    to_zone=int(to_zone),
                    ours_distance=float(ours_d),
                    ref_distance=float(ref_d),
                    diff_m=None,
                    reason="reference_distance_inconsistent",
                )
            )

    return inconsistencies


def extract_waypoint_multisets(
    parts_or_ref: Iterable[Path] | Path,
    *,
    pair_keys: set[int],
    usecols: list[str],
    taz_col: str,
    is_reference: bool,
) -> dict[int, Counter]:
    """
    Reads either:
      - a list of part CSVs (ours), or
      - a single reference CSV
    and returns:
      key -> Counter({TAZ12,: count})
    for only the requested `pair_keys`.
    """
    out: dict[int, Counter] = {k: Counter() for k in pair_keys}
    pair_keys_arr = np.fromiter(pair_keys, dtype=np.int64)

    parts: list[Path]
    if isinstance(parts_or_ref, Path):
        parts = [parts_or_ref]
    else:
        parts = list(parts_or_ref)

    for fp in parts:
        # For reference CSVs, they can be large; chunking keeps memory bounded.
        if fp.name.endswith(".csv") and fp.stat().st_size > 200_000_000:
            chunks = pd.read_csv(fp, usecols=usecols, chunksize=500_000)
            for chunk in chunks:
                keys = _pair_key(chunk["from"].to_numpy(), chunk["to"].to_numpy())
                mask = np.isin(keys, np.fromiter(pair_keys, dtype=np.int64))
                if not mask.any():
                    continue
                sub = chunk.loc[mask, ["from", "to", taz_col]].copy()
                sub_keys = _pair_key(sub["from"].to_numpy(), sub["to"].to_numpy()).astype(np.int64)
                for k, tz in zip(sub_keys, sub[taz_col].to_numpy()):
                    out[int(k)][int(tz)] += 1
        else:
            df = pd.read_csv(fp, usecols=usecols)
            keys = _pair_key(df["from"].to_numpy(), df["to"].to_numpy())
            mask = np.isin(keys, pair_keys_arr)
            if not mask.any():
                continue
            sub = df.loc[mask, ["from", "to", taz_col]].copy()
            sub_keys = _pair_key(sub["from"].to_numpy(), sub["to"].to_numpy()).astype(np.int64)
            for k, tz in zip(sub_keys, sub[taz_col].to_numpy()):
                out[int(k)][int(tz)] += 1

    return out


def compare_waypoints(
    ours_wp: dict[int, Counter],
    ref_wp: dict[int, Counter],
) -> list[dict[str, object]]:
    diffs: list[dict[str, object]] = []
    for k in ours_wp.keys():
        ours_c = ours_wp[k]
        ref_c = ref_wp.get(k, Counter())
        if ours_c == ref_c:
            continue
        from_zone = k // PAIR_MULT
        to_zone = k % PAIR_MULT
        # show up to 10 TAZs with differing counts for readability
        all_tazes = set(ours_c.keys()) | set(ref_c.keys())
        mismatches = []
        for tz in sorted(all_tazes)[:200]:
            if ours_c.get(tz, 0) != ref_c.get(tz, 0):
                mismatches.append((tz, ours_c.get(tz, 0), ref_c.get(tz, 0)))
        diffs.append(
            {
                "from": int(from_zone),
                "to": int(to_zone),
                "ours_rows": int(sum(ours_c.values())),
                "ref_rows": int(sum(ref_c.values())),
                "ours_unique": int(len(ours_c)),
                "ref_unique": int(len(ref_c)),
                "mismatch_examples": mismatches[:10],
            }
        )
    return diffs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare generated parsed TAZ route files against reference route tables."
    )
    parser.add_argument("--rel-tol", type=float, default=0.01, help="Relative tolerance for distance mismatch.")
    parser.add_argument("--abs-tol-m", type=float, default=50.0, help="Absolute tolerance for distance mismatch (meters).")
    parser.add_argument(
        "--max-route-check",
        type=int,
        default=2000,
        help="How many inconsistent pairs to waypoint-compare (multiset).",
    )
    parser.add_argument("--no-waypoint-check", action="store_true", help="Only check OD pair distance consistency.")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------- LDPTM internal ----------------
    ld_parts = _iter_part_csvs(OURS_LD_DIR)
    print(f"[LD] Loading ours distance map from {len(ld_parts)} part files...")
    ours_ld_dist_map, ours_ld_keys = build_ours_distance_map(ld_parts)

    print(f"[LD] Building reference distance map for {len(ours_ld_keys):,} keys...")
    ref_ld_dist_map, ref_ld_seen, ref_ld_distance_values = build_ref_distance_map(
        REF_ALL_ROUTES, ours_ld_keys
    )

    ld_incons = compare_distances(
        ours_ld_dist_map,
        ref_ld_dist_map,
        ref_ld_distance_values,
        rel_tol=args.rel_tol,
        abs_tol_m=args.abs_tol_m,
    )

    ld_df = pd.DataFrame(
        [
            {
                "from": x.from_zone,
                "to": x.to_zone,
                "ours_distance": x.ours_distance,
                "ref_distance": x.ref_distance,
                "diff_m": x.diff_m,
                "reason": x.reason,
            }
            for x in ld_incons
        ]
    )
    ld_csv = OUT_DIR / "inconsistencies_LD.csv"
    ld_df.to_csv(ld_csv, index=False)
    print(f"[LD] wrote {ld_csv} ({len(ld_df):,} inconsistent pairs/flags)")

    # ---------------- ETM external ----------------
    etm_parts = _iter_part_csvs(OURS_ETM_DIR)
    print(f"[ETM] Loading ours distance map from {len(etm_parts)} part files...")
    ours_etm_dist_map, ours_etm_keys = build_ours_distance_map(etm_parts)

    print(f"[ETM] Building reference distance map for {len(ours_etm_keys):,} keys...")
    ref_etm_dist_map, ref_etm_seen, ref_etm_distance_values = build_ref_distance_map(
        REF_PARSED_EXT, ours_etm_keys
    )

    etm_incons = compare_distances(
        ours_etm_dist_map,
        ref_etm_dist_map,
        ref_etm_distance_values,
        rel_tol=args.rel_tol,
        abs_tol_m=args.abs_tol_m,
    )

    etm_df = pd.DataFrame(
        [
            {
                "from": x.from_zone,
                "to": x.to_zone,
                "ours_distance": x.ours_distance,
                "ref_distance": x.ref_distance,
                "diff_m": x.diff_m,
                "reason": x.reason,
            }
            for x in etm_incons
        ]
    )
    etm_csv = OUT_DIR / "inconsistencies_ETM.csv"
    etm_df.to_csv(etm_csv, index=False)
    print(f"[ETM] wrote {etm_csv} ({len(etm_df):,} inconsistent pairs/flags)")

    # ---------------- Optional waypoint multiset check ----------------
    if args.no_waypoint_check:
        print("[waypoint-check] disabled via --no-waypoint-check")
        return

    # Only check waypoint multiset for a subset of inconsistent pairs.
    def _keys_from_df(df: pd.DataFrame) -> list[int]:
        keys = []
        for r in df[["from", "to"]].itertuples(index=False):
            keys.append(int(_pair_key(int(r[0]), int(r[1]))))
        return keys

    # LD waypoint diff
    ld_keys_to_check = set(_keys_from_df(ld_df.head(args.max_route_check)))
    if ld_keys_to_check:
        print(f"[LD] waypoint multiset compare for up to {len(ld_keys_to_check):,} pairs...")
        ours_ld_wp = extract_waypoint_multisets(
            ld_parts,
            pair_keys=ld_keys_to_check,
            usecols=["from", "to", "TAZ12,"],
            taz_col="TAZ12,",
            is_reference=False,
        )
        ref_ld_wp = extract_waypoint_multisets(
            REF_ALL_ROUTES,
            pair_keys=ld_keys_to_check,
            usecols=["from", "to", "TAZ12,"],
            taz_col="TAZ12,",
            is_reference=True,
        )
        ld_wp_diffs = compare_waypoints(ours_ld_wp, ref_ld_wp)
        ld_wp_csv = OUT_DIR / "waypoint_inconsistencies_LD.csv"
        pd.DataFrame(ld_wp_diffs).to_csv(ld_wp_csv, index=False)
        print(f"[LD] wrote {ld_wp_csv} ({len(ld_wp_diffs):,} waypoint mismatches)")

    # ETM waypoint diff
    etm_keys_to_check = set(_keys_from_df(etm_df.head(args.max_route_check)))
    if etm_keys_to_check:
        print(f"[ETM] waypoint multiset compare for up to {len(etm_keys_to_check):,} pairs...")
        ours_etm_wp = extract_waypoint_multisets(
            etm_parts,
            pair_keys=etm_keys_to_check,
            usecols=["from", "to", "TAZ12,"],
            taz_col="TAZ12,",
            is_reference=False,
        )
        ref_etm_wp = extract_waypoint_multisets(
            REF_PARSED_EXT,
            pair_keys=etm_keys_to_check,
            usecols=["from", "to", "TAZ12,"],
            taz_col="TAZ12,",
            is_reference=True,
        )
        etm_wp_diffs = compare_waypoints(ours_etm_wp, ref_etm_wp)
        etm_wp_csv = OUT_DIR / "waypoint_inconsistencies_ETM.csv"
        pd.DataFrame(etm_wp_diffs).to_csv(etm_wp_csv, index=False)
        print(f"[ETM] wrote {etm_wp_csv} ({len(etm_wp_diffs):,} waypoint mismatches)")


if __name__ == "__main__":
    main()

