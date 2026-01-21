# Unit Conversion Fixes - EV Charging Marginal Emissions Analysis

## Summary of Issues and Fixes

### Issue 1: Generation Extraction Had Wrong Unit Conversion ✅ FIXED

**Location:** `extract_generation_by_fuel()` function, line ~1292

**Problem:**
```python
'generation_kwh': gen_watts * 3600 / 1e6,  # WRONG - 3.6x too large!
```

**Fix Applied:**
```python
'generation_kwh': gen_watts / 1e3,  # CORRECT - W to kWh for 1-hour time steps
```

**Impact:** 
- Total generation was **3.6x too large** (15,351 GWh instead of ~4,264 GWh)
- This made the energy balance appear impossible: Generation >> Load + Wastage

**Explanation:**
- `gen_watts` is power in Watts (W), extracted from solution `net` values
- For 1-hour time steps: Energy (Wh) = Power (W) × 1 hour = Power (W)
- To convert to kWh: divide by 1,000
- The old formula multiplied by 3600 (seconds) and divided by 1e6, giving a result 3.6x too large

---

### Issue 2: Misleading Variable Names and Comments in Diagnostics ✅ FIXED

**Location:** Diagnostic functions `get_total_load_from_solution()` and `get_wastage_from_solution()`

**Problems:**
1. Variables named `total_load_ws` (implying Watt-seconds) but actually containing Watts
2. Comments incorrectly stating `net` values are in Watt-seconds
3. Confusion between power (W) and energy (W-s or W-h)

**Fixes Applied:**
1. Renamed `total_load_ws` → `total_load_watts` (more accurate)
2. Updated comments to clarify:
   - Load `net` values are **power in Watts**
   - Wastage values **ARE** in Watt-seconds (from energy balance constraint)
3. Added explicit conversion explanations

---

## Understanding Units in GOOD Model

### From Source Code Analysis:

**Load Assets (`load.py`):**
```python
solution["net"] = [profile[i] * capacity + shift[i] for i in model.steps]
```
- `profile[i]`: per-unit value (0-1)
- `capacity`: Watts (W)
- **Result: `net` is in Watts (power)**

**Producer Assets (`producer.py`):**
```python
solution["net"] = production  # production variable value
```
- **Result: `net` is in Watts (power)**

**Energy Balance Constraint (`region.py`):**
```python
energy = production[step] * model.time_step  # Producer
energy = profile[step] * model.time_step * capacity  # Load
```
- `model.time_step = 3600` seconds (for 1-hour steps)
- **Constraint uses Watt-seconds (W-s) for energy**
- **Wastage variable is in Watt-seconds to match**

### Correct Conversions:

**For Load/Generation (power values):**
```python
sum(net) = sum of 168 power values in Watts
Energy in Wh = sum(net) * 1 hour = sum(net)
Energy in kWh = sum(net) / 1,000
Energy in GWh = sum(net) / 1e9
```

**For Wastage (energy values):**
```python
sum(wastage) = sum of energy values in Watt-seconds
Energy in Wh = sum(wastage) / 3,600
Energy in GWh = sum(wastage) / 3.6e12
```

---

## Expected Results After Fix

### Before Fix:
```
Generation: 15,351.10 GWh  ← 3.6x TOO LARGE!
Load:        5,414.44 GWh
Wastage:        20.08 GWh
Balance: 15,351 ≠ 5,414 + 20  ← MISMATCH!
```

### After Fix:
```
Generation: ~4,264 GWh  ← Corrected (15,351 / 3.6)
Load:        5,414 GWh
Wastage:        20 GWh
Balance: 4,264 + Renewables ≈ 5,414 + 20 + Exports  ← Should balance!
```

**Note:** The apparent mismatch after the fix is because:
1. **Renewables (solar, wind)** are modeled as negative Load assets in California.json
   - They are NOT included in the "generation" extraction (which only counts Producer assets)
   - They ARE counted as negative in the "load" calculation
   - California has ~4,000-5,000 GWh of renewables in 7 days
   
2. **Imports/Exports** may also affect the balance
   - Check if imports are counted in "generation" extraction
   - Check if exports are counted in "load" calculation

### To Verify the Balance:

Run this after the fix:
```python
# Get renewable generation from Load assets with negative capacity
renewable_gen = 0
for source, node in baseline_solution._node.items():
    for handle, asset in node['assets'].items():
        if asset.get('_class') == 'Load':
            net = asset.get('net', [])
            if net and sum(net) < 0:  # Negative means generation
                renewable_gen += abs(sum(net))

renewable_gen_gwh = renewable_gen / 1e9
print(f"Renewable generation: {renewable_gen_gwh:.2f} GWh")
print(f"Total supply = Generation + Renewables: {baseline_total_gen_gwh + renewable_gen_gwh:.2f} GWh")
print(f"Total demand = Load + Wastage: {baseline_total_load_gwh + baseline_wastage_gwh:.2f} GWh")
```

---

## Files Modified:

1. **`e:\GitHub\good_model\EV_Charging_Marginal_Emissions.ipynb`**
   - Line ~1292: Fixed generation_kwh calculation
   - Diagnostic section: Fixed variable names and comments

2. **`e:\GitHub\good_model\UNIT_CONVERSION_FIXES.md`** (this file)
   - Documentation of all fixes and explanations

---

## Next Steps:

1. **Restart the notebook kernel** to clear any cached values
2. **Re-run all cells** from the beginning
3. **Verify the energy balance** makes sense
4. **Check that EV scenario generation > baseline generation** by ~106 GWh (or renewable displacement)
5. **Examine the generation by fuel tables** to see which fuels increased

---

## Key Takeaway:

The GOOD model uses **different units in constraints vs. solutions**:
- **Constraints:** Energy in Watt-seconds (W-s)
- **Solutions (net):** Power in Watts (W) 
- **Wastage:** Energy in Watt-seconds (W-s)

Always check the source code to verify which units are being used!
