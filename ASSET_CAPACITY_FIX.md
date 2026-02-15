# Asset Capacity Unit Conversion Fix

## Problem Identified

The `normalize_raw_data()` function was only scaling transmission line capacities and top-level node keys, but **was NOT scaling Load and Producer asset capacities** that are nested inside `node['assets']`.

### Root Cause

In the California.json file structure:
- Load assets are stored as `node['assets'][asset_name]` with `_class: "Load"` and `installed_capacity` in **Watts**
- Producer assets are stored as `node['assets'][asset_name]` with `_class: "Producer"` and `installed_capacity` in **Watts**
- The normalization function was only checking top-level node keys like `'load'`, `'demand'`, etc., which don't exist in this structure

### Impact

This caused **massive shortfalls** in the optimization results because:
1. Load asset capacities were interpreted as 1 billion times too large (Watts instead of GW)
2. The model tried to satisfy loads that were physically impossible
3. Shortfall variables showed values in the trillions of GWh (e.g., `WECC_SCE::shortfall: 2,604,879,808,100.00 GWh`)

### Evidence from Output

```
✓ Scaled 12 Transmission Lines to GW.
✓ Scaled 0 Nodes (Load/Demand) to GW.  ← This should have been > 0!
```

The function reported "0 Nodes" fixed because it wasn't finding load data in the expected top-level keys.

## Solution Applied

Created `normalize_graph()` function and updated workflow to:

1. **Normalize the graph BEFORE `from_graph()`** - This is critical because `from_graph()` deepcopies the graph and creates Asset objects. Normalizing after won't affect those objects.

2. **Check inside `node['assets']`** for Load and Producer assets
3. **Scale `installed_capacity`** from Watts to GW for any asset with:
   - `_class` in `['Load', 'Producer']`
   - `installed_capacity > 1e6` (1 MW threshold, indicating Watts units - values in GW would be < 1)

### Code Changes

1. **Created `normalize_graph(graph)` function** - Works directly on NetworkX graphs, should be called BEFORE `from_graph()`

2. **Updated workflow** - Both baseline and EV scenarios now call:
   ```python
   normalize_graph(baseline_graph)  # BEFORE from_graph()
   baseline_network = Network(**kw).from_graph(baseline_graph, policies)
   ```

3. **Added STEP 2B** in the normalization function:

```python
# --- STEP 2B: Fix Asset Capacities (Load and Producer assets) ---
# This is the CRITICAL fix - assets are nested inside nodes!
if 'assets' in data and isinstance(data['assets'], dict):
    for asset_name, asset_data in data['assets'].items():
        if not isinstance(asset_data, dict):
            continue
        
        # Check if this is a Load or Producer asset
        asset_class = asset_data.get('_class', '')
        if asset_class in ['Load', 'Producer']:
            # Check and scale installed_capacity
            if 'installed_capacity' in asset_data:
                cap = asset_data['installed_capacity']
                if isinstance(cap, (int, float)) and cap > 1e5:  # > 100 MW, likely in Watts
                    asset_data['installed_capacity'] = cap / 1e9
                    assets_fixed += 1
                    node_modified = True
```

## Expected Results After Fix

1. **Normalization output should now show:**
   ```
   ✓ Scaled 12 Transmission Lines to GW.
   ✓ Scaled X Nodes (top-level keys) to GW.
   ✓ Scaled Y Assets (Load/Producer capacities) to GW.  ← NEW!
   ```

2. **Shortfall values should be near zero** (or at least reasonable, not trillions of GWh)

3. **Energy balance should be satisfied** without massive shortfalls

4. **Load values should be realistic** (e.g., California total load ~30-50 GW, not billions of GW)

## Verification Steps

After running the updated code, check:

1. The normalization output shows assets being scaled
2. Shortfall variables are small or zero
3. Total load values are reasonable (check `get_total_load_from_solution()`)
4. The optimization solves without massive primal infeasibilities
5. Generation and load values are in the expected range

## Related Files

- `EV_Charging_Marginal_Emissions.ipynb` - Main notebook with the fix
- `Examples/California.json` - Data file with asset structure
- `good/optimization/assets/load.py` - Load asset class definition
- `good/optimization/assets/producer.py` - Producer asset class definition
