# Multi-Scale Grid Modeling: Literature Review and Comparison to ASTR2026

**Purpose.** ASTR2026 nests an unclustered California substation network (Tier 2) inside a
WECC balancing-area model (Tier 1). This document reviews how the published literature
handles that same two-scale problem — bulk system above, sub-transmission and distribution
below — and compares each approach to what our pipeline currently does.

**Scope.** Thirteen studies and modeling frameworks, selected to cover four distinct
strategies for the scale problem: distribution-only, bulk-only, co-simulation, and
single-model nesting. California-specific work is prioritized.

**Date.** September 2026.

---

## 1. The central finding

**No reviewed study models both the balancing-area scale and the distribution scale at full
resolution inside one optimization.** Every study resolves the tension in one of four ways:

| Strategy | What is sacrificed | Representative work |
|---|---|---|
| **A. Distribution-only, no transmission** | Bulk dispatch, generation mix, emissions, inter-regional transfer | Li & Jenn (2024) |
| **B. Bulk-only, load as exogenous nodal injection** | Feeder/substation deliverability | PyPSA-USA; WECC ADS; Powell et al. (2022) |
| **C. Co-simulation, two solvers exchanging boundary variables** | Global optimality; a single objective function | HELICS; Wallison et al. (2025); LA100 |
| **D. Single model, one network, aggressive spatial reduction** | Local congestion detail below the reduction threshold | Glista et al. (2026); Frysztacki et al. (2021) |

ASTR2026 is attempting **D at an unusually fine resolution** — 3,044 CA nodes inside a
single LP — while inheriting a **B-style** copperplate BA bus above it. That combination is
defensible and, at our node count, well-supported by the spatial-resolution literature. But
it creates one specific failure mode the literature warns about directly, discussed in §4.1.

---

## 2. The studies

### 2.1 Li & Jenn (2024), PNAS — *the anchor paper*

**Impact of electric vehicle charging demand on power distribution grid congestion.**
PNAS 121(18) e2317599121.

The closest work to ours in subject, and the **mirror image in method**.

- **Spatial unit:** distribution feeder. >5,000 feeders and >1,600 substations across PG&E,
  SCE and SDG&E.
- **Capacity data:** utility **Integration Capacity Analysis (ICA)** maps (PG&E, SCE) and
  SDG&E Grid Needs Assessment — these give real per-feeder capacity *and* hourly load
  profiles.
- **Load allocation:** CSTDM trips → TAZ → census block, with home charging allocated by
  population and workplace/public charging by job density.
- **Network model:** **none.** No power flow, no optimization. The method is a
  capacity-threshold comparison: add projected EV load to feeder baseload, test whether the
  hourly sum exceeds feeder capacity.
- **Transmission:** **not modeled at all.** No balancing area, no bulk dispatch.
- **Headline result:** 67% of California feeders need upgrades by 2045; 25 GW of upgrades;
  $6–20B.

**Relevance to us.** Li & Jenn get *real* per-feeder capacity from ICA and spend nothing on
network topology. We do the opposite — we reconstruct topology from GIS and have no real
capacity data below 100 kV. The two approaches are complementary rather than competing, and
their ICA dataset is the single most valuable validation target available to us (§4.4).

### 2.2 Glista, Knueven & Watson (2026) — *the quantitative case for our resolution*

**From zonal to nodal capacity expansion planning: spatial aggregation impacts on a
realistic test-case.** arXiv:2510.23586.

- **Test system:** California Test System (CATS), **8,795 buses**.
- **Method:** geographic distance-based bus merging at thresholds from 1 km to 100 km,
  treating each merged region as a copperplate zone.
- **Findings:**
  - Coarsest model (244 buses): **up to 41% capacity under-investment**; 36% under-investment
    and 43% objective error (~$4.5B/yr) in the stochastic case.
  - Fine models (7,092 buses, 1 km threshold): error under 1%.
  - Errors are driven specifically by **overestimated transmission deliverability** in
    aggregated zones.

**Relevance to us.** This is the strongest published argument *for* what ASTR2026 is doing.
Our 3,044 CA nodes sit between their 244-bus (41% error) and 7,092-bus (<1% error) cases.
It also names our exact failure mode: a copperplate zone overestimates deliverability. Our
BA bus is a copperplate zone.

### 2.3 Frysztacki, Hörsch, Hagenmeyer & Brown (2021)

**The strong effect of network resolution on electricity system models with high shares of
wind and solar.** Applied Energy 291:116726.

