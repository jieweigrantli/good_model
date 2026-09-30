# ASTR2026 Model Specification

California substation-level transmission layer nested inside a WECC balancing-area model.

**Status:** as of 23 September 2026. First computable `P_cong`: +417 kt CO2 for the June
week on a PG&E-only configuration. The full-California model still cannot solve S1 or S2.

---

## 1. Structure

Two tiers in a single linear program.

| Tier | Unit | Count | Source |
|---|---|---|---|
| 1 — balancing areas | WECC BA | 20 outside CA, 6 inside | `Examples/WEC_modified.json` |
| 2 — California substations | substation | 3,044 nodes, 4,528 corridors | GRIP + HIFLD + CEC + OSM |

The six California BAs are `WEC_CALN`, `WECC_SCE`, `WEC_SDGE`, `WEC_LADW`, `WEC_BANC`,
`WECC_IID`. Inside California the BA node represents the rest of that utility's system;
outside California it represents an external system not modelled at substation level.

**CA-internal BA-to-BA edges are removed.** The base WECC graph carries 12 directed
aggregate paths between California BAs (SCE↔CALN 3.0/3.7 GW, SCE↔LADWP 3.8, SCE↔SDG&E 1.3,
BANC↔CALN 2.8, IID↔SCE 0.6, IID↔SDG&E 0.1). With the substation network nested inside the
same BAs those corridors exist twice, and the aggregate copy is always the cheaper route, so
no modelled corridor could bind. The 20 CA↔outside edges (18.1 GW per direction) are kept.

---

## 2. Network physics

Transmission is **NTC pipe-flow**, two opposing directed arcs per corridor:

```
-F̄ℓ - zᵀℓ ≤ fℓ,t ≤ F̄ℓ + zᵀℓ
```

This is a linear surrogate for angle-based DC flow `fℓ,t = bℓ(θi,t − θj,t)`. Pipe-flow
permits transfers that Kirchhoff's voltage law would forbid, so **reported congestion is a
lower bound on physical congestion**. Angle-based DC OPF is future work. The choice keeps
the 8,760-hour co-optimization of dispatch and capacity a linear program at substation
resolution.

**Not represented:** voltage, reactive power, and contingencies. Substation transformer
limits *are* represented (see §6).

---

## 3. Topology reconstruction

WECC withholds transmission topology from the public Anchor Data Set, so connectivity is
reconstructed from line geometry.

**Sources.** GRIP `TransmissionLines` (PG&E, 60/70/115/230/500 kV), CEC statewide lines
(33–500 kV, 6,839 rows), HIFLD. The HIFLD line layer additionally carries `SUB_1`/`SUB_2`
named endpoints for ≥100 kV lines — 86% of 2,171 CA lines have both named. OpenStreetMap
adds 3,981 substation footprints and 43,841 power ways whose shared node ids carry real
connectivity. Both are used; see "Asserted topology" below.

**Method.**
1. Each line part's endpoints snap to a substation within 1,500 m. Model nodes win inside a
   400 m precedence radius; beyond that the nearer of model node and junction substation
   wins. The 1,500 m figure follows published open-data pipelines (Bor et al. 2024).
2. Snapping is **voltage-gated**: an endpoint may only terminate at a site rated at or above
   the line's voltage, or of unknown rating. Ungated, a 230 kV line could terminate at a
   21 kV distribution yard purely because it was nearest.
3. Lines that *pass through* a substation are **split** at it, subject to the same voltage
   gate. Snapping only ever looked at endpoints, so a substation on the wire was invisible:
   among nodes reaching no line at all, the median perpendicular distance to line geometry
   was 44 m while the median distance to the nearest endpoint was 898 m.
4. Remaining endpoints cluster into pass-through nodes; runs of pass-through geometry are
   contracted to substation-to-substation corridors.
5. Contraction emits a **minimum spanning tree** over each run's terminals, weighted by
   routed distance — not all pairs. All-pairs treated a shared corridor as a clique and
   produced edges like San Ramon–Vincent at 499 km, which is not a circuit that exists.

**Site voltage.** GRIP `EDSubstations` report the *secondary* voltage, so Vaca Dixon — a
500/230 kV yard — reads as 12 kV. Voltage gating therefore uses the highest voltage of any
substation record within 400 m, treating a yard as one site.

**Asserted topology.** HIFLD's line layer names both terminal substations (`SUB_1`/`SUB_2`)
and OSM maps substations as polygons and links ways through shared node ids. Both are used:
1,437 corridors are confirmed or asserted by a published source rather than inferred from
proximity. Above 100 kV that is 57–67% of corridors; below 100 kV only 17%, because HIFLD's
layer stops at 100 kV and OSM's coverage there is uneven.

**Synthetic feeds.** 342 tagged radial feeds connect components the data leaves islanded,
each sized to its own peak so it cannot act as a bulk bypass. Without them an islanded node
cannot import, and its demand becomes shortfall that reads as congestion — biasing `P_cong`
toward the finding. `--no-synthetic-feeds` disables them for with/without reporting.

**Resulting graph.** 4,528 corridors, median 4.3 km. One connected component: all 3,044
nodes and 100% of peak demand.

---

## 4. Demand allocation

### 4.1 EV charging — TAZ → block → feeder → substation

Follows Li & Jenn (2024), whose code is in `R_Yanning/`.

1. **TAZ → census block.** Home charging by 2010 block population; workplace and public
   charging by LODES 2019 workplace-area jobs. Renormalised within TAZ.
2. **Block → feeder.** Longest intersection where the block polygon meets a feeder line;
   nearest feeder otherwise. 710,145 blocks against 7,298 feeders — 379,420 (53%) by
   intersection, 330,725 by nearest.
3. **Feeder → substation.** PG&E `FeederDetail.Substation` by name (99% resolved); SCE by
   *geometry*, snapping published substation points to model nodes within 2 km (95%
   resolved). SCE names a yard "Universal 69/12 kV" where HIFLD says "Universal City", so
   name matching alone left 29,889 SCE blocks unresolved.

**Weight fallbacks, so no demand is dropped.** LODES records jobs only at workplace blocks
(259k of 710k), and 811 TAZs holding 14.8% of statewide EV energy had no recorded jobs. A
zero there means "no jobs data", not "no charging". Work weight falls back to population,
then to equal split; home weight falls back to equal split. All 5,423 mapped TAZs sum to
exactly 1 on both weights.

**Territory mask.** Only PG&E and SCE publish feeders, so a nearest-feeder rule outside
their territories hands LADWP, BANC, IID and SDG&E demand to an IOU substation hundreds of
kilometres away. Measured when this was missing: IID's assigned peak fell to zero and SCE's
rose to 36.7 GW against a real ~24 GW. Assignments are therefore masked to the assigning
utility's CEC service territory; 190,139 cross-territory assignments were dropped.

Output: 10,010 TAZ–substation pairs over 3,951 TAZs and 1,227 substations. The remaining
1,503 TAZs — SDG&E and the publicly owned utilities — fall back to k-nearest.

### 4.2 Base (non-EV) load

Measured county hourly demand (NREL OEDI 8562), split within county by census-block
population, aggregated to substations. 2019: 274.9 TWh statewide, 56.77 GW peak on
4 September 15:00.

**Timezone.** The NREL hourly axis is UTC carried without a tz label. Used raw it shifts all
demand 7–8 hours: raw California demand troughs at index hour 11 and peaks at 22, against a
real system that troughs at 03–05 and peaks at 17–19 local. Against PG&E's measured
substation profiles, raw correlates at **r = 0.44** with peak hour 2; converted to
`America/Los_Angeles` it correlates at **r = 0.80** with peak hour 18 and trough hour 3,
both matching the measurement exactly.

**Measured substitution.** Following Li & Jenn, base load is *not* allocated where a utility
publishes it. 895 substations use published month-hour profiles directly — 366 PG&E (kW) and
529 SCE (**amperes**, converted with each substation's published secondary voltage, verified
by inverting `MVA = A × kV × √3` and recovering 2.40→2.4, 4.16→4.3, 12→15.5, 16→22.2). Only
the residual, BA total minus measured, is allocated across the rest, so BA hourly totals are
preserved exactly.

This matters because allocated and published quantities should never be summed and compared.
Our chain gets the California total right to 2% but individual substations wrong by a factor
of ten either way (p10 0.10×, p90 1.88× against measured). After substitution that spread
collapses to p10 1.01×, p90 1.17×, where everything above 1.0 is EV added on top.

---

## 5. Balancing-area assignment

`substation_id → parent_ba` comes from grid data, by precedence:

1. the substation's own `Owner` (HIFLD), or PG&E for GRIP substations — **3,210 of 5,146**
2. the CEC load-serving-entity territory polygon containing it — **1,589**
3. the nearest CEC territory polygon — **347**
4. nearest BA centroid — **0**

This replaces a majority vote over the TAZs that k-nearest happened to attach to a
substation. That vote failed for bulk hubs, which serve no local load: Tesla, a 500 kV PG&E
switching hub, was assigned to `WEC_BANC` by a single TAZ 5.4 km away that ranked it 4th of
4. Measured against `Owner`, PG&E substations were wrong 66% of the time. The new assignment
moves 245 substations (8%) and 3.1 GW.

---

## 6. BA gateways

Every substation touching a ≥230 kV line gateways to its parent BA, sized from **incident
circuit capacity not already represented as a modelled corridor**. Components with no such
line get one gateway sized to their own peak demand and local generation, with no headroom
multiplier — a radial feed cannot carry more than the load it serves.

Sizing off *total* incident capacity counts each line twice, once as the corridor to its
neighbour and again as a radial tie to the BA bus, and the BA node has no internal limit.
That produced 372 GW of gateway capacity against a 63 GW statewide peak (SDG&E: 45 GW
against 5.4 GW), letting flow hop substation → BA bus → substation around any corridor.

Named interties use published WECC path ratings: Path 66/COI 4,800 MW, Path 15 5,400 MW
split across Los Banos and Midway, Path 26 4,000 MW, Palo Verde–Devers 2,800 MW.

**Substation transformer limits are modelled.** `Region` constrains net import (imports
minus exports) to the step-down rating each step, so a switching station passing power
through at transmission voltage is unaffected and only energy actually stepped down to local
load crosses the transformer. Ratings come from published bank data where available (1,215
substations: PG&E bank ratings, SCE GNA/ICA) and are otherwise derived from assigned peak.

