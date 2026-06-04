"""
02_03_sample_EV_hh.py — Python port of ``02_03_sample_EV hh.R``.

Samples new EV households per TAZ per year for each trip source.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

IN_DIR = "data/mobility_data/CSTDM_processed"
SHARE_CSV = "data/mobility_data/EV_Toolbox/evhh_share_TAZ.csv"
YEARS = list(range(2020, 2045))
SEED = 42


def _expand_years(count_tbl: pd.DataFrame) -> pd.DataFrame:
    return pd.concat(
        [count_tbl.assign(year=y) for y in YEARS], ignore_index=True
    )


def _calc_new_evhh(count_tbl: pd.DataFrame, share_tbl: pd.DataFrame, join_cols) -> pd.DataFrame:
    out = pd.merge(count_tbl, share_tbl, on=join_cols)
    out["n_EVhh"] = out["n_hh"] * out["EVhh_share"]
    out = out.sort_values(["HomeZone", "year"])
    out["n_EVhh_newf"] = out.groupby("HomeZone")["n_EVhh"].diff()
    out.loc[out["year"] == YEARS[0], "n_EVhh_newf"] = out["n_EVhh"]
    out["n_EVhh_new"] = np.floor(out["n_EVhh_newf"]).astype("Int64")
    return out


def sample_evhh(taz_hh: pd.DataFrame, taz_evhh_count: pd.DataFrame) -> pd.DataFrame:
    """Port of R ``sample.EVhh`` — samples hhIDs without replacement within a
    HomeZone, cumulatively across years (drawn hhIDs are removed from the pool).
    """
    pool = taz_hh[taz_hh["HomeZone"].isin(taz_evhh_count["HomeZone"].unique())].copy()
    rng = np.random.default_rng(SEED)
    drawn_ids: set = set()
    rows: list[pd.DataFrame] = []
    for y in YEARS:
        ycount = taz_evhh_count.loc[
            taz_evhh_count["year"] == y, ["HomeZone", "n_EVhh_new"]
        ]
        merged = pd.merge(pool, ycount, on="HomeZone")
        sampled_parts = []
        for zone, g in merged.groupby("HomeZone", sort=False):
            n = int(g["n_EVhh_new"].min())
            n = max(0, min(n, len(g)))
            if n == 0:
                continue
            idx = rng.choice(len(g), size=n, replace=False)
            pick = g.iloc[idx][["SerialNo"]].rename(columns={"SerialNo": "hhID"})
            pick["HomeZone"] = zone
            sampled_parts.append(pick)
        if sampled_parts:
            ydraw = pd.concat(sampled_parts, ignore_index=True)
            ydraw["year"] = y
            rows.append(ydraw)
            drawn_ids.update(ydraw["hhID"].tolist())
            pool = pool.loc[~pool["SerialNo"].isin(drawn_ids)].copy()
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["HomeZone", "hhID", "year"])


def main() -> None:
    sd = pd.read_csv(os.path.join(IN_DIR, "SDPTM_hh_home_TAZ.csv"))
    ld = pd.read_csv(os.path.join(IN_DIR, "LDPTM_hh_home_TAZ.csv"))
    ld_ae = pd.read_csv(os.path.join(IN_DIR, "LDPTM_AccEgr_hh_home_TAZ.csv"))
    etm = pd.read_csv(os.path.join(IN_DIR, "ETM_hh_home_TAZ.csv"))
    share = pd.read_csv(SHARE_CSV)

    ca_share = (
        share.groupby("year", as_index=False)
        .agg(EVhh_CA=("EVhh_TAZ", "sum"), hh_CA=("hh_TAZ", "sum"))
    )
    ca_share["EVhh_share"] = ca_share["EVhh_CA"] / ca_share["hh_CA"]

    def _count_by_zone(df: pd.DataFrame) -> pd.DataFrame:
        return df.groupby("HomeZone", as_index=False).size().rename(columns={"size": "n_hh"})

    sd_count = _calc_new_evhh(
        _expand_years(_count_by_zone(sd)),
        share[["year", "TAZ", "EVhh_share"]],
        join_cols=[("year", "year"), ("HomeZone", "TAZ")] if False else None,
    ) if False else None

    # Use pd.merge with left_on/right_on for clarity
    def _make_count(df, share_df, by_taz=True):
        c = _expand_years(_count_by_zone(df))
        if by_taz:
            out = pd.merge(c, share_df, left_on=["year", "HomeZone"], right_on=["year", "TAZ"])
        else:
            out = pd.merge(c, share_df, on="year")
        out["n_EVhh"] = out["n_hh"] * out["EVhh_share"]
        out = out.sort_values(["HomeZone", "year"])
        out["n_EVhh_newf"] = out.groupby("HomeZone")["n_EVhh"].diff()
        out.loc[out["year"] == YEARS[0], "n_EVhh_newf"] = out.loc[
            out["year"] == YEARS[0], "n_EVhh"
        ]
        out["n_EVhh_new"] = np.floor(out["n_EVhh_newf"]).astype("Int64")
        return out

    sd_count = _make_count(sd, share[["year", "TAZ", "EVhh_share"]])
    ld_count = _make_count(ld, share[["year", "TAZ", "EVhh_share"]])
    ld_ae_count = _make_count(ld_ae, share[["year", "TAZ", "EVhh_share"]])
    etm_count = _make_count(etm, ca_share[["year", "EVhh_share"]], by_taz=False)

    # Long distance
    evhh_ld = sample_evhh(ld, ld_count)
    evhh_ld.to_csv(os.path.join(IN_DIR, "EVhh_new_LDPTM_sample42.csv"), index=False)

    # Short distance
    evhh_sd = sample_evhh(sd, sd_count)
    evhh_sd.to_csv(os.path.join(IN_DIR, "EVhh_new_SDPTM_sample42.csv"), index=False)

    # Long distance access/egress
    evhh_ld_ae = sample_evhh(ld_ae, ld_ae_count)
    evhh_ld_ae.to_csv(os.path.join(IN_DIR, "EVhh_new_LDPTM_AccEgr_sample42.csv"), index=False)

    # ETM
    evhh_etm = sample_evhh(etm, etm_count)
    evhh_etm.to_csv(os.path.join(IN_DIR, "EVhh_new_ETM_sample42.csv"), index=False)


if __name__ == "__main__":
    main()