- Interpolates between a 37-node model (country/synchronous-zone boundaries) and a
  **1,024-node model based on substation locations**.
- **Low-resolution models ignore congestion and underestimate system cost by 23%.**
- High-resolution siting of wind and solar reduces cost by up to 10%.
- Cost underestimation is worst when grid expansion is constrained.

**Relevance to us.** Independent confirmation, on a different continent and a different
model, that substation-resolution networks change results by tens of percent — not a
second-order refinement. Also validates using substations (not administrative boundaries) as
the node definition, which is our choice.

### 2.4 Tehranchi, Barnes, Frysztacki & Azevedo — PyPSA-USA

**PyPSA-USA: a flexible open-source energy system model and optimization tool for the
United States.** SSRN 5029120; github.com/PyPSA/pypsa-usa.

- Bulk transmission model of the US, configurable at **zonal, nodal, or blended** resolution.
- Supports capacity expansion, production cost simulation, and power flow.
- Interconnections linked by DC ties.

**Relevance to us.** The closest thing to an off-the-shelf alternative to our Tier 1. Its
"blended" mode — nodal in the region of interest, zonal elsewhere — is *exactly* the ASTR2026
architecture, which suggests our design is a recognized pattern rather than an improvisation.
Worth examining directly for how they handle the zonal/nodal seam.

### 2.5 Powell, Cezar, Azevedo et al. (2022), Nature Energy

**Charging infrastructure access and operation to reduce the grid impacts of deep electric
vehicle adoption.** Nature Energy 7:932–945.

- Data-driven model of EV charging demand across the **entire US Western Interconnection**.
- Peak net demand rises up to 25% under forecast adoption, 50% under full electrification.
- Finds that locally optimized control and high home charging *strain* the grid, while
  uncontrolled daytime charging reduces storage need, ramping and emissions.
- **Network:** bulk/zonal. Distribution is not resolved.

**Relevance to us.** The WECC-scale EV counterfactual for our S0/S1 comparison. Their
counter-intuitive control result is a caution: charging-control conclusions are sensitive to
the scale at which the network is represented, so our S1/S2 results should be compared
against theirs explicitly.

### 2.6 NREL LA100 (2021)

**The Los Angeles 100% Renewable Energy Study.** NREL/TP-6A20-79444.

- Models LADWP generation, transmission **and distribution** — the most detailed integrated
  California study to date.
- Achieved by **integrating more than a dozen separate tools**, not one model.

**Relevance to us.** The most important precedent, and a warning. LA100 covers one
vertically-integrated utility and still required a dozen coupled tools. ASTR2026 attempts
statewide coverage in a single LP. That is only tractable because we make a strong
simplification — linear pipe-flow, no voltage, no reactive power — which should be stated
explicitly as the price of the single-model approach.

### 2.7 HELICS — Palmintier et al. (2017)

**Design of the HELICS high-performance transmission–distribution–communication–market
co-simulation framework.** IEEE MSCPES.

- Co-simulation framework scaling to 100,000+ federates.
- **The T–D boundary is handled explicitly:** each distribution circuit acts as a load on
  transmission; transmission acts as supplier to distribution. Shared variables at boundary
  buses are **voltage magnitude and angle, plus real and reactive load**, with co-iteration
  to convergence.

**Relevance to us.** HELICS defines the T–D interface as a *negotiated, iterated* boundary.
Our BA↔substation gateway is the same concept but solved in one shot as a capacity-limited
LP arc. That is a legitimate simplification — but it means the **gateway rating is doing all
the work that co-iteration does in HELICS**, which is precisely why mis-sizing it (as we
found: 372 GW against a 63 GW peak) corrupted every congestion result.

### 2.8 Wallison et al. (2025)

**Electric Vehicle Integration using Large-Scale Combined Transmission and Distribution Grid
Models.** arXiv:2503.17545.

- Combined T&D simulation over the Houston/Dallas–Fort Worth area.
- 96 urban and long-haul truck charging demand simulations feed the combined model.
- Used for cost-optimal siting and sizing of truck charging infrastructure.
- Explicitly motivated by the gap that prior EV integration work uses *simplified* systems.

**Relevance to us.** Same diagnosis of the literature gap that motivates ASTR2026, solved by
co-simulation over a metro area rather than optimization over a state.

### 2.9 Bor, Oughton, Peters, Rivera, Gaunt, Weigel & Wiltberger (2024)

**A Reproducible Method for Mapping Electricity Transmission Infrastructure for Space Weather
Risk Assessment.** arXiv:2412.17685.