This is not a marginal constraint: PG&E's published bank loadings run at a median 85.6% of
rating, 41% above 90% and 19% already above 100%. Without it the model can only find
congestion on transmission lines, which have far more headroom, and would attribute EV
impacts to the wrong asset class.

---

## 7. Corrections not yet reflected in results

Every scenario table produced so far predates all of the following:

- nodal energy balance skipped at demand-only nodes (2,641 of 3,060 nodes, 32% of demand)
- EV baseline folded into base load, making S0 and S1 serve identical demand
- NREL base load shifted 7–8 hours by the timezone bug
- base load allocated at substations where the utility publishes it
- feeder assignment unmasked by service territory
- CA-internal BA-to-BA bypass edges
- gateway capacity oversized ~6×
- TAZ allocation by distance rather than feeder
- BA assignment by TAZ vote
- clique contraction producing phantom 500 km corridors

---

## 8. Known gaps

| Gap | Size | Status |
|---|---|---|
| SCE substation load-profile units | — | resolved: amperes, not power |
| SDG&E feeder and ICA data | 138 nodes, 4.5 GW (7% of peak) | no public endpoint found; request drafted |
| POU data (BANC, LADWP, IID) | 347 nodes, 5.0 GW (8%) | no ICA exists — outside CPUC jurisdiction |
| HIFLD `SUB_1`/`SUB_2` topology | ≥100 kV tier | in use |
| Sub-transmission topology (33–99 kV) | 1,302 nodes, 25 GW | geometry only, no from/to attributes |
| Isolated nodes | 0 | closed by 342 tagged synthetic feeds |
| **S1 will not solve** | — | **two timeouts; blocks `P_cong`** |
| Chronological 8760 from ICA | — | ICA is 288 month-hour bins |
| Dual extraction | — | quadratic rebuild in `region.py::solution()` |

**Structural proxies for POU load do not work.** Tested against 367 PG&E substations with
measured peak: catchment population scores best (log-log r = +0.61, share error 0.27), while
every structural proxy is at or worse than an equal split (incident line capacity 0.51,
corridor count 0.40, equal split 0.40). Population catchment — what k-nearest approximates —
is the recommended fallback.

---

## 9. Scenarios

| Scenario | Description |
|---|---|
| S0 | Nested substations, base load only, no EV |
| S1 | EV at substations, rated NTC, no new BESS |
| S2 | S1 plus endogenous BESS |
| S3 | EV at substations, relaxed (10×) CA NTC and BA interfaces |

```
P_cong  = (E_S1 − E_S0) − (E_S3 − E_S0)
M_BESS  = (E_S1 − E_S0) − (E_S2 − E_S0)
```

---

## 10. Pipeline

| Script | Purpose |
|---|---|
| `08_00_county_base_load.py` | NREL county demand → TAZ base load (UTC→Pacific) |
| `08_01_map_taz_to_substation.py` | k-nearest fallback mapping |
| `08_02_map_block_to_feeder.py` | TAZ → block → feeder → substation |
| `08_03_aggregate_ev_load_at_substations.py` | substation hourly loads |
| `08_04_fetch_ica_data.py` | SCE DRPEP ICA and GNA acquisition |
| `08_05_assign_substation_ba.py` | substation → BA from ownership and territory |
| `09_01_build_ca_meso_grid.py` | corridor reconstruction, gateways, network JSON |
| `09_02_nest_meso_in_good.py` | nest CA network inside WECC graph |
| `10_01_run_scenarios_S0_S3.py` | scenario solves |
| `10_05_plot_network_map.py` | network map (PNG + interactive HTML) |

Reference: `R_Yanning/` holds Li & Jenn's published pipeline.

---

## 11. Solver

Gurobi via `gurobi_direct`, barrier with crossover on.

### June-week results (22 September 2026)

| | S0 (no EV) | S3 (EV, relaxed delivery) |
|---|---:|---:|
| objective | 3.012e9 | 1.281e9 |
| CO2 | 5.555e9 kg | 5.612e9 kg |
| shortfall | 285.2 GWh | 111.9 GWh |
| wastage | 845.9 GWh | 382.3 GWh |
| binding line-hours | 42,097 | 5,534 |
| solve time | 100 min | 159 min |

Relaxing delivery cuts binding line-hours by 87% and shortfall by 61%, which
confirms the transmission constraints in S0 actually bind. **S1 is missing**,
so `P_cong` is not yet computable. S0 and S3 differ in two ways at once (EV
load *and* delivery limits), so the pair alone cannot separate the two.

S0's 285.2 GWh shortfall is a floor present before any EV load: it sits on
the 139 allocated-base substations that exceed their transformer rating, and
those are concentrated where no utility publishes a profile.

### PG&E-only framework test (23 September 2026)

Nesting only `WEC_CALN` at substation level and leaving the rest of California
as copper plates gives 916 nodes and 2,792 edges against 3,060/9,556, roughly a
fifth the LP rows. It also has **zero** under-supplied substations, because all
21 in the full model are SCE or SDG&E. S1 solved in 19.5 minutes here after
failing twice on the full model at 2 h and 4 h.

| | S0 (no EV) | S1 (EV, constrained) | S3 (EV, relaxed) |
|---|---:|---:|---:|
| objective | 8.886e8 | 1.030e9 | 8.706e8 |
| CO2 | 4.888e9 kg | 5.053e9 kg | 4.636e9 kg |
| shortfall | 75.3 GWh | 89.1 GWh | 74.2 GWh |
| wastage | 543.1 GWh | 542.7 GWh | 180.0 GWh |
| binding line-hours | 11,303 | 11,989 | 3,134 |

```
E_S1 - E_S0 = +165,100 t CO2     EV under constrained delivery
E_S3 - E_S0 = -252,300 t CO2     EV under relaxed delivery
P_cong      = +417,400 t CO2     one week, PG&E only
```

`P_cong` is positive, as the hypothesis requires. Three independent signals move
together when delivery is relaxed and none was imposed: binding line-hours fall
74%, wastage falls 67%, and CO2 drops *below* the no-EV baseline -- the same
charging demand absorbs renewable output that constrained delivery had spilled.

**This is a lower bound and a framework test, not a headline number.**
Copper-plating the rest of California removes inter-BA congestion, and PG&E has
the most in-area hydro and renewables of any California BA.

Two results are still missing. **S2 timed out**, so `M_BESS` is unavailable;
endogenous storage couples hours together and crossover handles that far worse
than the purely spatial problem in S0/S1/S3. **Invariant B4 is untested**:
`P_cong > 0` is necessary but not sufficient, and the falsification test is
whether it approaches zero as capacity scales up. S0 also still carries
75.3 GWh of shortfall on PG&E's own under-supplied nodes, so invariant B2 is
not satisfied and some shortfall in every scenario is structural.

## 0. Provenance, fixes, deficits and limitations

One place to see what is measured, what is assumed, and what is missing. Written
so a reader can tell which numbers rest on published data and which rest on a
choice we made.

### 0.1 Data sources

| what | source | used for |
|---|---|---|
| PG&E substations, feeders, transmission lines | GRIP `EDSubstations`, `FeederDetail`, `TransmissionLines` (`data/GRIP_SHP`) | node set, corridor geometry, bank counts |
| PG&E measured substation load | GRIP `SubstationLoadProfile` (`high` band, 288 month-hours, 639 substations) | base load, and the baseload half of the capacity identity |
| PG&E bank ratings | GRIP `DFSubstationArea___PeakFacilityLoadingPercent` (1,236 banks, 694 substations) | fallback transformer rating; **not trustworthy, see 0.3** |
| PG&E ICA detailed results | `grip.pge.com` via the `PresignedURLPROD` feature service; 20 division ZIPs, 12 GB, 2,979 load-side feeders (`08_11`) | headroom half of the capacity identity |
| SCE ICA / GNA | DRPEP ArcGIS (`08_04`) | SCE substation ratings, load profiles |
| Substations, transmission lines (national) | HIFLD (4,442 CA substations; 2,171 CA lines with named endpoints) | non-PG&E nodes, line terminals, **plant switchyards** |
| Power infrastructure (crowd-sourced) | OpenStreetMap via osmium (3,981 substations, 43,841 lines) | corridor confirmation, shared-node chaining |
| Balancing areas, utility territories | CEC GeoPackages | BA assignment, territory masking |
| WECC generators, profiles, **emission factors** | `Examples/WEC_modified.json` | generation, dispatch, per-asset `co2` |
| County electricity demand | NREL | validation of the allocated base load |
| Travel demand, EV charging | CSTDM TAZ, Li & Jenn charging model | EV load |
| Census blocks | US Census | TAZ -> block -> feeder -> substation chain |

### 0.2 Artificial fixes and assumptions

Ordered by how much they could move a result.

