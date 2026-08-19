"""
10_02_compute_P_cong_M_BESS.py

From seasonal scenario_summary.csv files compute:
  P_cong = (E_S1 - E_S0) - (E_S3 - E_S0)
  M_BESS = (E_S1 - E_S0) - (E_S2 - E_S0)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

import common as C


def main() -> None:
    rows = []
    for week in C.SEASONAL_WEEKS:
        path = C.ASTR_RESULTS_DIR / week["name"] / "scenario_summary.csv"
        if not path.is_file():
            print(f"missing {path}")
            continue
        df = pd.read_csv(path)
        rows.append(df)
    if not rows:
        print("No scenario summaries found — run 10_01 first.")
        # write empty template
        C.ensure_dir(C.ASTR_RESULTS_DIR)
        pd.DataFrame(
            columns=["season", "E_S0", "E_S1", "E_S2", "E_S3", "P_cong", "M_BESS"]
        ).to_csv(C.ASTR_RESULTS_DIR / "P_cong_M_BESS.csv", index=False)
        return

    all_df = pd.concat(rows, ignore_index=True)
    ok = all_df[all_df["status"] == "ok"].copy()
    if ok.empty or "co2_kg" not in ok.columns:
        print("No successful solves with co2_kg; writing status table only.")
        all_df.to_csv(C.ASTR_RESULTS_DIR / "scenario_summary_all.csv", index=False)
        return

    metrics = []
    for season, g in ok.groupby("season"):
        e = g.set_index("scenario")["co2_kg"].to_dict()
        e0, e1, e2, e3 = (e.get(s) for s in ("S0", "S1", "S2", "S3"))
        if None in (e0, e1, e3):
            print(f"  {season}: incomplete S0/S1/S3")
            continue
        p_cong = (e1 - e0) - (e3 - e0)
        m_bess = (e1 - e0) - (e2 - e0) if e2 is not None else float("nan")
        metrics.append(
            {
                "season": season,
                "E_S0": e0,
                "E_S1": e1,
                "E_S2": e2,
                "E_S3": e3,
                "P_cong": p_cong,
                "M_BESS": m_bess,
                "delta_EV_S1": e1 - e0,
                "delta_EV_S3": e3 - e0,
            }
        )
        print(f"  {season}: P_cong={p_cong:.3e} kg  M_BESS={m_bess}")

    out = pd.DataFrame(metrics)
    out_path = C.ASTR_RESULTS_DIR / "P_cong_M_BESS.csv"
    out.to_csv(out_path, index=False)
    if len(out):
        pooled = {
            "season": "pooled_sum",
            "E_S0": out["E_S0"].sum(),
            "E_S1": out["E_S1"].sum(),
            "E_S2": out["E_S2"].sum(),
            "E_S3": out["E_S3"].sum(),
            "P_cong": out["P_cong"].sum(),
            "M_BESS": out["M_BESS"].sum(),
            "delta_EV_S1": out["delta_EV_S1"].sum(),
            "delta_EV_S3": out["delta_EV_S3"].sum(),
        }
        pd.concat([out, pd.DataFrame([pooled])], ignore_index=True).to_csv(
            out_path, index=False
        )
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