- Builds a national transmission graph from **HIFLD line geometry + OpenStreetMap substation
  locations** (~60,000 substations).
- Substations are nodes, lines are edges, joined by **spatial snapping with a defined
  distance threshold**; voltage is used to classify the infrastructure hierarchy.
- Related open-data pipelines report a **1,500 m snap distance** with a **250 m substation
  buffer**, and — critically — **insert synthetic nodes at unmatched line endpoints** rather
  than discarding them.

**Relevance to us.** This is the closest published analogue to our `_reconstruct_corridors`,
and it validates the approach in general while differing from ours in two specific ways worth
adopting (§4.2): a much larger snap tolerance (1,500 m vs our 400 m), and synthetic nodes for
dangling endpoints instead of dropped spans.

### 2.10 NREL dsgrid — Hale et al. (2018)

**The Demand-Side Grid (dsgrid) Model.** NREL/TP-6A20-71492.

- Bottom-up hourly electricity load by **county, subsector and end use**, covering ~80% of
  US load.
- Reconciles bottom-up demand modeling against top-down totals.
- The related TEMPO work produces **county-level hourly passenger EV charging profiles**
  through 2050.

**Relevance to us.** This is the methodological pedigree for our `08_00_county_base_load.py`
(NREL county hourly demand → census block population → TAZ). Our pipeline is applying an
established method; the step beyond dsgrid is our k-nearest substation allocation, which
dsgrid does not attempt because it stops at the county.

### 2.11 Birchfield, Overbye et al. — synthetic grids

**Grid structural characteristics as validation criteria for synthetic networks.** IEEE Trans.
Power Systems 32(4):3258–3265 (2017); ACTIVSg test cases.

- Builds fully synthetic but statistically realistic grids (200 to 70,000 buses) from public
  information, because real topology is confidential.
- **Validates against structural statistics of real grids** — node degree distributions, line
  length distributions, voltage-class ratios.

**Relevance to us.** Offers the validation methodology we currently lack. We have no ground
truth for our reconstructed corridors, but we *can* check whether our graph's structural
statistics resemble a real transmission network. A graph with 601 singleton components and
58% of nodes in the main component would fail such a test immediately.

### 2.12 WECC Anchor Data Set (ADS)

**WECC ADS PCM and power flow cases.**

- The reference planning dataset for the Western Interconnection.
- **Transmission data is withheld from the public release for security reasons.**
- Researchers routinely work around this using the **WECC-240 reduced model** plus **EIA-930**
  capacity limits.

**Relevance to us.** This is why our problem is hard, and it is not a failing of our pipeline.
The authoritative CA transmission topology is not public. Building from HIFLD/GRIP/CEC is the
standard workaround, and the accuracy ceiling we hit is the field's ceiling too.

### 2.13 Jenn et al. (2025), Nature Communications

**Cascading marginal emissions signals for green charging with growing electric vehicle
adoption.** Nature Communications.

- Couples a WECC dispatch model with detailed private passenger EV demand built from real
  charging and driving data from 700+ California drivers.
- Finds that following average or marginal emissions-factor signals can **increase** grid
  emissions when signals are noisy, when too many EVs follow the same signal, or when
  high-emitting generators respond.

**Relevance to us.** Directly relevant to interpreting our marginal-emissions result
(~0.86 t CO₂/MWh for EV load in the June week). It establishes that marginal emissions
signals in WECC are unstable under load feedback — so our S1−S0 emissions delta should be
reported as scenario-specific, not as a transferable emissions factor.

---

## 3. Where ASTR2026 sits

| Dimension | ASTR2026 today | Literature norm |
|---|---|---|
| CA network nodes | **3,044** substations | 244–8,795 (Glista); 1,024 (Frysztacki); zonal elsewhere |
| Node definition | substation | substation (Frysztacki) or feeder (Li & Jenn) |
| Network physics | linear pipe-flow / NTC, 2 directed arcs | DC-OPF or AC power flow |
| Bulk layer | WECC BA copperplate buses | zonal (PyPSA-USA), or DC-OPF |
| T–D boundary | single-shot capacity-limited LP arc | iterated boundary exchange (HELICS) |
| Load allocation | county demand → block population → TAZ → k-NN substation | county (dsgrid); census block → feeder (Li & Jenn) |
| Capacity below 100 kV | inferred from voltage heuristics | ICA measured data (Li & Jenn) |
| Topology source | HIFLD/GRIP/CEC geometry, snapped | same (Bor et al.), or synthetic (Birchfield), or confidential (ADS) |
| Validation | none yet | structural statistics (Birchfield); one-line diagrams |