| assumption | value | why it exists | risk |
|---|---|---|---|
| ICA line sections -> feeder | **max** across sections | headroom at a section includes upstream impedance, so the section nearest the substation is the bank-relevant one. Min gave 2.8% headroom against GNA's ~20%; max gives 46%; median gives 14% | **high** -- the choice moves overloaded PG&E nodes between 19 and 246. Median is best cross-validated; max is in use. `--section-agg` switches it |
| `TYPICAL_LOADING` | 0.856 | divides assigned peak to derive a rating where nothing is published (1,883 nodes) | **high** -- it is the median of the *maximum* bank loading per substation, used as if it described a substation. The substation-level figure is **0.79**. Not yet corrected |
| generation switchyard rule | plant >= 100 MW, within 2 km, highest voltage wins | plants have their own switchyards and serve no load, so the TAZ test dropped them | medium -- fixes the two dominant cases; 19 of 42 plants >= 300 MW still land below 230 kV |
| nuclear dispatch | forced to 100% of nameplate every hour | `dispatchable=False` applies both a max and a must-run min | medium -- correct as baseload, but it makes curtailment entirely a function of corridor adequacy |
| transmission operating cost | 0.36 $/MWh per corridor | without it a corridor pair is a flat direction and energy cycles A->B->A for free; measured 293 TWh of gross flow against 40 TWh of generation | low -- necessary, and small against gas at ~38 $/MWh |
| `MIN_RATING_W` | 5 MW | floor so a near-zero-load node still has a usable interface | low -- but 165 of 211 derived PG&E ratings sit *on* this floor |
| synthetic feeds | 5 MW, 342 nodes | otherwise isolated nodes shed all their load | low-medium -- fabricated capacity, flagged in output |
| zero-load divide guard | 1 W, 1,611 assets | `profile = profile_W / cap` needs `cap > 0` | low -- presolve removes them |
| BESS capex / duration / efficiency | 2.1 $/W, 4 h, 0.90 | -- | low for the as-built runs, where capex is not charged at all |
| `EV_HOME_SHARE` | 0.68 | split of charging to home | medium, untested here |
| corridor snapping | 1,500 m substation, 400 m hub precedence, 250 m split | reconstruct corridors from line geometry | medium -- MST contraction replaced clique after a 499 km spurious corridor |
| solver | `Method=2`, `Crossover=-1`, `BarConvTol` 1e-8; ScaleFlag, NumericFocus, Aggregate and OptimalityTol at Gurobi defaults | the v1 pins (`ScaleFlag=1` above all) existed to survive the $/J formulation's 13-order coefficient spread, which the GOOD 2.x units removed | low -- but the objective is **not** a convergence diagnostic here; see 0.5 |
| storage duration, existing pumped hydro | 10 h | upstream `migrate.ASSUMPTIONS` (marked PLACEHOLDER); v1 assumed 8 h | low-medium -- a 25% change in pumped hydro energy capacity, inherited rather than chosen |
| VRE curtailment measurement | availability minus production, solar and wind only, excluding energy-budget assets | GOOD 2.x makes solar and wind curtailable `Producer`s, so spill no longer appears as node `wastage` (which is now exactly zero) | medium -- the exclusions matter: counting budget-limited hydro reported 1,932 GWh against a true 40.5 GWh |

### 0.5 Reproducibility of the objective

The objective is dominated by unserved energy priced at a $10,000/MWh value of
lost load, which makes it a poor convergence diagnostic for this model. Five
certified-optimal solves of the identical PG&E March week returned objectives
spanning 0.214% ($572,238):

| configuration | time | objective | shortfall |
|---|---:|---:|---:|
| `Crossover=-1` | 44.8 s | 2.675762e8 | 14.333 GWh |
| `Crossover=1` | 44.0 s | 2.676289e8 | 14.338 GWh |
| `Crossover=1` (repeat) | ~45 s | 2.680771e8 | 14.383 GWh |
| `Crossover=1` (repeat) | 43.7 s | 2.681484e8 | 14.390 GWh |
| `Method=1` (dual simplex) | 53.1 s | 2.678213e8 | 14.358 GWh |

The 57.0 MWh of shortfall these disagree about is worth $570,000 at VOLL on its
own, which is 100.4% of the observed objective spread. Over the same five runs
total CO2 was identical to the printed precision and curtailment agreed to within
0.2%. `Crossover=1` is additionally not reproducible run to run.

Runs should therefore be compared on **CO2, shortfall GWh and curtailment GWh**,
not on the objective. An objective that moves a few tenths of a percent means a
few tens of MWh of shortfall moved.

### 0.3 Data deficits

| deficit | scale | consequence |
|---|---|---|
| PG&E bank ratings contradict PG&E's own load data | substation 02201 sums to 9.88 MVA against a published 118.9 MW peak | the bank layer is a Grid Needs Assessment keyed on `gnaneedid`, not an asset inventory. Demoted to a fallback behind the ICA identity |
| substations with ICA but no measured baseload | 65, incl. SF K and SF L | the identity needs both halves; these fall back to bank sums. ~14 GWh of S0 shortfall |
| nodes with no PG&E identity at all | `HIFLD_*`, ~40 GWh of S0 shortfall | rating is `peak / 0.856`, i.e. invented. The largest single component of residual S0 shortfall |
| coal emission factor | one shared value, 250 g/kWh | real coal is 900-1,000 g/kWh. A fuel-level placeholder, not per-unit data. `ASTR_COAL_CO2` overrides; left unchanged so results trace to the input |
| fossil generators with no `co2` | 43 | fall back to their own fuel's median from the same file; volume printed at runtime |
| HIFLD `Max_Voltag` unreliable | Midway and Gates, both 500 kV Path 15 terminals, are tagged 0 | a voltage filter rejects correct high-voltage endpoints. Why the switchyard rule cannot be tightened further |
| The Geysers under-connected | 776 MW on 405 MW of corridor | real field splits across Fulton and Lakeville (230 kV) and Eagle Rock (115 kV); we model one tap |
| SDG&E ICA | not acquired | SDG&E ratings are derived only. Request email drafted, not sent |
| no distribution layer | -- | Li & Jenn's binding constraint is feeder thermal/voltage on 5,582 feeders. We cannot represent it, and deliberately discard it in the ICA aggregation |

### 0.4 Limitations

| limitation | effect on results |
|---|---|
| **PG&E only**; rest of WECC copper-plated | every congestion number is a **lower bound**. No intra-SCE or inter-BA congestion is visible. Li & Jenn find PG&E worst in *magnitude* but SCE widest in *extent*, so scope omits the broader problem |
| **NTC pipe-flow**, not angle-based DC OPF | no loop flow or Kirchhoff voltage law, so reported congestion is a **lower bound** |
| **current EV adoption only** | `P_cong` is indistinguishable from zero. Li & Jenn's own counts put overload at 14% of feeders in 2022 and 80% by 2045, so this is expected rather than a null result |
| **four seasonal weeks**, not 8760 h | VRE share 5.95% against the annual 6.01%, so the sample is representative. Required by the RPS, which is a budget over the solve horizon |
| **as-built** (`--no-capex`) | no investment can mask a shortage. Matches Li & Jenn's frozen-grid design, but means results are not a forecast |
| **0.10% solver noise floor** (~6,770 t) | any CO2 difference below this is not interpretable. `P_cong` and `M_BESS` both sit at or under it |
| emission factors are CO2, not CO2e | `ch4` and `n2o` are on the assets and not counted |
| RPS has no compliance valve | `non_compliance_capacity` is unset so the RPS is hard. This is why the horizon cannot be decomposed by time |

### Results: corrected plant topology + per-generator emission factors (27 September 2026)

Supersedes every earlier result in this document. Two corrections landed together
and both move the headline numbers by more than a factor of two, so nothing
above this line should be quoted.

**Correction 1: generation switchyards.** `09_01::generation_switchyards` now
keeps a substation as a node when a large plant sits on it, regardless of load.
Previously Diablo Canyon's 2,240 MW was snapped 15.9 km to FOOTHILL, a 12 kV
distribution substation with 300 MW of corridor, and The Geysers' 926 MW to
MIDDLETOWN, 12 kV, 13.6 MW. Verified against the NRC filing and CPUC documents:
Diablo Canyon has its own 230 kV and 500 kV switchyards and five lines (Morro
Bay and Mesa at 230 kV, Midway x2 and Gates at 500 kV), and HIFLD's own line
layer records exactly those five. It now sits on its own 500 kV switchyard with
**10,300 MW** of corridor. Curtailment fell **2,072.6 -> 163.3 GWh (-92%)**.

**Correction 2: per-generator emission factors.** `_emissions_kg` now uses each
asset's published `co2` (kg/J electricity) instead of fuel-level constants that
were 1.9x (gas) to 22x (biomass) too high. Total emissions fall to 31% of the
previous values and the consequential EV factor from 771 to 410 g/kWh.

PG&E only (`--only-ba WEC_CALN`), 946 substations nested, 672 h, `--no-capex`.

| | lines | interfaces | transformers | EV | BESS | CO2 Mt | shortfall | wastage | generation | BESS cycling | dispatched |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | x1 | x1 | published | off | -- | 6.5194 | 74.65 | 164.5 | 59,244 | -- | -- |
| S0R | x10 | x10 | published | off | -- | 6.4894 | 19.94 | 92.2 | 59,205 | -- | -- |
| S1 | x1 | x1 | published | on | -- | 6.7698 | 91.02 | 163.3 | 59,912 | -- | -- |
| S2 | x1 | x1 | published | on | 509 MW / 2,035 MWh | 6.7607 | 91.18 | 112.5 | 60,030 | 6/6 | 144.11 GWh |
| S3 | x10 | x10 | published | on | -- | 6.7413 | 30.13 | 92.1 | 59,906 | -- | -- |
| S4 | x10 | x10 | **x10** | on | -- | 6.7538 | 4.20 | 92.1 | 59,948 | -- | -- |

Shortfall and wastage in GWh; generation in GWh over the 672 h.

