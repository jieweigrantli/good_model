# Python port of the Distribution-Grid-EV-CA pipeline

This folder now contains Python (`.py`) translations of every `.R` script in the
original project, plus a `common.py` module with shared helpers.

## Mapping: R → Python

| R script                                          | Python script                                                       |
| ------------------------------------------------- | ------------------------------------------------------------------- |
| `01_01_get_LDV_households_per_TAZ.R`              | `01_01_get_LDV_households_per_TAZ.py`                               |
| `02_01_map_tract_to_bg_2010.R`                    | `02_01_map_tract_to_bg_2010.py`                                     |
| `02_02_get_EV household share_per_bg_TAZ.R`       | `02_02_get_EV_household_share_per_bg_TAZ.py`                        |
| `02_03_sample_EV hh.R`                            | `02_03_sample_EV_hh.py`                                             |
| `02_04_get_EV trips.R`                            | `02_04_get_EV_trips.py`                                             |
| `03_01_draw_charging events_SD.R`                 | `03_01_draw_charging_events_SD.py`                                  |
| `03_02_charging events_LD_ETM.R`                  | `03_02_charging_events_LD_ETM.py`                                   |
| `04_01_get_share_block vs TAZ.R`                  | `04_01_get_share_block_vs_TAZ.py`                                   |
| `04_02_split_charging events_TAZ to block_yearly new.R` | `04_02_split_charging_events_TAZ_to_block_yearly_new.py`      |
| `04_03_map_block to feeder.R`                     | `04_03_map_block_to_feeder.py`                                      |
| `05_02_sample_charging events_distance bins_IOU blocks.R` | `05_02_sample_charging_events_distance_bins_IOU_blocks.py`   |
| `06_01_sum_charging profiles.R`                   | `06_01_sum_charging_profiles.py`                                    |
| `07_05_grid_EV.R`                                 | `07_05_grid_EV.py`                                                  |
| `PNAS_05_03_plot_empirical data.R`                | `PNAS_05_03_plot_empirical_data.py`                                 |
| `PNAS_08_03_plot_HWP.R`                           | `PNAS_08_03_plot_HWP.py`                                            |
| `PNAS_08_04_plot_overload_spatial.R`              | `PNAS_08_04_plot_overload_spatial.py`                               |
| `PNAS_08_05_plot_overload_intensity.R`            | `PNAS_08_05_plot_overload_intensity.py`                             |
| `PNAS_08_06_plot_overload_frequency.R`            | `PNAS_08_06_plot_overload_frequency.py`                             |
| `PNAS_08_07_plot_overload_hour.R`                 | `PNAS_08_07_plot_overload_hour.py`                                  |
| `PNAS_08_09_plot_case study.R`                    | `PNAS_08_09_plot_case_study.py`                                     |
| `PNAS_08_10_plot_headroom vs overload.R`          | `PNAS_08_10_plot_headroom_vs_overload.py`                           |
| `PNAS_09_01_plot_upgrade cost.R`                  | `PNAS_09_01_plot_upgrade_cost.py`                                   |

Shared helpers live in [`common.py`](common.py).

## Library mapping

| R library / function        | Python equivalent                                        |
| --------------------------- | -------------------------------------------------------- |
| `data.table`                | `pandas`                                                 |
| `fread` / `fwrite`          | `pd.read_csv` / `DataFrame.to_csv`                       |
| `saveRDS` / `readRDS`       | `common.save_rds_like` / `common.read_rds_like` (pickle) |
| `tidyverse` / `dplyr`       | `pandas`                                                 |
| `sf` + `geopandas`          | `geopandas`                                              |
| `st_read`, `st_write`       | `gpd.read_file`, `GeoDataFrame.to_file`                  |
| `st_transform(x, crs = y)`  | `gdf.to_crs(y)`                                          |
| `st_join` + `st_nearest_feature` | `geopandas.sjoin_nearest`                           |
| `ggplot2`                   | `matplotlib.pyplot`                                      |
| `ggsave`                    | `fig.savefig`                                            |
| `dcast(df, A ~ B, value.var = "v")` | `df.pivot_table(index=A, columns=B, values="v")` |
| `melt(dt, id.vars=…)`       | `pd.melt(df, id_vars=…)`                                 |
| `setnames(df, "a", "b")`    | `df.rename(columns={"a": "b"}, inplace=True)`            |
| `.N` (within group)         | `df.groupby(...).size()`                                 |
| `shift(x)` (by group)       | `df.groupby(...)["x"].shift()`                           |
| `quantile(x, probs = q)`    | `np.quantile(x, q)`                                      |

## Running the pipeline

The scripts expect the same **relative** data layout as the R pipeline:

```
data/
  mobility data/…
  mapping/…
  grid data/…
  charging data/…
figures/
  PNAS/…
```

Run them in the order implied by their numeric prefix, e.g.:

```bash
python 01_01_get_LDV_households_per_TAZ.py
python 02_01_map_tract_to_bg_2010.py
python 02_02_get_EV_household_share_per_bg_TAZ.py
python 02_03_sample_EV_hh.py
python 02_04_get_EV_trips.py
python 03_01_draw_charging_events_SD.py
python 03_02_charging_events_LD_ETM.py
python 04_01_get_share_block_vs_TAZ.py
python 04_02_split_charging_events_TAZ_to_block_yearly_new.py
python 04_03_map_block_to_feeder.py
python 05_02_sample_charging_events_distance_bins_IOU_blocks.py
python 06_01_sum_charging_profiles.py
python 07_05_grid_EV.py
python PNAS_05_03_plot_empirical_data.py
python PNAS_08_03_plot_HWP.py
python PNAS_08_04_plot_overload_spatial.py
python PNAS_08_05_plot_overload_intensity.py
python PNAS_08_06_plot_overload_frequency.py
python PNAS_08_07_plot_overload_hour.py
python PNAS_08_09_plot_case_study.py
python PNAS_08_10_plot_headroom_vs_overload.py
python PNAS_09_01_plot_upgrade_cost.py
```

## Notes

- **`.rds` → `.pkl`**: intermediate datasets produced by R are read/written as
  pickles on the Python side. If you still have the original R `.rds` files you
  can read them with [`pyreadr`](https://github.com/ofajardo/pyreadr) and save
  them as `.pkl` the first time through.
- **Geospatial steps** (`04_03_map_block_to_feeder.py`,
  `PNAS_08_04_plot_overload_spatial.py`) rely on `geopandas`. Install it via
  `pip install geopandas` (or `conda install -c conda-forge geopandas`).
- **Maps & shapefile plotting** from the PNAS plots that require `ggplot2 +
  sf` (intricate choropleths, multi-layer shapefile rendering) were simplified
  to the chart/stat parts of the figures; the geographic overlays can be added
  using `geopandas`' `.plot()` once you have the shapefiles.
- **R random seeds** (`set.seed(42)`, `set.seed(12)`, etc.) are emulated using
  `numpy.random.default_rng(seed)`, but the sampling algorithms differ
  slightly between the two languages. Results will be statistically equivalent
  but not bit-identical to the R outputs.
- A number of R blocks are exploration / sanity checks (`head(...)`, `summary(...)`,
  `setdiff(...)`). These were either dropped or kept as `print(...)` calls
  where they were informative.