**Fair positioning:** our spatial resolution is at the high end of the published range and is
well justified by Glista and Frysztacki. Our network *physics* is at the low end — pipe-flow
with no voltage — which is the price paid for solving 3,044 nodes and 8,760 hours in one LP.
Our weakest link is not resolution or physics but **topology confidence and validation**.

---

## 4. What to improve, in priority order

### 4.1 The BA bus is a copperplate zone — the literature's named failure mode

Glista et al. attribute up to 41% error specifically to **overestimated deliverability in
aggregated zones**. Our BA node is exactly that: an unlimited internal bus that every gateway
connects to. Two fixes are already in place — removing the 12 CA-internal BA-to-BA bypass
edges, and resizing gateways from 430 GW to 172 GW — but the residual risk remains wherever
gateway capacity exceeds what physically leaves the modeled network.

**Recommendation:** report total per-BA gateway capacity as a ratio to BA peak in every run's
diagnostics, and treat any BA above ~2× as a flag. Currently BANC is 8.8× (driven by the
`parent_ba` misassignment) and CALN is 3.1×.

### 4.2 Adopt the open-data reconstruction conventions from Bor et al.

Two concrete differences from our `_reconstruct_corridors`:

1. **Snap tolerance.** Published pipelines use ~1,500 m snapping with a 250 m substation
   buffer. We use `CORRIDOR_SUBSTATION_SNAP_M = 400`. Our own audit found 77% of endpoints
   within 400 m of a real substation and only 15% dangling beyond 1 km — so raising the
   tolerance toward the published value should recover a large share of the 601 singletons.
2. **Synthetic nodes for unmatched endpoints.** The published method *inserts a node* at an
   unmatched line endpoint rather than dropping the span. We currently discard those spans,
   which is a direct cause of our fragmented graph.

### 4.3 Use HIFLD `SUB_1`/`SUB_2` as real topology for the bulk tier

Our own investigation established that the HIFLD transmission-line layer carries named
terminal substations: 2,171 CA lines, 86% with both endpoints named, 65% already joining to
our substation table by name alone. **No reviewed study uses this field** — Bor et al. and the
OSM pipelines all snap geometrically. Using it would make our ≥100 kV backbone
*attribute-verified* rather than geometrically inferred, which is a genuine methodological
contribution rather than catching up to the field.

The constraint is the layer's 100 kV voltage floor. This motivates an explicit tiering:

- **Tier 2a — bulk backbone (≥100 kV, ~353 nodes):** wired from `SUB_1`/`SUB_2`. Real topology.
- **Tier 2b — sub-transmission (60–69 kV, ~1,302 nodes, 25 GW):** geometric inference only.
  Document as an assumption.
- **Tier 2c — distribution delivery (GRIP EDSubstations, 12/4.16 kV, 704 nodes):** radially
  attached. No public topology exists; this is a modeling choice, not a measurement.

Stating this tiering explicitly is more honest than the current undifferentiated "substation
network," and it matches how LA100 and the co-simulation literature separate scales.

### 4.4 Validate against ICA — the dataset Li & Jenn used

We have no ground truth for either our allocated substation loads or our inferred capacities.
Li & Jenn's source — PG&E and SCE **ICA maps**, plus SDG&E Grid Needs Assessment — provides
real per-feeder hourly load *and* capacity. Aggregating ICA feeders to their parent substation
gives a direct check on both our k-NN load allocation and our capacity heuristics, over the
three IOUs that cover most of our footprint.

This is the highest-value validation available and it requires no modeling change.

### 4.5 Add structural validation in the style of Birchfield

Before trusting any scenario result, check the reconstructed graph against the structural
statistics of real transmission networks: node degree distribution, line length distribution,
and share of nodes in the giant component. Our current graph — 850 components, 601 singletons,
58% in the main component — would fail this test, and that failure is more diagnostic than any
solver output.

### 4.6 Fix `parent_ba` from substation ownership

Not from the literature, but it interacts with 4.1. Our BA assignment is a majority vote of the
TAZs that k-NN happened to attach to a substation; bulk hubs serve no local load and so are
decided by one or two arbitrary TAZs. Measured against the HIFLD `Owner` field, PG&E-owned
substations are assigned to the wrong BA **66% of the time** (21 of 62 correct), and 13 voltage
gateways carrying 16.2 of 57.9 GW attach to the wrong BA bus. Owner should take precedence over
the TAZ vote.