**Consequential EV emission factor** (dCO2 / EV energy delivered, where
delivered = 626.8 GWh demanded minus that scenario's incremental shortfall):

| comparison | dCO2 | delivered | EF |
|---|---|---|---|
| S1 - S0, constrained | 250,403 t | 610.4 GWh | **410 g/kWh** |
| S3 - S0R, lines relaxed | 251,861 t | 616.6 GWh | 408 g/kWh |
| S4 - S0R, all relaxed | 264,365 t | 642.5 GWh | 411 g/kWh |

Marginal generation is gas throughout (+692 GWh S1-S0; coal *falls* 28 GWh), so
410 g/kWh is the gas fleet's own factor. The factor being flat across
transmission regimes is the finding: relaxing transmission changes how much EV
energy is delivered, not what generates it.

**Derived metrics, with the noise floor stated:**

```
P_cong (S1-S0)-(S3-S0R) =  -1,458 t     <- indistinguishable from zero
M_BESS (S1-S2)          =   9,173 t     <- barely above the floor
noise floor (0.10% of 6.8 Mt) ~ 6,770 t
```

The 0.10% floor is the spread measured across solver configurations that all
reported "optimal" (see the parameter sweep section).

**At current EV adoption, congestion in PG&E imposes no measurable carbon
penalty.** That is consistent with Li & Jenn, whose own counts give 795 of 5,582
feeders overloaded in 2022 (14%) rising to 4,452 (80%) by 2045. Our 16.4 GWh of
626.8 GWh EV energy undeliverable (2.6%) is the same story one tier up. The
congestion signal is a 2030s-2040s phenomenon, so the obvious next test is EV
adoption scaling rather than more model refinement.

**Storage works but not on carbon.** S2's 6 batteries all cycled ~71 times and
moved 144 GWh, cutting curtailment 31% (163.3 -> 112.5 GWh) while leaving
shortfall unchanged. Storage absorbs spilled zero-carbon energy, loses 10% to
round-trip efficiency, and the replacement is gas. Real curtailment benefit,
negligible carbon benefit. Note this is the third fleet tested: sizing to EV
peak gave 0.85 cycles, sizing to deficit gave 0.08, sizing to curtailment gave
70.8. Placement, not scale, was always the binding issue.

### Why storage does nothing here, and where it would (26 September 2026)

S2's batteries were tested three ways and none of them moved the result. The
reason is physical, not economic, and it took a deliberate experiment to find.

| fleet | sites | discharged | energy | cycles | CO2 saving |
|---|---|---|---|---:|---|
| endogenous (optimiser free to build) | 786 avail. | -- | -- | -- | built only 66 MW |
| prescribed by EV peak, 1,163 MW / 4,652 MWh | 786 | 11 (1%) | 3.97 GWh | 0.85 | 315-502 t |
| prescribed by deficit, 1,069 MW / 6,892 MWh | 115 | 2 (2%) | 0.53 GWh | 0.08 | -1,000 t |

Every saving is inside the 0.10% objective spread measured across solver
configurations, so none is distinguishable from zero.

**The batteries were free.** Under `--no-capex` the capex variable is bounded to
zero and `make_store_asset` sets no operating cost, so `Store.objective` charges
nothing for either building or running them. An LP will take free arbitrage if
any exists. It declined, which means the constraint is not price.

**Storage cannot relieve a delivery constraint, because it needs that
constraint to charge.** A battery must charge before it discharges, and charging
means importing *more* through the path that is already capped. A substation
importing at its limit has no spare hour to fill a battery. The per-node deficit
shape (`shortfall_by_node.csv`, written by `10_01::_shortfall_by_node`) shows
how absolute this is in S0: the five largest deficits are short in **672 of 672
hours**, so no battery of any size at any price helps them. Median longest
contiguous deficit run in S0 is 91 h.

**EV load is what makes storage viable at all**, because EV charging is peaky
where base-load delivery failure is permanent. Adding EV load moves the median
longest deficit run from 91 h to 5 h. Of 97.6 GWh of deficit at ICA-rated nodes,
5,826 MWh sits in runs of 8 h or less and 35,515 MWh in longer runs -- so even
perfectly placed storage reaches about a seventh of the deficit energy. That
ceiling is physical.

**Sizing to the deficit was worse than sizing to EV peak, and the reason is the
finding.** The batteries that actually cycled were at nodes with *no deficit*:
SUB_18295 (2,240 MW nuclear, 1,301 GWh curtailed) and SUB_04314 (926 MW
geothermal, 608 GWh curtailed). They were absorbing stranded must-run
generation. Deficit-sizing deleted batteries from exactly those nodes because
they have no shortfall to size against. **Storage's value in this system is at
the surplus end, not the deficit end.**

So the sizing recommendation is by curtailment, not deficit, and an order of
magnitude larger than anything tested: SUB_18295 and SUB_04314 spill 1,909 GWh
between them, 92% of all curtailment, at roughly 1,900 MW sustained. Both tested
fleets absorb 4.7 GWh per cycle against 2,072 GWh spilled -- about 500x too
small. Useful storage here is GW-scale at two sites, not MW-scale at hundreds.

This also bounds a storage-versus-transmission comparison. The 161 GWh of
corridor-attributable shortfall is not addressable by storage at any price, so
the two are not substitutes for the same problem: wires fix delivery, storage
fixes curtailment, and in this system those sit at opposite ends. The comparison
that *is* meaningful is narrower -- upgrading the corridors out of
SUB_18295/SUB_04314 against storing their output on site, both aimed at the same
1,909 GWh. Note transmission `capex_cost` is 0 throughout `09_02`, so that
comparison needs a corridor cost basis the model does not currently carry.

### As-built four-week results, all four certified (25 September 2026)

First set where every scenario reaches a **certified** optimum on the 672 h
horizon, after `ScaleFlag=1` (see below). PG&E nested, rest of California copper
plate, `--no-capex` so no investment can mask a shortage. Total solve time 2.1 h
for all four, against three prior 4 h attempts that certified nothing.

| | S0 (no EV) | S1 (EV, constrained) | S2 (EV + BESS) | S3 (EV, relaxed) |
|---|---:|---:|---:|---:|
| objective | 1.969748e9 | 2.706243e9 | 2.699162e9 | 1.446984e9 |
| CO2 | 23.0023 Mt | 24.5820 Mt | 24.5815 Mt | 22.0259 Mt |
| shortfall | 128.64 GWh | 196.50 GWh | 195.79 GWh | 79.57 GWh |
| wastage | 2074.1 GWh | 2072.6 GWh | 2071.1 GWh | 618.4 GWh |
| solve | 1,146 s | 680 s | 1,921 s | 3,755 s |

```
EV carbon cost (S1 - S0) = +1,579,730 t CO2
EV unserved  (S1 - S0)   =      67.86 GWh = 10.8% of the ~627 GWh of EV demand
M_BESS       (S1 - S2)   =        502 t CO2  <- within solver noise
P_cong       (S1 - S3)   = +2,556,107 t CO2  <- CONTAMINATED, needs S0R
```

**Forbidding investment multiplies the congestion signal sixfold.** With CAPEX
enabled the same comparison gave only 10.9 GWh of EV-induced shortfall, because
the optimiser simply built 1.27 GW more storage for S1 than for S0. The as-built
framing is what exposes the delivery constraint.

**Distributed storage does nothing measurable here.** S2 prescribes 1,163 MW
across 786 substations -- 17.6x what the optimiser chose to build when free --
and it relieves 0.71 GWh of the 67.86 GWh EV shortfall, about 1%. The resulting
502 t sits inside the 0.10% objective spread measured across solver
configurations, so it cannot be distinguished from zero. The mechanism is
visible in the wastage column: it barely moves between S0 and S1 (2074.1 ->
2072.6 GWh), so there is no surplus for a battery to time-shift. EV shortfall
here is a *delivery* problem, not a timing one, and storage downstream of a
constrained transformer cannot relieve a constraint upstream of it.

`P_cong` remains the contaminated form. S3's wastage collapses from 2072.6 to
618.4 GWh, which is overwhelmingly recovered base-load curtailment rather than
anything EV-related, so the figure still charges EVs for a relaxation that
benefits all load. `S0R` (relaxed transmission, no EV) is implemented in `10_01`
and closes this.

### What the solver taught us, expensively

**Crossover is 98% of the runtime and is where the model fails.** On the S1
attempt that ran four hours, barrier took 258 s and crossover consumed the
remaining 14,140 s without finishing.

**`NumericFocus=2` is what rescued S3.** Its barrier hit the 1,000-iteration
limit without reaching `BarConvTol`, and crossover still recovered to
`Optimal` from that starting point. S1 has only ever run with
`NumericFocus=1` and has failed twice, at 2 h and at 4 h.

**More time does not fix S1.** Doubling its budget bought ~100 extra minutes
of grinding and ended at dual infeasibility 128, no better than the 34.6 it
reached on the shorter run. The signature throughout is dual infeasibility
oscillating over orders of magnitude rather than descending, which is
conditioning rather than slowness. The coefficient spread is the suspect:
shortfall 2.78e-6 $/J against wastage 2.78e-10 is a 1e4 ratio, and
capacities span 1e6 to 1e10 W.

**Crossover cannot be skipped for dispatch.** On this S0, barrier terminated
sub-optimal at 3.0575e9 and crossover corrected it to 3.0118e9, a 1.5%
objective gap. Earlier measurement put the crossover=0 CO2 delta at 147 kt
against 329 kt with crossover, plus 1.2 TWh of phantom generation. This is why
stage 2 keeps crossover on: the energy and emissions totals the study reports
come from stage 2, and an interior point misstates them. Stage 1 skips
crossover because it reads out capacity only, never dispatch, and a few parts
in ten thousand of objective gap does not move a sizing decision.

### Do not decompose the horizon by time

**The concatenated four-week horizon is a requirement of the policy layer, not a
sampling convenience.** `Portfolio_Standard`
(`good/optimization/policies/portfolio_standard.py`) sums generation over
whatever horizon is solved, and `Examples/policies.json` leaves
`non_compliance_capacity` unset, so it defaults to 0 and the escape variable is
clamped. The RPS is therefore a hard constraint over the solve window. Because
wind and solar are non-dispatchable with fixed profiles, the renewable term is
a constant rather than a variable, so the RPS acts as a ceiling on all other
generation, roughly `other <= renewable * (1 - ratio) / ratio`.

Splitting 672 h into four independent 168 h solves converts one seasonal
renewable budget into four weekly ones, and a weak-renewable week can no longer
borrow from a strong one. Measured on identical data and capacity:

| S0 shortfall | four weeks jointly | sum of four weekly solves |
|---|---|---|
| total | **128.5 GWh** | **2,945.7 GWh** |
| worst node | CA substation SUB_02201, 53.0 GWh | WECC_AZ, 580.9 GWh |

The weekly split manufactures a multi-gigawatt Arizona shortfall that does not
exist on the proper horizon, and drives the September EV carbon cost to
-5,061 t (the system saturates, so added EV load becomes shortfall rather than
generation). Arizona's VRE share of its own load is 6.01% annually and the four
seasonal weeks reproduce that at 5.95%, but individual weeks range from 8.40%
(June) to 3.99% (December) -- the four-week sample is representative and single
weeks are not.

Two related facts worth knowing when reading this model: renewables carry
`_class: Load` with positive `installed_capacity` in every balancing area,
which is the injection sign convention and not a data error; and the RPS forces
Arizona to import ~3.2 GW on average, which fits comfortably inside its ~11.2 GW
of intertie capacity on a representative window but not on a stressed week.

### Two-stage solve (superseded -- kept for the record)

The two-stage split below was implemented and then abandoned for the reason
above: its stage 2 dispatched each week independently, which is exactly the
decomposition the RPS forbids. `--stage1-capex-out` and `--fix-capex-from`
remain in `10_01` and are correct as CAPEX plumbing, but stage 2 must not be run
on `--horizon weekly` while ratio policies are active.

The 672 h four-week LP does not solve in one pass. Its barrier stalls: primal
residual plateaus at 1.09e4 and complementarity at ~1.4e-1 for its last 15
iterations, terminating sub-optimal at 347 s. Crossover then ran 2,265,777
iterations in 14,400 s without converging, its objective still at 7.28e9 and
falling against a true optimum near 2.16e9. Crossover cost grows superlinearly
in model size: one week is 314k presolved rows and pushes to a vertex in
~1,180 s, while four weeks is 1.25M rows.

The horizon is therefore split by *decision type* rather than by time:

**Stage 1 (sizing).** All four weeks in one LP, `Crossover=0`,
`BarConvTol=1e-4` via `ASTR_BARCONVTOL`. At a tolerance the model can meet,
barrier terminates `Optimal` in 81-196 iterations and 123-852 s per scenario --
33 min for all four, against a single-stage S0 that failed after 4 h. Only the
capacity decisions are read out, per scenario, to `stage1_capex_<tag>.json`.

**Stage 2 (dispatch).** Each seasonal week separately, `Crossover=1` and the
tight default tolerances, with every extensible asset held at stage 1's build.

Capacity stays co-optimised across all four seasons, which is the reason the
concatenated horizon exists. Only dispatch is separated -- and the claim
originally made here, that dispatch is separable because the weeks are
non-contiguous and carry no storage state between them, **was wrong**. Storage
state is not the only thing coupling the weeks: the RPS is a budget over the
solve horizon, so separating the weeks tightens it into four weekly budgets.
See "Do not decompose the horizon by time" above for the measured cost.

Two implementation points that are easy to get wrong, both caught by
`10_01`'s unit-tested helpers:

* `ev_charging_project.utils.extract_capex_expansion` matches only handles
  beginning `optional_`, but S2's substation batteries are named `bess_<hub>`.
  Reusing it records 16 of S2's 802 extensible assets and lets each week
  re-decide the other 786. `_extract_capex_decisions` keys on `extensible`
  instead.
* The record must be **absolute built capacity**, not the solved `capex`
  delta. S1-S3 run with `_apply_capex_floor_nlg` having already folded S0's
  build into `installed_capacity`, so their `capex` is only the increment above
  S0, while stage 2 rebuilds from the unflooded base. Storing deltas made S0
  report 7,220 MW of storage and S1 report 1,261 MW -- S1 apparently building
  less than S0 despite serving strictly more load. Absolute totals mean the
  same number whichever baseline produced them.

**Cost of the approximation.** Stage 2 cannot re-optimise capacity against the
dispatch it finds, so reported objectives are an upper bound on the true
single-shot optimum: a feasible build, dispatched optimally. The gap is bounded
by stage 1's sizing tolerance (~3e-4 relative), far below the inter-scenario
differences this study reports.

**Accounting.** CO2, shortfall, wastage and generation are extensive in time
and sum over the four weeks. The objective does not: stage 1's CAPEX is an
annual charge incurred once while each stage-2 week carries only operating
cost, so `10_08_aggregate_two_stage.py` sums operating cost and keeps CAPEX
separate rather than counting it four times.

### As-built dispatch (`--no-capex`)

Closes every CAPEX decision so no new capacity can be built, to ask what the
grid as it stands does with the demand. Every extensible asset in WEC.json
carries `installed_capacity = 0` and unlimited `capex_capacity`, so this removes
all 799 of them -- within the WEC_CALN restriction, the 16 BA-level
`optional_storage_*` (the 783 optional solar/wind are already closed upstream by
`_disable_solar_wind_capex`). What survives is the genuinely installed fleet:
4.22 GW of pumped hydro, 0.60 GW of battery, and all real generation.

S2's substation batteries are pure CAPEX too (`installed_capacity = 0`,
everything in `capex_capacity`), so a blanket switch-off would make S2
byte-identical to S1 and force M_BESS to zero by construction. `--no-capex`
therefore moves their per-site cap into `installed_capacity`, giving a fleet
fixed exogenously by `09_01::candidate_bess` (half the substation's mean EV
peak, 1 MW floor). M_BESS then measures the value of a prescribed fleet rather
than of an optimally-sized one, which is a change in what the metric means.

