# ASTR2026 transmission / meso layer (scripts 08–10)

Unclustered California substation delivery layer nested in the WECC GOOD model,
as specified in the ASTR2026 transmission-layer execution plan.

Two tiers:

1. **WECC balancing areas** outside California (existing GOOD graph).
2. **Unclustered substations** inside California (~704 PG&E `EDSubstations` plus
   HIFLD non-PG&E substations). No k-means / hub clustering.

Primary production solve is a **single-shot 8760-hour LP** per scenario S0–S3
(pipe-flow / NTC constraints, no integer variables). On a 32 GB workstation the
**4-week concatenated seasonal LP** (672 h, one week each from Mar/Jun/Sep/Dec)
is the feasibility test and the Stage-1 CAPEX fallback. Do **not** launch
`--horizon 8760` until that 4-week run completes.

## Prerequisites

- Unzipped PG&E GRIP Shape at
  `data/GRIP_SHP/GRIP_SHP/GRIP_SHP/GRIP_SHP/`
  (`EDSubstations.shp`, `TransmissionLines.shp`, …) — no re-extraction step
- TAZ centroids: `data/shps/TAZ_centroid_sf.gpkg` (reprojected to EPSG:3310)
- Multiday outputs in `multiday_charging/outputs/`
- Repo-root GOOD + Gurobi for `10_01`
- `geopandas`, `pyarrow` (parquet)

## Run order

```bash
cd Distribution-Grid-EV-CA
python 08_01_map_taz_to_substation.py
python 08_02_hourly_ev_load_by_taz.py
python 08_03_aggregate_ev_load_at_substations.py
python 09_01_build_ca_meso_grid.py
python 09_02_nest_meso_in_good.py
python 10_03_select_stressed_weeks.py

# from repo root (GOOD imports). Default horizon is the 4-week test — not 8760.
cd ..
python Distribution-Grid-EV-CA/10_01_run_scenarios_S0_S3.py --horizon four_week
python Distribution-Grid-EV-CA/10_02_compute_P_cong_M_BESS.py
python Distribution-Grid-EV-CA/10_04_plot_diagnostic_seasonal.py
```

Full-year production path (do not run until 4-week is feasible):

```bash
python Distribution-Grid-EV-CA/10_01_run_scenarios_S0_S3.py --horizon 8760
```

If 8760 memory exceeds ~24 GB, Stage 1 is the 4-week CAPEX solve (`Z*` fixed)
and Stage 2 is 12 monthly dispatch LPs with `Z = Z*` (see `10_01` docstring).

Dry-run graph construction only: `--dry-run`.

## Outputs

| Path | Contents |
|------|----------|
| `data/mapping/taz_to_substation.csv` | TAZ, substation_id, source, distance_m |
| `data/meso/taz_hourly_ev_8760.parquet` | TAZ 8760 EV load (kW) |
| `data/meso/substation_hourly_loads_8760.parquet` | \(L^{total}_{s,t}\) (kW) |
| `data/meso/ca_substation_network.json` | Unclustered nodes, NTC edges, generators, interties |
| `data/meso/wecc_ca_nested_graph.json` | Nested WECC + CA graph (June template profiles) |
| `data/results/S0_S3_four_week_runs.parquet` | 4-week decision-variable summaries |
| `data/results/S0_S3_8760_runs.parquet` | Full-year summaries (after 8760 solve) |
| `data/results/summary_metrics_8760.json` | \(P_{cong}\), \(M_{BESS}\), binding corridors |
| `figures/ASTR_diagnostics/` | Heatmaps, binding-line maps, metric bars, diurnal mix |

## Modeling notes

- Spatial work uses **EPSG:3310** (CA Albers). PG&E TAZs map only to
  `EDSubstations.shp`; non-PG&E TAZs map only to HIFLD substations.
- Non-EV BA load is downscaled with housing/employment weights \(w_s\):
  \(L^{total}_{s,t} = w_s \cdot L^{base}_{BA,t} + L^{EV}_{s,t}\).
- Line limits use rated MVA when present, else
  \(500\,\mathrm{kV}\approx 1500\,\mathrm{MVA}\),
  \(230\,\mathrm{kV}\approx 400\,\mathrm{MVA}\),
  \(115\,\mathrm{kV}\approx 150\,\mathrm{MVA}\).
- Physics are continuous pipe-flow / NTC
  (\(-\bar F_\ell \le f_{\ell,t} \le \bar F_\ell\)), so the 8760 problem is an LP.
- S0 nested substations with base load only; S1 constrained EV, no new BESS;
  S2 endogenous BESS at substations with EV peak; S3 10× CA delivery ratings.
- Gurobi: `Method=1` (dual simplex), `NodefileStart=0.5`, MPS I/O.
