"""
10_02_compute_P_cong_M_BESS.py

From S0–S3 dispatch:

  P_cong  = (E_S1 - E_S0) - (E_S3 - E_S0)
  M_BESS  = (E_S1 - E_S0) - (E_S2 - E_S0)

Also ranks binding transmission corridors (line-hour frequency).

Writes:
  data/results/summary_metrics_8760.json          (if 8760 results exist)
  data/results/summary_metrics_four_week.json     (4-week test)
  astr_meso_results/P_cong_M_BESS.csv
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

import common as C


def _load_summaries() -> pd.DataFrame:
    frames = []
    four = C.ASTR_RESULTS_DIR / "four_week" / "scenario_summary.csv"
    y8760 = C.ASTR_RESULTS_DIR / "8760" / "scenario_summary.csv"
    if four.is_file():
        frames.append(pd.read_csv(four))
    if y8760.is_file():
        frames.append(pd.read_csv(y8760))
    for week in C.SEASONAL_WEEKS:
        path = C.ASTR_RESULTS_DIR / week["name"] / "scenario_summary.csv"
        if path.is_file():
            df = pd.read_csv(path)
            if "tag" not in df.columns:
                df["tag"] = week["name"]
            if "horizon" not in df.columns:
                df["horizon"] = "weekly"
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _metrics_from_group(g: pd.DataFrame, key: str) -> dict | None:
    ok = g[g["status"] == "ok"] if "status" in g.columns else g
    if ok.empty or "co2_kg" not in ok.columns:
        return None
    e = ok.set_index("scenario")["co2_kg"].to_dict()
    e0, e1, e2, e3 = (e.get(s) for s in ("S0", "S1", "S2", "S3"))
    if None in (e0, e1, e3):
        return None
    p_cong = (e1 - e0) - (e3 - e0)
    m_bess = (e1 - e0) - (e2 - e0) if e2 is not None else float("nan")
    row = {
        "key": key,
        "E_S0": e0,
        "E_S1": e1,
        "E_S2": e2,
        "E_S3": e3,
        "P_cong": p_cong,
        "M_BESS": m_bess,
        "delta_EV_S1": e1 - e0,
        "delta_EV_S3": e3 - e0,
    }
    if "objective" in ok.columns:
        obj = ok.set_index("scenario")["objective"].to_dict()
        row.update({f"obj_{s}": obj.get(s) for s in ("S0", "S1", "S2", "S3")})
    if "binding_line_hours" in ok.columns:
        bind = ok.set_index("scenario")["binding_line_hours"].to_dict()
        row["binding_hours_S1"] = bind.get("S1")
        row["binding_hours_S3"] = bind.get("S3")
    return row


def _top_binding_corridors(tag: str, n: int = 25) -> list[dict]:
    path = C.ASTR_RESULTS_DIR / tag / "S1" / "line_flows_summary.csv"
    if not path.is_file():
        return []
    df = pd.read_csv(path)
    if df.empty or "binding_hours" not in df.columns:
        return []
    meso = df[df["line"].astype(str).str.startswith("meso_")].copy()
    use = meso if not meso.empty else df
    use = use.sort_values("binding_hours", ascending=False).head(n)
    return use.to_dict(orient="records")


def main() -> None:
    all_df = _load_summaries()
    C.ensure_dir(C.ASTR_RESULTS_DIR)
    C.ensure_dir(C.RESULTS_DIR)
    if all_df.empty:
        print("No scenario summaries found — run 10_01 first.")
        pd.DataFrame(columns=["key", "E_S0", "E_S1", "E_S2", "E_S3", "P_cong", "M_BESS"]).to_csv(
            C.ASTR_RESULTS_DIR / "P_cong_M_BESS.csv", index=False
        )
        return

    all_df.to_csv(C.ASTR_RESULTS_DIR / "scenario_summary_all.csv", index=False)
    metrics = []
    group_col = "tag" if "tag" in all_df.columns else "season"
    if group_col not in all_df.columns:
        all_df[group_col] = "run"
    for key, g in all_df.groupby(group_col):
        row = _metrics_from_group(g, str(key))
        if row is None:
            print(f"  {key}: incomplete S0/S1/S3")
            continue
        print(f"  {key}: P_cong={row['P_cong']:.3e} kg  M_BESS={row['M_BESS']}")
        metrics.append(row)

    out = pd.DataFrame(metrics)
    csv_path = C.ASTR_RESULTS_DIR / "P_cong_M_BESS.csv"
    if len(out):
        numeric = out[["E_S0", "E_S1", "E_S2", "E_S3", "P_cong", "M_BESS", "delta_EV_S1", "delta_EV_S3"]].copy()
        pooled = numeric.sum(numeric_only=True).to_dict()
        pooled["key"] = "pooled_sum"
        out = pd.concat([out, pd.DataFrame([pooled])], ignore_index=True)
    out.to_csv(csv_path, index=False)
    print(f"Wrote {csv_path}")

    payload = {
        "metrics": metrics,
        "formula": {
            "P_cong": "(E_S1 - E_S0) - (E_S3 - E_S0)",
            "M_BESS": "(E_S1 - E_S0) - (E_S2 - E_S0)",
        },
        "top_binding_corridors": {},
    }
    for key in out.get("key", []):
        if key == "pooled_sum":
            continue
        payload["top_binding_corridors"][str(key)] = _top_binding_corridors(str(key))

    # Prefer writing the matching canonical JSON
    tags = set(all_df[group_col].astype(str))
    if "8760" in tags:
        path = C.SUMMARY_METRICS_JSON
    else:
        path = C.FOUR_WEEK_METRICS_JSON
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {path}")
    # Always also copy a generic name used by the execution plan
    C.SUMMARY_METRICS_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