Removing the 16 optional_storage assets also removes the 16 dense columns the
barrier log reported -- a capex variable sits in every hour's capacity
constraint, the classic pathology for Cholesky factorisation. With them gone
crossover starts working: dual pushes complete immediately and primal pushes
drain at ~200,000/s, against a CAPEX-enabled run where crossover never finished
2.27M iterations in 4 h.

### ScaleFlag=1: what actually made `four_week` solvable

**`ScaleFlag=2` was the cause of the four-week convergence failure.** It had been
set in the config from the start, on the reasoning that a model with a nine-order
coefficient spread needs aggressive scaling. It does not: aggressive scaling was
*creating* the instability. Standard scaling certifies the same model in
**1,011.9 s** where `ScaleFlag=2` failed to certify in 14,400 s on three separate
attempts.

672 h, S0, `--no-capex`, identical 2,400 s budget:

| config | certified | objective reached | final dual inf |
|---|---|---|---|
| **`ScaleFlag=1`** | **yes, 1,011.9 s** | **1.969024916e9** | **0** |
| `Quad=1` | no | 1.9964419e9 | 35.9 |
| `ScaleFlag=3` | no | 1.9752436e9 | 15,933 |
| `Presolve=1` | no | 2.0026964e9 | 3,398 |
| `NumericFocus=3` | no | never left barrier (0 simplex iterations) | -- |
| baseline (`ScaleFlag=2`) | no | 2.0094031e9 | 13,400 |

The certified optimum is 2.0% *below* where the baseline had crawled to after
four hours, so those runs were never near-converged -- they were 2% away with
dual infeasibility oscillating over two orders of magnitude. Everything
previously attributed to intrinsic conditioning (barrier stalling on primal
residual, crossover needing 4.1M iterations, dual infeasibility reaching 6.18
then rebounding to 320) was this one parameter.

Two cautions carried out of this. `NumericFocus=3` is unusable at this size: it
made zero simplex iterations in 2,400 s. And Gurobi **appends** to `LogFile`, so
any tool parsing that file must keep only the segment after the last
"Optimize a model" or it will attribute the previous run's progress to the
current one -- which is exactly how `NumericFocus=3` first appeared to match
`Presolve=1` byte for byte.

### Solver parameter sweep, and why concurrent LP does not help

`10_09_solver_param_sweep.py` times Gurobi configurations on one seasonal week,
using the `ASTR_GUROBI_PARAMS` env hook in `10_01::_solver_kw`. The motivation
was that barrier iterations here are cheap (1.23 s each, Factor Ops 5.8e8) while
the crossover clean-up ran ~4.1M **single-threaded** simplex iterations, so the
phase consuming 3.5 of 4 hours used one of 14 cores.

June week, S0, `--no-capex`, time to certified optimum:

| config | solve | speedup | objective |
|---|---|---|---|
| `Method=3, Aggregate=1` | 325.9 s | 2.89x | 471234294.3 |
| `Method=3` (concurrent) | 350.4 s | 2.69x | 471107962.3 |
| `Sifting=2` | 792.6 s | 1.19x | 470987284.5 |
| `Aggregate=1` | 864.4 s | 1.09x | 471453455.1 |
| baseline | 941.3 s | 1.00x | 471234287.5 |

**The 2.89x does not carry to 672 h.** At four weeks, concurrent is *worse*:
barrier takes 1,916 s instead of 1,234 s because 14 cores are split three ways,
and crossover then starts from further back. At matched wall time the
non-concurrent run was at 1.9997e9 where concurrent was at 2.0323e9. The
one-week gain came from a simplex method winning the race outright; at four
weeks neither simplex finishes, so concurrency only starves barrier. Use the
default `Method=2` on `four_week`.

Two by-products of the sweep worth keeping. Certified-optimal objectives span
470987284.5 to 471453455.1 across configurations -- a **0.10% spread** that sets
a floor on any difference worth interpreting (the EV carbon cost S1-S0 is 2.25%
of total, comfortably above it). And `Aggregate=0`, long set in the config, is a
deviation from Gurobi's default of 1 that turns out to buy only 1.09x, so it was
never the problem it looked like.

### Barrier tolerance: do not loosen it

`BarConvTol` measures the complementarity gap, not primal feasibility, and on
this model those come apart. At `1e-4` barrier quits at iteration 87 with a
primal residual of 6.70e6 -- a primal-infeasible point that Gurobi discards,
leaving crossover to restart primal simplex from scratch (objective 1.46e8,
primal infeasibility 2.44e15). At the `1e-7` default it runs its full 1,000
iterations and reaches a primal residual of 1.41e1, six orders of magnitude
better, which is what lets crossover push properly.

Raising `BarIterLimit` does not help: over iterations 989-1000 the primal
residual oscillates between 1.4e1 and 1.7e1 with no downward trend. Barrier is
stalled on primal feasibility, not short of iterations. Its dual bound is also
unreliable here -- it reported a 5.8e-5 relative gap at 2.0513e9 while crossover
went on to find 1.9895e9, 3% lower.

The sizing-tolerance exception in the superseded two-stage section applies only
to a pass that reads out capacity and never dispatch.

### Settings

`TimeLimit` defaults to 4 h (override with `ASTR_TIME_LIMIT_S`), 24 h for the
8760 horizon. `OptimalityTol` 1e-5, `FeasibilityTol` 1e-6, `BarConvTol` 1e-7,
`ScaleFlag` **1** (see above -- the 2 that `_solver_kw` still hardcodes is the
setting that broke `four_week`; pass `ASTR_GUROBI_PARAMS='{"ScaleFlag":1}'` until
the default is changed), `NumericFocus` 1 (2 for S3). Duals are disabled
(`extract_duals=False`); enabling them doubled memory and caused a MemoryError
at 12M variables.

Results carry a `graph_build` fingerprint and `built_at` timestamp so rows
from an older network cannot be compared silently against a newer one.

