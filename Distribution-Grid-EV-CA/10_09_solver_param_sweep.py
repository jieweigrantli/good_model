"""
10_09_solver_param_sweep.py

Time a set of Gurobi parameter configurations on one seasonal week, to pick
settings for the 672 h run without paying 672 h prices to find them.

Why sweep on a week
-------------------
The four-week LP takes hours per attempt, so comparing configurations on it
directly is unaffordable. One week has the same shape -- presolved 314k x 1.10M
against the four-week 1.20M x 4.38M, so columns outnumber rows by 3.5x either
way -- and it reliably reaches a certified optimum, which gives a clean
time-to-optimal number to rank configurations by. Settings that help here are
the candidates to carry over; the carry-over is an assumption, not a guarantee,
so the winner still has to be confirmed on the full horizon.

What is being attacked
----------------------
Barrier is not the expensive part. It runs 1,000 iterations in ~1,234 s at
1.23 s each (Factor Ops 5.8e8, "less than 1 second per iteration"). The cost is
the crossover clean-up: ~4.1M simplex iterations, single-threaded, which spent
3.5 of 4 hours on one of 14 cores while barrier had been using all of them. So
the configurations below mostly aim at either parallelising that phase
(concurrent LP) or shrinking it (sifting, aggregation, scaling).

Usage
  python 10_09_solver_param_sweep.py --week june --limit 2400
  python 10_09_solver_param_sweep.py --week june --configs baseline concurrent sifting

Writes astr_meso_results/solver_param_sweep.csv
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

# Each entry is a raw Gurobi parameter dict layered over the script's defaults
# (Method=2, Crossover per --crossover, BarHomogeneous=1, Presolve=2,
# NumericFocus=1, ScaleFlag=2, Aggregate=0).
CONFIGS: dict[str, dict] = {
    # what the model runs today, for reference
    "baseline": {},
    # Concurrent LP: barrier, primal simplex and dual simplex on separate
    # threads, first to finish wins. Aimed squarely at the single-threaded
    # crossover clean-up while 13 cores sit idle.
    "concurrent": {"Method": 3},
    # Deterministic variant: same idea, reproducible, usually a little slower.
    "concurrent_det": {"Method": 5},
    # Sifting works a column subset at a time instead of pricing all 4.4M every
    # iteration, which is the standard remedy when columns greatly outnumber rows.
    "sifting": {"Sifting": 2},
    # Aggregation is switched off in the current defaults. Turning it back on may
    # cut the presolved model, and everything downstream scales with that size.
    "aggregate": {"Aggregate": 1},
    # Geometric-mean scaling, for an RHS range spanning [4e4, 9e13].
    "scale_geo": {"ScaleFlag": 3},
    # Higher numerical effort, against the dual-infeasibility oscillation that
    # stopped the four-week run certifying (it reached 6.18 then rebounded to 320).
    "numfocus2": {"NumericFocus": 2},
    # Barrier alone: no crossover at all, as an upper bound on how much of the
    # runtime crossover owns. Values from an interior point are less reliable.
    "nocross": {"Crossover": 0},
    # The two most promising ideas together.
    "concurrent_agg": {"Method": 3, "Aggregate": 1},

    # --- numerics set -------------------------------------------------------
    # Added after the parallelism set won at 168 h (2.89x) and then LOST at
    # 672 h: concurrent splits 14 cores three ways, barrier slows from 1,234 s
    # to 1,916 s, and crossover starts from further back. Lesson: sweep on the
    # horizon you actually care about. These target the real symptom instead --
    # dual infeasibility oscillating over two orders of magnitude while the
    # objective creeps, which is conditioning, not slowness.
    #
    # Quadruple-precision simplex. The closest match to the symptom: built for
    # LPs where simplex cannot hold a stable basis. Slower per iteration, so it
    # only wins if it cuts iteration count sharply.
    "quad": {"Quad": 1},
    # ScaleFlag has only ever been run at 2 (aggressive), and the log carries a
    # live "unscaled primal violation" warning, so scaling is doing real work.
    "scale_std": {"ScaleFlag": 1},
    "scale_geo672": {"ScaleFlag": 3},
    # Aggressive presolve can worsen conditioning even as it shrinks the model.
    "presolve1": {"Presolve": 1},
    # Maximum numerical effort.
    "numfocus3": {"NumericFocus": 3},
}

RE_SOLVED = re.compile(r"Solved in (\d+) iterations and ([\d.]+) seconds")
RE_BARRIER = re.compile(r"Barrier (?:solved model|performed) in (\d+) iterations and ([\d.]+) seconds")
RE_OPT = re.compile(r"Optimal objective\s+([-\d.e+]+)")
RE_LIMIT = re.compile(r"Time limit reached|ITERATION LIMIT|Stopped in")


RE_ITER = re.compile(r"^\s*\d+\s+([\d.]+e[+-]\d+)\s+([\d.]+e[+-]\d+)\s+([\d.]+e[+-]\d+)\s+(\d+)s\s*$", re.M)


def _progress_from_log(tag: str, scenario: str) -> dict:
    """Last simplex line of the child's Gurobi log: how far it actually got.

    On four_week nothing certifies inside a sweep budget, so time-to-optimal
    cannot rank the configurations. What can is the objective reached and
    whether dual infeasibility was descending or merely oscillating.
    """
    log = C.ASTR_RESULTS_DIR / tag / scenario / "gurobi.log"
    if not log.is_file():
        return {}
    text = log.read_text(encoding="utf-8", errors="ignore")
    # Gurobi APPENDS to LogFile, so the file holds every run ever made with this
    # tag. Scanning all of it silently attributes the previous config's progress
    # to this one: NumericFocus=3 never cleared barrier ("Stopped in 0
    # iterations"), produced no simplex lines, and so inherited Presolve=1's
    # numbers byte for byte. Keep only the last run's segment.
    marker = "Optimize a model"
    if marker in text:
        text = text[text.rfind(marker):]
    rows = RE_ITER.findall(text)
    if not rows:
        return {}
    obj, pinf, dinf, secs = rows[-1]
    out = {"last_obj": float(obj), "last_primal_inf": float(pinf),
           "last_dual_inf": float(dinf), "last_s": int(secs)}
    tail = [float(r[2]) for r in rows[-40:]]
    if tail:
        out["dual_inf_min_tail"] = min(tail)
        out["dual_inf_max_tail"] = max(tail)
    return out


def run_one(name: str, params: dict, week: str, limit: int, only_ba: str,
            scenario: str, no_capex: bool, horizon: str = "weekly") -> dict:
    env = dict(os.environ)
    env["ASTR_TIME_LIMIT_S"] = str(limit)
    if params:
        env["ASTR_GUROBI_PARAMS"] = json.dumps(params)
    else:
        env.pop("ASTR_GUROBI_PARAMS", None)

    cmd = [
        sys.executable, str(PKG / "10_01_run_scenarios_S0_S3.py"),
        "--only-ba", only_ba, "--scenarios", scenario, "--crossover", "1",
    ]
    if horizon == "four_week":
        cmd += ["--horizon", "four_week"]
        tag = f"four_week_{only_ba}"
    else:
        cmd += ["--horizon", "weekly", "--seasons", week]
        tag = f"{week}_{only_ba}"
    if no_capex:
        cmd.append("--no-capex")

    print(f"\n=== {name}: {params or 'defaults'} ===", flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(PKG), env=env,
                          capture_output=True, text=True, timeout=limit + 1800)
    wall = time.time() - t0
    out = proc.stdout + proc.stderr

    rec = {"config": name, "params": json.dumps(params), "wall_s": round(wall, 1)}
    m = RE_BARRIER.search(out)
    if m:
        rec["barrier_iters"], rec["barrier_s"] = int(m.group(1)), float(m.group(2))
    m = RE_SOLVED.search(out)
    if m:
        rec["total_iters"], rec["solve_s"] = int(m.group(1)), float(m.group(2))
    m = RE_OPT.search(out)
    if m:
        rec["objective"] = float(m.group(1))
    rec["certified"] = bool(m) and not RE_LIMIT.search(out)
    rec["hit_limit"] = bool(RE_LIMIT.search(out))
    rec["ok"] = proc.returncode == 0
    rec.update(_progress_from_log(tag, scenario))

    if rec["certified"]:
        print(f"  -> CERTIFIED in {rec.get('solve_s')}s  obj={rec.get('objective')}", flush=True)
    else:
        print(f"  -> not certified; reached obj={rec.get('last_obj')} "
              f"dual_inf={rec.get('last_dual_inf')} "
              f"(tail {rec.get('dual_inf_min_tail')}..{rec.get('dual_inf_max_tail')}) "
              f"at {rec.get('last_s')}s", flush=True)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="weekly", choices=["weekly", "four_week"],
                    help="four_week ranks by progress in a fixed budget, since nothing "
                         "certifies there; weekly ranks by time to certified optimum.")
    ap.add_argument("--week", default="june", choices=[w["name"] for w in C.SEASONAL_WEEKS])
    ap.add_argument("--limit", type=int, default=2400, help="Per-solve TimeLimit, seconds.")
    ap.add_argument("--only-ba", default="WEC_CALN")
    ap.add_argument("--scenario", default="S0")
    ap.add_argument("--no-capex", action="store_true", default=True)
    ap.add_argument("--with-capex", dest="no_capex", action="store_false")
    ap.add_argument("--configs", nargs="*", default=None,
                    help=f"Subset of: {' '.join(CONFIGS)}")
    args = ap.parse_args()

    names = args.configs or list(CONFIGS)
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        raise SystemExit(f"unknown configs: {unknown}. Known: {list(CONFIGS)}")

    rows = []
    out_csv = C.ASTR_RESULTS_DIR / "solver_param_sweep.csv"
    for name in names:
        try:
            rows.append(run_one(name, CONFIGS[name], args.week, args.limit,
                                args.only_ba, args.scenario, args.no_capex,
                                horizon=args.horizon))
        except subprocess.TimeoutExpired:
            rows.append({"config": name, "params": json.dumps(CONFIGS[name]),
                         "ok": False, "hit_limit": True, "certified": False})
            print(f"  -> {name} exceeded the wall-clock guard", flush=True)
        # write after each run so a long sweep is never lost
        pd.DataFrame(rows).to_csv(out_csv, index=False)

    df = pd.DataFrame(rows)
    print("\n" + "=" * 72)
    cols = [c for c in ["config", "certified", "solve_s", "barrier_s", "objective",
                        "last_obj", "last_dual_inf", "dual_inf_min_tail",
                        "last_s", "wall_s"] if c in df.columns]
    print(df[cols].to_string(index=False))
    if not df["certified"].any() and "last_obj" in df.columns:
        r = df.dropna(subset=["last_obj"]).sort_values("last_obj")
        if len(r):
            print("\n  nothing certified; ranked by objective reached (lower is better):")
            for _, x in r.iterrows():
                print(f"    {x['config']:16} {x['last_obj']:.6e}  "
                      f"dual_inf {x['last_dual_inf']:.3g} at {int(x['last_s'])}s")
            print(f"\n  best progress: {r.iloc[0]['config']}")
    if "solve_s" in df.columns and df["certified"].any():
        best = df[df["certified"]].sort_values("solve_s").iloc[0]
        base = df[df["config"] == "baseline"]
        print(f"\n  fastest certified: {best['config']} at {best['solve_s']}s")
        if len(base) and pd.notna(base.iloc[0].get("solve_s")):
            b = float(base.iloc[0]["solve_s"])
            print(f"  speedup vs baseline ({b}s): {b / float(best['solve_s']):.2f}x")
    print(f"\n  wrote {out_csv}")


if __name__ == "__main__":
    main()