### 4.7 Report the pipe-flow simplification explicitly

Every reviewed study that resolves distribution uses either AC power flow (LA100, HELICS) or
DC-OPF. We use linear pipe-flow with no voltage or reactive power. This is defensible for a
statewide 8,760-hour LP, but it means **we cannot make voltage or thermal-limit claims at the
distribution level** — which is exactly the claim Li & Jenn make with ICA data. Our results
should be framed as energy-deliverability and dispatch outcomes, not as feeder adequacy.

---

## 5. Summary

ASTR2026's spatial resolution is well-supported and near the high end of published practice.
Its network physics is deliberately simplified and should be described as such. The real
exposure is topology confidence: we infer connectivity that the WECC ADS withholds and that no
public dataset provides below 100 kV.

The three highest-value moves are: **(1)** use HIFLD `SUB_1`/`SUB_2` to make the ≥100 kV
backbone attribute-verified, **(2)** adopt published snap tolerances and synthetic-node
handling to repair the fragmented graph, and **(3)** validate load allocation and capacities
against utility ICA data. The first would be novel relative to the reviewed literature; the
second and third bring us to established practice.

---

## Sources

1. [Li & Jenn (2024), *Impact of electric vehicle charging demand on power distribution grid congestion*, PNAS 121(18)](https://www.pnas.org/doi/abs/10.1073/pnas.2317599121) — [full text](https://pmc.ncbi.nlm.nih.gov/articles/PMC11067039/)
2. [Glista, Knueven & Watson (2026), *From zonal to nodal capacity expansion planning*, arXiv:2510.23586](https://arxiv.org/html/2510.23586)
3. [Frysztacki, Hörsch, Hagenmeyer & Brown (2021), *The strong effect of network resolution*, Applied Energy 291:116726](https://www.sciencedirect.com/science/article/pii/S0306261921002439) — [preprint](https://arxiv.org/pdf/2101.10859)
4. [Tehranchi, Barnes, Frysztacki & Azevedo, *PyPSA-USA*, SSRN 5029120](https://papers.ssrn.com/sol3/Delivery.cfm/5029120.pdf?abstractid=5029120&mirid=1) — [repository](https://github.com/PyPSA/pypsa-usa)
5. [Powell, Cezar, Azevedo et al. (2022), *Charging infrastructure access and operation*, Nature Energy 7:932–945](https://www.nature.com/articles/s41560-022-01105-7)
6. [NREL LA100 (2021), *The Los Angeles 100% Renewable Energy Study*](https://www.nrel.gov/analysis/los-angeles-100-percent-renewable-study.html) — [full report](https://maps.nrel.gov/la100/la100-study/report)
7. [Palmintier et al. (2017), *Design of the HELICS co-simulation framework*, IEEE MSCPES](https://ieeexplore.ieee.org/abstract/document/8064542/) — [documentation](https://docs.helics.org/en/helics2/user-guide/helics_co-sim_sequence.html)
8. [Wallison et al. (2025), *EV Integration using Large-Scale Combined T&D Grid Models*, arXiv:2503.17545](https://arxiv.org/abs/2503.17545)
9. [Bor, Oughton et al. (2024), *A Reproducible Method for Mapping Electricity Transmission Infrastructure*, arXiv:2412.17685](https://arxiv.org/pdf/2412.17685)
10. [Hale et al. (2018), *The Demand-Side Grid (dsgrid) Model*, NREL](https://www.osti.gov/biblio/1471555) — [toolkit](https://www2.nrel.gov/analysis/dsgrid)
11. [Birchfield, Overbye et al. (2017), *Grid structural characteristics as validation criteria for synthetic networks*, IEEE TPWRS 32(4)](https://electricgrids.engr.tamu.edu/) — [validation criteria paper](http://overbye.engr.tamu.edu/wp-content/uploads/sites/146/2021/04/Grid-Structural-Characteristics-as-Validation-Criteria-for-Synthetic-Networks.pdf)
12. [WECC Anchor Data Set (ADS)](https://www.wecc.org/program-areas/reliability-planning-performance-analysis/reliability-modeling/anchor-data-set-ads) — [Data Development and Validation Manual v3.0](https://www.wecc.org/system/files/documents/anchor_data_set/2024/ADS_Data_Development_and_Validation_Manual_6-30-2020_V3.0.pdf)
13. [Jenn et al. (2025), *Cascading marginal emissions signals for green charging*, Nature Communications](https://www.nature.com/articles/s41467-025-64979-7)