Costs (all $/J; 1 MWh = 3.6e9 J): shortfall 10,000 $/MWh, wastage 1 $/MWh,
transmission operating 0.36 $/MWh. Derivations in
`cost_calibration_methodology.tex`.

## 12. Migration to GOOD 2.x

The transmission layer now runs on the GOOD 2.x core (branch `v2-migration`;
the 1.x production state is frozen at tag `1.1.3`). GOOD 2.x replaced the Pyomo
model with a vectorized linopy/xarray one and moved from SI units to power
system units. This section records what changed here, because several of the
changes move results rather than just the code.

### Why the units changed

GOOD 1.x worked in watts, joules and $/J. On this model that put objective
coefficients across ~13 orders of magnitude (7e-10 to 1e3) against right-hand
sides up to 9e13, which is why the v1 solver configuration needed
`ScaleFlag=1`, a loosened `OptimalityTol` and per-scenario `NumericFocus` just
to terminate. GOOD 2.x uses MW, MWh and $/MWh throughout.

Measured effect on the PG&E-only March week (962 nodes, 3,090 edges, `--no-capex`):

| | GOOD 1.x | GOOD 2.x |
|---|---:|---:|
| build | several minutes | 6.2 s |
| solve to certified optimal | ~19.5 min | 44 s |
| solver parameters pinned | 9 | 4 |

The v1 tuning is *not* carried over unexamined. `ScaleFlag`, `NumericFocus`,
`Aggregate` and `OptimalityTol` are back at Gurobi's defaults, since each was
compensating for a coefficient spread that no longer exists.
`10_09_solver_param_sweep.py` should be re-run on the four-week horizon before
any of them is pinned again.

### Where the conversion happens

Scripts `08_*` and `09_*` still build in v1 units and the conversion happens
once, at the solver boundary, through upstream's own `good.migrate.from_v1()`
(called from `astr_v2.to_v2`). Converting here rather than rewriting every
builder was deliberate: `from_v1` is the tested converter, the meso tier and the
base WECC tier get converted by the same pass so they cannot disagree, and
upstream's data repairs come for free (1,360 trailing-colon profile keys fixed,
2,010 unresolvable profile references dropped, hydro normalisation, and the
25-hour-day wind repair).

Three things sit outside `from_v1` and `astr_v2.to_v2` handles them after it:

1. **`transformer_capacity`** is a node attribute, and `from_v1` converts only
   assets, lines and profiles. Left alone it would arrive in watts and be read as
   MW, a factor of 1e6 that would make every substation transformer limit
   non-binding — silently, since a slack constraint raises nothing.
2. **Storage durations.** `from_v1` replaces every storage spec with its own EPA
   Platform v6 assumptions. The ASTR batteries are sized by `08_13`/`08_14` with
   an explicit `--duration-h`, so that has to be put back or the sizing flag does
   nothing. Note upstream assumes 10 h for existing pumped hydro where v1 assumed
   8 h.
3. **Nuclear baseload.** Was `prepare_graph`'s job in v1; no upstream equivalent.

Profiles are also snapshotted and restored, because `from_v1` assumes 8,760-hour
input: it pads short profiles to a full year and renormalises hydro over that
padded year. Hydro is therefore normalised in `10_01` *before* slicing, over the
real year. Run after slicing, `hydro_shape` would divide by the mean of 168 real
hours and 8,592 copies of the 168th, scaling the hydro fleet by a factor that
changes with whichever week was chosen.

### Results changes that are real, not porting artifacts

**Solar and wind became curtailable.** `from_v1` reclassifies 2,188 VRE assets
(55,169 MW) from must-take `Load` to `Producer` with `dispatchable=True`. This is
upstream fixing a v1 modelling error, and it changes two headline quantities:

* **Curtailment moved.** In v1, surplus VRE had nowhere to go and appeared as
  node `wastage`. Now the optimiser simply does not produce it, and node wastage
  is **exactly zero** across all 962 nodes. Curtailment is therefore measured
  in `_vre_curtailment` as availability minus production
  (`capacity_factor x profile x installed_capacity - production`), and
  `wastage_GWh` reports dumped surplus plus VRE curtailment so it stays
  comparable with the v1 results. The two components are broken out in
  `shortfall_wastage.json`.
* **The RPS numerator changed**, because the renewable generation counted toward
  each state standard is now a decision rather than a given.

Two exclusions in the curtailment measurement are load-bearing. Only solar and
wind count, and nothing carrying an `energy_budget_window` counts: `from_v1`
gives all 1,360 hydro units a 24-hour energy budget, so hydro routinely produces
below hourly availability because it is holding water, not spilling it.
Including it reported 1,932 GWh of curtailment on a week where the real VRE
figure is a fraction of that.

### Fixes to our own inputs that v2 exposed

* **Link/line handle collision.** `09_02` named each `Link` and its single
  `Transmission` identically. GOOD 2.x keeps one global handle namespace, so all
  3,022 lines on a PG&E week were rejected as duplicates. Links now use the base
  WECC graph's own `source:target` convention.
* **Edge key.** `good.graph.graph_from_nlg` hardcodes `edges="links"`, but every
  graph file here stores edges under `"edges"` — which worked in v1 only because
  that call passed no key and NetworkX 3.4+ defaults to `"edges"`.
  `astr_v2.graph_from_nlg` reads the key off the data.
* **`deep_reload(good)` removed.** GOOD 2.x resolves components through a
  registry populated at import and checks membership with `issubclass`. A deep
  reload rebinds every class, so the registry holds pre-reload classes while the
  graph is validated against post-reload ones: all 13,771 components were
  rejected as "not a Node, Edge, Asset, Line or Policy subclass", which reads
  like a schema failure rather than a stale import.
* **Policies.** GOOD 2.0 replaced lambda-string `inclusion_criteria` with
  declarative `include`/`exclude` filters. `migrate.convert_policies()` does the
  translation at load time, so `Examples/policies.json` stays in its published
  form and the 32 state RPS ratios remain traceable to it.

### Unit-facing changes to result files

Result CSVs are now in MW and MWh. Columns were renamed rather than silently
rescaled, so a stale file fails loudly instead of being read as the wrong
magnitude: `energy_J`→`energy_MWh`, `mean_W`→`mean_MW`,
`co2_kg_per_J`→`co2_kg_per_MWh`, `capacity_W`→`capacity_MW`,
`mean_flow_W`/`peak_flow_W`→`_MW`, `shortfall_J`/`wastage_J`→`_MWh`,
`discharge_Wh`→`discharge_MWh`, `planned_W`/`built_W`→`planned_MW`/`built_MW`.
The `peak_hour_J` column is gone, since peak power is now read directly.

`ASTR_COAL_CO2` is now in kg/MWh, not kg/J.

Builder-side columns (`installed_capacity_W`, `interface_capacity_W`,
`capex_capacity_W`) stay in watts: they are inputs to `from_v1`, not results.
The two-stage capex files are in MW, and `_fix_capex_nlg` /
`_apply_capex_floor_nlg` convert back to watts on the way into the graph.

### Validation against the certified 1.x results

Four-week PG&E-only S0 and S1, `--no-capex`, against the certified 1.x numbers.
The middle column is before the relocated-plant profile fix described below, and
is shown because it is what isolates that bug's effect.

| | | 1.x certified | 2.x before fix | 2.x | vs 1.x |
|---|---|---:|---:|---:|---:|
| S0 | CO2 | 6.5194 Mt | 6.290 Mt | 6.4170 Mt | −1.57% |
| | shortfall | 74.65 GWh | 74.74 GWh | 74.75 GWh | **+0.14%** |
| | spill | 164.5 GWh | 201.8 GWh | 169.1 GWh | +2.78% |
| S1 | CO2 | 6.7698 Mt | 6.531 Mt | 6.6546 Mt | −1.70% |
| | shortfall | 91.02 GWh | 91.10 GWh | 91.11 GWh | **+0.10%** |
| | spill | 163.3 GWh | 200.6 GWh | 167.9 GWh | +2.81% |
| | EV delta | 250,400 t | 241,000 t | 237,587 t | −5.1% |

Solve time fell from 1,012 s to 292 s for S0; S1 took 374 s.

Shortfall reproduces to within 0.14%, which is the result that matters most:
shortfall is where the transmission ratings and substation transformer limits
show up, so it is the part of the model this project exists to measure. CO2 and
spill each sit a consistent offset away in both scenarios (−1.6% and +2.8%),
which points at a systematic modelling difference rather than noise — the VRE
reclassification, which changes both the curtailment decision and the RPS
numerator.

### A bug this validation exposed

Solar produced 395 GWh *above* its own availability, which should be impossible.
The cause was in `09_02`, not in the migration: 39 VRE plants (588 MW) given
their own switchyards referenced a profile key held on their parent BA node, and
the nesting code copied profiles from the *substation's* BA. A switchyard's BA
comes from its location while a generator carries its own from the WECC data, so
SCE plants do legitimately land on PG&E switchyards, and their profile key was
not on the node. GOOD 2.x resolves a missing profile to a flat 1.0 in every hour,
so those 39 solar plants ran at nameplate through the night: 588 MW x 672 h =
395.1 GWh of phantom renewable generation, counted into the RPS numerator and
displacing fossil.

Profiles now follow the generator. The fix raised CO2 by 2.0% and cut spill by
16%, which took the gap against 1.x from −3.5% to −1.6% on CO2 and from +23% to
+2.8% on spill, while leaving shortfall unchanged. The defect predates the
migration; 1.x simply did not turn an unresolved profile into nameplate output,
so it stayed invisible.

The 2,029 profile references that still do not resolve are all `generator:`,
`storage:` and `geothermal:` placeholders present in `WEC_modified.json` itself.
For a dispatchable thermal unit, no profile is the correct bound.

### Why spill is dumped surplus on the four-week horizon and curtailment on a week

The four-week runs report 169 GWh of spill, all of it dumped surplus, with VRE
curtailment at exactly zero. The single March week reports the reverse: 40.5 GWh,
all VRE curtailment, nothing dumped.

