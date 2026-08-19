# ASTR2026 transmission / meso layer (scripts 08–10)

Implements the nested California delivery layer described in
`ASTR/ASTR2026_Li_Jenn/ch3_transmission_bottleneck_extended_abstract.tex`.

## Prerequisites

- Unzipped PG&E GRIP Shape at
  `data/GRIP_SHP/GRIP_SHP/GRIP_SHP/GRIP_SHP/`
  (`EDSubstations.shp`, `TransmissionLines.shp`, …)
- Multiday outputs in `multiday_charging/outputs/`
- Repo-root GOOD + Gurobi for `10_01` solves
- `geopandas`, `sklearn` (optional; quantile fallback exists)

## Run order

```bash
cd Distribution-Grid-EV-CA
python 08_01_map_taz_to_substation.py
python 08_02_hourly_ev_load_by_taz.py
python 08_03_aggregate_ev_load_at_substations.py
python 09_01_build_ca_meso_grid.py
python 09_02_nest_meso_in_good.py
python 10_03_select_stressed_weeks.py
# from repo root (GOOD imports):
cd ..
python Distribution-Grid-EV-CA/10_01_run_scenarios_S0_S3.py
python Distribution-Grid-EV-CA/10_02_compute_P_cong_M_BESS.py
python Distribution-Grid-EV-CA/10_04_plot_diagnostic_seasonal.py
```

Optional: `python .../10_01_run_scenarios_S0_S3.py --seasons june --dry-run`

## Outputs

| Path | Contents |
|------|----------|
| `data/mapping/taz_to_substation.csv` | TAZ → substation |
| `data/meso/` | Hub graph, seasonal loads, nested JSON |
| `astr_meso_results/` | S0–S3 solves, `P_cong_M_BESS.csv` |
| `figures/ASTR_diagnostics/` | Diagnostic PNGs |
| `ASTR/.../ch3_transmission_bottleneck_manuscript.tex` | Full draft paper |

## Notes

- If CEC/HIFLD downloads fail, `08_01` adds BA gateway proxies so southern CA
  TAZs are not forced onto distant PGE substations.
- Nested physics use capacity-constrained transfers (not DC angles).
- Default meso resolution is **one node per substation** (`SUB_<id>`). Optional
  screening aggregation: `python 09_01_build_ca_meso_grid.py --aggregate 80`.
- S2 BESS candidates default to every node with EV peak > 0; set
  `ASTR_BESS_TOP_N=5` to restore selective siting.