Both are consistent with the RPS being a horizon-summed ratio. Over four seasonal
weeks it binds hard enough to hold every VRE unit at full output, so surplus that
cannot be delivered has to be dumped at the node. Over a single March week —
California's high-hydro, high-wind season — the RPS is slack and the model is free
to curtail VRE instead. This is why `wastage_GWh` reports the sum of the two: the
physical quantity is the same and only the mechanism the optimiser reaches for
changes with the horizon.

### Full v2 scenario results

All six scenarios, four-week horizon (672 h), PG&E-only nesting, `--no-capex`,
corrected topology and per-generator emission factors. 30 September 2026.

| scenario | CO2 Mt | shortfall GWh | spill GWh | solve |
|---|---:|---:|---:|---:|
| S0 no EV | 6.4170 | 74.753 | 169.077 | 292 s |
| S0R no EV, relaxed | 6.3817 | 19.896 | 91.502 | 275 s |
| S1 EV, constrained | 6.6546 | 91.111 | 167.890 | 374 s |
| S2 EV + BESS | 6.6457 | 91.085 | 125.785 | 390 s |
| S3 EV, lines relaxed | 6.6229 | 30.080 | 91.465 | 323 s |
| S4 EV, lines + transformers | 6.6338 | 4.146 | 91.465 | 287 s |

```
P_cong  (S1 - S3, corridors only)           = +31,700 t
P_cong' (S1 - S4, corridors + transformers) = +20,737 t
M_BESS  (S1 - S2)                           =  +8,830 t
```

**`P_cong` is now resolved, where in 1.x it was not.** The 1.x result was −1,458 t:
the wrong sign, and inside a noise floor then estimated at ~6,770 t. Two repeat
four-week S1 solves differ by **177 t of CO2** (0.0027%), so `P_cong` sits 179x
that floor, `P_cong'` 117x and `M_BESS` 50x.

The floor is far tighter than the 1.x estimate because that estimate came from
objective variation, and the objective is dominated by shortfall priced at VOLL
(see 0.5). The same repeat pair differs by $459,871 in objective and 46 MWh in
shortfall, and 46 MWh x $10,000/MWh = $460,000 — so essentially all of the
objective variation is unserved energy, which barely touches the generation mix
and therefore barely touches CO2. Comparing runs on CO2 rather than the objective
is what makes these differences resolvable at all.

### Consequential EV emission factor

Each EV scenario against the no-EV baseline of its own delivery regime, so the
non-EV benefit of relaxing transmission cancels rather than being charged to EVs.
Delivered energy is the 626.75 GWh of EV demand added, less the extra shortfall
the EV load itself causes.

| | baseline | ΔCO2 | delivered | EF |
|---|---|---:|---:|---:|
| S1 | S0 | 237,587 t | 610.4 GWh | 389.2 g/kWh |
| S2 | S0 | 228,757 t | 610.4 GWh | 374.8 g/kWh |
| S3 | S0R | 241,159 t | 616.6 GWh | 391.1 g/kWh |
| S4 | S0R | 252,122 t | 642.5 GWh | 392.4 g/kWh |

Flat at roughly 389 g/kWh across delivery regimes, against 410 g/kWh in 1.x —
consistent with the systematic −1.6% CO2 offset. Storage is the only case that
moves it materially, to 374.8.

**S4 emits more than S3 in absolute terms** (6.6338 against 6.6229 Mt) despite
relaxing strictly more. That is a delivered-energy effect and not an
inconsistency: relaxing the substation transformers lets S4 serve 642.5 GWh of EV
load where S3 serves 616.6 GWh and sheds the rest. Per kWh the two agree to 0.3%.

### Storage in S2

The fleet is re-sized against the v2 spill figures by `08_14`, since the
curtailment file it reads is one this migration changed: 8 substations, 550 MW,
2,198 MWh at 4 hours.

| | 1.x | 2.x |
|---|---:|---:|
| fleet | 1,163 MW | 550 MW |
| batteries that cycled | 6 of 6 | 8 of 8 |
| fleet cycles over 672 h | 70.8 | 71.7 |
| discharged | 144.11 GWh | 157.59 GWh |
| spill reduction (S1 → S2) | 50.8 GWh | 42.1 GWh |

Comparable work from under half the power rating, because every node in the new
fleet has a real discharge window: `hours_free` runs 104 to 509 across the eight,
where in 1.x the two nodes holding 92% of the spill had `hours_free = 0` and their
batteries could charge but never discharge. Charged energy (175.10 GWh) exceeds
discharged by 10.0%, which is the round-trip loss and confirms the 0.90 efficiency
survived `from_v1`'s storage override.

### S5: transmission sized to observed overload

S3 and S4 multiply every corridor by ten. That answers "what is corridor
congestion costing if delivery were free", but it is a diagnostic and not a
build, so the benefit it reports cannot be costed. **S5** replaces it for
reporting: `08_15_size_transmission_to_overload.py` reads S3's per-corridor peak
flows and sets each corridor to the capacity the unconstrained optimum actually
wanted, exactly as `08_14` sizes the battery fleet from observed curtailment.

The rule is Li & Jenn's (2024), adopted because it is the published method this
project is anchored to:

* **Upgrade need = maximum overload over the horizon.** Their feeder rule is
  `max_t(baseload + EV - capacity)` clipped at zero, taken at the single worst
  hour with no averaging. The corridor equivalent is
  `max_t(flow) - capacity` read off a run where that corridor was not binding.
* **Only the overload is costed**, not replacement of the whole line.
* **Congestion is reported in their two metrics** so the numbers are comparable
  with their feeder results: *overload intensity* = peak wanted / rating, and
  *overload frequency* = binding hours / all hours.

One difference is structural. Li & Jenn can subtract, because feeder load is
given. Flow on a meshed network is a decision, so the "load" a corridor would
carry has to come from a solve in which it was free — which is why the 10x
scenarios are kept as the diagnostic that sizes S5, rather than deleted.

Measured on the four-week PG&E run:

| | |
|---|---:|
| corridors needing an upgrade | 158 of 3,090 (5.1%) |
| capacity actually required | 11,838 MW |
| capacity the 10x relaxation grants | 6,522,044 MW |
| fraction of the grant that does work | **0.182%** |
| overload intensity | median 1.76, max 10.0 |
| overload frequency | median 0.21, max 1.00 |

**S5 reproduces S3 exactly**: identical shortfall (29.9905 GWh), identical spill
(91.4654 GWh) and an identical objective to the cent ($924,170,694.48). That is a
consequence rather than a coincidence — sizing each corridor to
`max(base, S3 peak)` makes S3's operating point feasible in S5, and S5's feasible
region is a subset of S3's, so the S3 optimum is S5's optimum. The whole
corridor-congestion benefit is therefore available from **11,838 MW rather than
6.5 TW**, and `P_cong` becomes costable.

CO2 differs by 325 t (0.005%) between the two, which is the degenerate-optimum
spread at equal total cost and supersedes the earlier 177 t noise-floor estimate
(that pair predated the gateway fix below). Against a 325 t floor, `P_cong` sits
97x, `P_cong'` 65x and `M_BESS` 28x.

### Indicative cost comparison

Annualising the four seasonal weeks by 13 gives 410,241 t/yr for the transmission
build and 116,935 t/yr for the storage fleet.

| | overnight | annualised | $ per t |
|---|---:|---:|---:|
| transmission, 11,838 MW @ $240/kW (25th pct), 40 yr | $2.84B | $71M/yr | **$173** |
| transmission @ $456/kW (median) | $5.40B | $135M/yr | $329 |
| transmission @ $800/kW (75th pct) | $9.47B | $237M/yr | $577 |
| storage, 550 MW @ $1,977/kW (EPA 4-hour), 15 yr | $1.09B | $72M/yr | **$620** |

Storage is cheaper to build and more effective per MW (16.35 against 2.67 t/MW,
6.1x) but more expensive per tonne avoided, because the transmission build lasts
40 years against 15 and carries no round-trip loss.

**Four caveats, and the first is the one that matters.** The per-kW transmission
bracket is Li & Jenn's, and theirs is for **distribution feeder** projects from
PG&E's Distribution Investment Deferral Framework. The corridors here are
sub-transmission and transmission, so applying it is a placeholder; a
transmission-appropriate $/MW-mile source with line lengths from `09_01` would
replace it. Second, the 13x seasonal extrapolation assumes the four weeks
represent the year. Third, nothing is discounted — these are straight-line
annualisations, not levelised costs. Fourth, `M_BESS` is itself an upper bound
because cycling is unpriced, so storage's $/t is if anything understated.

### Part 1: maximum CO2 saving potential by solution

Three interventions, and only two of them save emissions. Four-week horizon,
annualised by 13.

| solution | ceiling, 4 wk | annualised | how the ceiling is established |
|---|---:|---:|---|
| corridor upgrades | **31,557 t** | 410,241 t/yr | S5's 11,838 MW reproduces the 10x relaxation exactly, so this is proven and not an estimate |
| substation storage | **12,846 t** | 166,998 t/yr | a 10x fleet (5,496 MW) absorbs all 93.7 GWh of substation spill; nothing further is reachable |
| transformer upgrades | **−10,825 t** | −140,725 t/yr | raises emissions; see below |

**Transformer upgrades do not abate carbon.** S4 relieves the substation banks on
top of S3's corridors and emissions *rise* by 10,825 t, because it serves 25.93
GWh more EV load that S3 sheds, at 417.4 g/kWh. The benefit is avoided unserved
energy, which is a reliability and service-quality result, and it should be
reported on that axis rather than as a negative abatement.

**Storage saturates against the spill, transmission does not.** The 550 MW fleet
removes 42.1 of the 93.7 GWh of substation spill; the 5,496 MW fleet removes all
93.7 GWh and the 74.17 GWh that remains sits entirely on WECC_IID, a balancing-area
node with no substation battery and therefore outside the layer storage can reach.
So 12,846 t is a hard ceiling for storage sited this way, against 31,557 t for
corridors — storage can capture at most **41%** of what corridor relief captures,
and only by building ten times the power.

### Part 2: build range and levelised cost per tonne

Costed by `10_10_cost_transmission_vs_storage.py`, which replaces the earlier
per-kW placeholder. That placeholder used Li & Jenn's bracket, which is drawn from
PG&E's **distribution feeder** projects, while most of what 08_15 wants upgraded
is sub-transmission. Each corridor is now costed from its own voltage, length and
required circuit count.

#### What the sizing turns out to contain

| class | arcs | MW | circuits | route-miles | overnight |
|---|---:|---:|---:|---:|---|
| BA interface (substation capacity) | 42 | 4,916 | — | — | $0.37–1.23B |
| 115 kV corridor | 30 | 2,714 | 42 | 526 | $0.37–0.68B |
| 60 kV corridor | 48 | 2,024 | 74 | 657 | $0.29–0.50B |
| 230 kV corridor | 8 | 1,046 | 34 | 228 | $0.62–1.00B |
| 70 kV corridor | 11 | 813 | 25 | 181 | $0.13–0.23B |
| distribution voltage | 18 | 247 | 44 | 91 | $0.06–0.20B |
| 200 kV corridor | 1 | 78 | 1 | 42 | $0.05–0.09B |
| **total** | **158** | **11,838** | **220** | **1,726** | **$1.89–3.93B** |

Two features of that table matter more than the total.

**42% of the build is not a line.** 4,916 MW sits on 42 BA interfaces, which are
not corridors at all: 09_01 derives an interface capacity from the external
transmission a voltage gateway sees *beyond* the modelled network, so there is no
route, no length and no voltage. They are costed as substation capacity at
$75,000–250,000 per MVA and flagged, because a per-mile figure would be fiction.

**The build is sub-transmission, not bulk transmission.** 60–115 kV carries 4,738
of the 11,838 MW against 1,124 MW at 200–230 kV, which is why the feeder-cost
placeholder was wrong in both directions — too expensive per MW for the
sub-transmission bulk, and the right source only for the 18 arcs at 12 and 4.16 kV
(247 MW), where it is now used and only there.

Terminal equipment — two positions for each of 220 new circuits — is $205M, 8–14%
of the line total.

#### Levelised cost

Levelised with a capital recovery factor rather than straight-line, because the
assets have very different lives and straight-line flatters the longer one. At 5%
real, CRF is 0.0583 over 40 years against 0.0963 over 15, a ratio of 1.65, where
straight-line annualisation implied 40/15 = 2.67 in transmission's favour.
**Discounting therefore narrows transmission's advantage rather than widening it.**

| | overnight | levelised | $/t at 5% |
|---|---:|---:|---:|
| transmission, low | $1.89B | $110M/yr | **$268** |
| transmission, high | $3.93B | $229M/yr | **$558** |
| storage, 550 MW | $1.09B | $105M/yr | **$896** |

Sensitivity to the rate:

| rate | transmission low | transmission high | storage |
|---|---:|---:|---:|
| 3% | $199 | $415 | $779 |
| 5% | $268 | $558 | $896 |
| 7% | $345 | $719 | $1,021 |
| 10% | $470 | $980 | $1,223 |

Transmission is cheaper per tonne than storage at every rate tested and at both
ends of its cost range. The storage supply curve below still matters, though: its
cheapest 223 MW is a better buy than its full fleet, and at 5% that subset is
around $660/t against $896 for all 550 MW.

#### Does the corridor upgrade need transformer upgrades?

No, for the emissions benefit. S5 delivers the full 31,557 t with every substation
transformer left at its published rating, which is what makes it a proof rather
than an estimate. Relaxing the transformers as well (S4) does not add abatement —
it *raises* emissions by 10,825 t, because it serves 25.93 GWh of EV load that S3
sheds. So step-down transformer capacity is a separate decision, justified by
served load rather than by carbon, and it is deliberately excluded from the cost
above.

The build does already carry substation work, though, in two places: the 4,916 MW
of BA-interface capacity is substation and gateway capacity rather than line, and
the $205M of terminal equipment is bays, breakers, instrument transformers and
protection at both ends of every new circuit.

What is **not** costed, in rough order of how much it could move the total:
undergrounding, which California urban segments can require at roughly ten times
overhead cost and which would dominate if it applied to any material share of the
1,726 route-miles; land and right-of-way beyond what the per-mile figures embed;
distribution reinforcement downstream of the substations, which is outside this
model entirely and is precisely what Li & Jenn measure; and protection and
relaying changes on adjacent circuits that a capacity change can force.

#### Storage supply curve



Storage has a genuine supply curve, because each battery's discharge displaces gas
directly and can be attributed. Capex at $1,977/kW (EPA Platform v6, 4-hour),
15-year straight-line.

| cumulative storage | CO2, 4 wk | share of ceiling | marginal $/t |
|---|---:|---:|---:|
| 83 MW | 2,825 t | 22% | $298 |
| 182 MW | 5,383 t | 42% | $394 |
| **223 MW** | **6,283 t** | **49%** | **$459** |
| 264 MW | 6,793 t | 53% | $910 |
| 337 MW | 7,445 t | 58% | $1,143 |
| 550 MW | 8,995 t | 70% | $1,396 |
| 5,496 MW | 12,846 t | 100% | $13,022 |

Returns fall away sharply. The first 223 MW carries half the ceiling at under
$459/t; the last 4,946 MW carries 30% of it at $13,022/t, which is 21x worse per
MW than the first 550 MW and far beyond any plausible carbon value.

Transmission's *capacity* is not a range — 11,838 MW is the proven minimum
sufficient build — so its range comes from unit cost. The build is concentrated:
the top 25 of 158 corridors are half of it and the top 50 are 78%, split 42 BA
interfaces (4,916 MW) and 116 substation corridors (6,921 MW).

| solution | build | overnight | life | $/t |
|---|---|---:|---:|---:|
| corridors @ $240/kW (25th pct) | 11,838 MW | $2.84B | 40 yr | **$173** |
| corridors @ $456/kW (median) | 11,838 MW | $5.40B | 40 yr | $329 |
| corridors @ $800/kW (75th pct) | 11,838 MW | $9.47B | 40 yr | $577 |
| storage, efficient subset | 223 MW | $0.44B | 15 yr | **$459** |
| storage, full fleet | 550 MW | $1.09B | 15 yr | $620 |
| storage, ceiling | 5,496 MW | $10.86B | 15 yr | $1,042 |

Corridor upgrades are cheaper per tonne across their whole cost range than storage
is at any build size, and they reach 2.5x the ceiling. Storage's best subset
($459/t for 223 MW) is competitive only with the upper half of the transmission
cost range.

Caveats, in the order they could change the conclusion. The per-kW transmission
bracket is Li & Jenn's and is drawn from **distribution feeder** projects, while
these corridors are sub-transmission and above — it is a placeholder, and it is
doing much of the work in the comparison above. The four seasonal weeks are
extrapolated by 13. Nothing is discounted. And `M_BESS` is an upper bound because
cycling carries no cost, so storage's $/t is if anything understated.

### Storage against transmission: why the two are not substitutes

On every absolute measure the transmission relaxation does more than the battery
fleet:

| vs S1 | ΔCO2 | Δshortfall | Δspill |
|---|---:|---:|---:|
| S2, 550 MW storage | 8,830 t | 0.026 GWh | 42.1 GWh |
| S3, corridors x10 | 31,700 t | 61.031 GWh | 76.4 GWh |

Storage is more efficient per unit of capacity, though, and that is the
interesting part. S3 is granted 6,521,774 MW of extra corridor capacity but its
peak flows exceed the S1 ratings on only 155 of 3,084 corridors, by 11,830 MW in
total — **0.181% of the grant**. Against the capacity that actually does work:

| | CO2 saved | capacity | per MW |
|---|---:|---:|---:|
| storage | 8,830 t | 550 MW | 16.05 t/MW |
| transmission | 31,700 t | 11,830 MW needed | 2.68 t/MW |

so storage is about **6x more effective per MW** (3,303x against the untargeted
10x grant, which is an artifact of the diagnostic rather than a result).

Three things explain it.

**Both interventions act only on natural gas.** Wind, solar, nuclear and
geothermal generation is identical to the decimal across S1, S2 and S3 —
10,256.4, 3,398.9, 4,943.9 and 1,965.4 GWh — because the horizon-summed RPS holds
them at full output in every case. Neither intervention adds renewable energy;
both work by delivering energy that would otherwise be dumped, displacing gas.
The arithmetic closes on the fuel shift alone: S2's −22.6 GWh gas, −1.9 coal and
−0.9 biomass imply 8,969 t against a measured 8,830 t, and S3's −82.8 GWh gas,
+3.9 coal, −5.7 oil and −4.9 biomass imply 30,842 t against 31,700 t. So the spill
is the ceiling on both, and whichever intervention converts spill to delivered
energy most cheaply wins on efficiency.

**Storage is precisely sited and the transmission relaxation is not.** `08_14`
places all 550 MW at exactly the eight nodes that spill, each with a real
discharge window. S3 multiplies every corridor by ten whether it binds or not, so
99.8% of the grant is slack.

**They relieve different constraints, which is why storage cannot replace
transmission.** Storage moves energy in time at one node; it cannot move energy
between nodes. S2 cuts shortfall by 0.026 GWh out of 91.111 — essentially nothing
— while S3 cuts it by 61.031 GWh. None of the eight battery nodes is one of the 99
nodes carrying shortfall, and the five largest deficit nodes are short in 672 of
672 hours, so a battery there could never charge in the first place. The deficit
is an import limit, and only a bigger import path fixes it.

Storage also pays a large efficiency toll on the spill it captures: 175.10 GWh
charged against 157.59 GWh discharged, so the 17.51 GWh of round-trip loss is 42%
of the 42.1 GWh of spill it removes.

**`M_BESS` is an upper bound.** The prescribed batteries carry no
`operating_cost`, so cycling is free, and the fleet runs 71.7 cycles in four weeks
— about 2.6 a day, against roughly one a day for real grid storage. EPA Platform
v6 gives new batteries $7.1/MWh of variable O&M, which on 157.59 GWh of discharge
would be $1.12M against roughly $0.86M of avoided gas fuel cost. A sensitivity run
with `operating_cost` set would bound `M_BESS` from below; until then the 8,830 t
should be read as the most storage could do, not what it would do.

### Not yet done

`10_02_compute_P_cong_M_BESS.py` keys on the bare `four_week` tag and reports
`incomplete` for `four_week_WEC_CALN`, so the metrics above are computed directly
from the per-scenario outputs rather than by that script.
