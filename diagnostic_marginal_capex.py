# Diagnostic: Why no CAPEX? Why do wind/solar show positive marginal generation?
# Run this cell AFTER baseline_solution, ev_solution, baseline_graph, ev_graph, baseline_gen, ev_gen exist.
# Also requires extract_generation_by_fuel and marginal_df (from marginal emissions cell).

import numpy as np
import pandas as pd

def _scalar(val):
    if isinstance(val, (list, tuple, np.ndarray)):
        return float(val[0]) if len(val) else 0.0
    return float(val or 0.0)

print("=" * 80)
print("DIAGNOSTIC: Marginal Generation, Curtailment, Storage, and CAPEX")
print("=" * 80)

# --- 1. CAPEX VERIFICATION ---
print("\n--- 1. CAPEX: Why is there no expansion? ---")
extensible_producers = []
extensible_stores = []
extensible_lines = []
for region, node in baseline_graph._node.items():
    if not str(region).startswith('WECC_'):
        continue
    for handle, asset in node.get('assets', {}).items():
        cap = _scalar(asset.get('capex_capacity', 0))
        if cap > 0:
            fuel = asset.get('fuel', asset.get('type', 'unknown'))
            if asset.get('_class') == 'Producer':
                extensible_producers.append((region, handle, fuel, cap / 1e6))
            elif asset.get('_class') == 'Store':
                extensible_stores.append((region, handle, fuel, cap / 1e6))
    for tgt, edge in baseline_graph._adj.get(region, {}).items():
        if not str(tgt).startswith('WECC_'):
            continue
        for line_name, line in edge.get('lines', {}).items():
            limit = _scalar(line.get('capex_limit', 0))
            if limit > 0:
                extensible_lines.append((region, tgt, line_name, limit / 1e6))

print(f"  Producers with capex_capacity > 0: {len(extensible_producers)}")
if extensible_producers:
    for r, h, f, c in extensible_producers[:5]:
        print(f"    {r} / {h} ({f}): {c:.2f} MW")
else:
    print("    NONE - CAPEX expansion is disabled for all generators.")
print(f"  Stores with capex_capacity > 0: {len(extensible_stores)}")
print(f"  Transmission lines with capex_limit > 0: {len(extensible_lines)}")

# Check solution for any non-zero capex
capex_in_solution = []
for region, node in baseline_solution._node.items():
    for handle, asset in node.get('assets', {}).items():
        c = asset.get('capex')
        if c is not None:
            v = _scalar(c)
            if abs(v) > 1e-3:
                capex_in_solution.append((region, handle, v / 1e6))
print(f"  Non-zero capex in baseline_solution: {len(capex_in_solution)}")
if capex_in_solution:
    for r, h, v in capex_in_solution[:5]:
        print(f"    {r} / {h}: {v:.2f} MW")

# --- 2. WIND/SOLAR CURTAILMENT IN BASELINE ---
print("\n--- 2. Wind/Solar curtailment in BASELINE (production < available) ---")
vre_fuels = ['wind', 'solar']
curtailment_rows = []
for region, node in baseline_solution._node.items():
    if not str(region).startswith('WECC_'):
        continue
    gr_node = baseline_graph._node.get(region, {})
    for handle, asset in node.get('assets', {}).items():
        gr_asset = gr_node.get('assets', {}).get(handle, {})
        if gr_asset.get('_class') != 'Producer':
            continue
        fuel = (gr_asset.get('fuel') or '').lower()
        if fuel not in vre_fuels:
            continue
        net = np.array(asset.get('net', []), dtype=float)
        if len(net) == 0:
            continue
        cap = _scalar(gr_asset.get('installed_capacity', 0))
        prof = asset.get('profile', gr_asset.get('profile'))
        if prof is not None:
            prof = np.array(prof, dtype=float)
            if len(prof) != len(net):
                prof = np.ones(len(net)) * (np.mean(prof) if len(prof) else 1.0)
        else:
            prof = np.ones(len(net)) * _scalar(gr_asset.get('capacity_factor', 1))
        available = cap * prof
        tol = 1e-3 * cap
        curtailed_hours = np.sum((available > tol) & (net < available - tol))
        total_available = np.sum(available)
        total_prod = np.sum(net)
        if total_available > 1e6:
            curtail_pct = 100 * (1 - total_prod / total_available)
            if curtailed_hours > 0 or curtail_pct > 0.1:
                curtailment_rows.append({
                    'region': region, 'asset': handle, 'fuel': fuel,
                    'curtailed_hours': curtailed_hours, 'curtail_pct': curtail_pct,
                    'total_available_GWh': total_available / 3.6e12,
                    'total_prod_GWh': total_prod / 3.6e12
                })

if curtailment_rows:
    df_curt = pd.DataFrame(curtailment_rows)
    print(df_curt.to_string(index=False))
    print("\n  -> Baseline IS curtailing wind/solar. EV load increases demand,")
    print("     so EV scenario uses more of previously curtailed VRE -> positive marginal for wind/solar.")
else:
    print("  No significant curtailment detected in baseline for WECC wind/solar.")
    print("  (Marginal wind/solar may come from different regions or battery shifting.)")

# --- 3. BATTERY AND PUMP HYDRO ACROSS SCENARIOS ---
print("\n--- 3. Battery and Pump Hydro: Baseline vs EV ---")
storage_fuels = ['battery', 'pump hydro']
for fuel in storage_fuels:
    bl = baseline_gen[baseline_gen['fuel'] == fuel]
    ev = ev_gen[ev_gen['fuel'] == fuel]
    bl_discharge = bl['generation_kwh'].sum() / 1e6  # GWh
    ev_discharge = ev['generation_kwh'].sum() / 1e6
    bl_charge = 0.0
    ev_charge = 0.0
    # Charge = consumption; extract_generation_by_fuel uses max(0,net) for storage
    # So we need production/consumption from solution for charge
    for region, node in baseline_solution._node.items():
        if not str(region).startswith('WECC_'):
            continue
        gr = baseline_graph._node.get(region, {})
        for h, a in node.get('assets', {}).items():
            if gr.get('assets', {}).get(h, {}).get('fuel') == fuel:
                cons = np.array(a.get('consumption', [0]), dtype=float)
                bl_charge += np.sum(cons) * 3600 / 3.6e12  # W*s -> GWh
    for region, node in ev_solution._node.items():
        if not str(region).startswith('WECC_'):
            continue
        gr = ev_graph._node.get(region, {})
        for h, a in node.get('assets', {}).items():
            if gr.get('assets', {}).get(h, {}).get('fuel') == fuel:
                cons = np.array(a.get('consumption', [0]), dtype=float)
                ev_charge += np.sum(cons) * 3600 / 3.6e12
    print(f"  {fuel}:")
    print(f"    Baseline: discharge={bl_discharge:.3f} GWh, charge={bl_charge:.3f} GWh")
    print(f"    EV:      discharge={ev_discharge:.3f} GWh, charge={ev_charge:.3f} GWh")
    print(f"    Delta:   discharge={ev_discharge-bl_discharge:+.3f} GWh, charge={ev_charge-bl_charge:+.3f} GWh")

# --- 4. IS BATTERY THE MAIN DRIVER OF MARGINAL GENERATION? ---
print("\n--- 4. Marginal generation by fuel: is battery the main driver? ---")
if 'marginal_df' in globals() and marginal_df is not None:
    mg = marginal_df.groupby('fuel')['marginal_generation_kwh'].sum()
    mg = mg.sort_values(key=abs, ascending=False)
    total_marginal = mg.sum()
    print("  Total marginal generation by fuel (kWh):")
    for f, v in mg.items():
        pct = 100 * v / total_marginal if abs(total_marginal) > 1e-9 else 0
        print(f"    {f:15s}: {v:12,.0f} kWh ({pct:+.1f}%)")
    battery_share = mg.get('battery', 0) / total_marginal if abs(total_marginal) > 1e-9 else 0
    print(f"\n  Battery share of total marginal: {100*battery_share:.1f}%")
    if abs(battery_share) > 0.3:
        print("  -> Battery is a MAJOR driver of marginal generation differences.")
    else:
        print("  -> Battery is NOT the dominant driver; other fuels contribute more.")
else:
    print("  marginal_df not found. Run the marginal emissions extraction cell first.")

# --- 5. CAPEX EXTRACTION CHECK ---
print("\n--- 5. CAPEX extraction logic check ---")
def _scalar_capex(val):
    if isinstance(val, (list, tuple, np.ndarray)):
        return float(val[0]) if len(val) else 0.0
    return float(val or 0.0)
capex_found = []
for region, sol_node in baseline_solution._node.items():
    if not str(region).startswith('WECC_'):
        continue
    for handle, asset_sol in sol_node.get('assets', {}).items():
        gr_asset = baseline_graph._node.get(region, {}).get('assets', {}).get(handle, {})
        if gr_asset.get('_class') != 'Producer':
            continue
        c = _scalar_capex(asset_sol.get('capex', 0.0))
        if abs(c) >= 1e-3:
            capex_found.append((region, handle, gr_asset.get('fuel','?'), c/1e6))
print(f"  Generator CAPEX >= 1e-3 W in solution: {len(capex_found)}")
if not capex_found and len(extensible_producers) == 0:
    print("  -> CAPEX comparison is correct: no expansion is enabled (capex_capacity=0 for all).")
    print("  -> The empty CAPEX table reflects reality: the model did not build new capacity.")
print("=" * 80)
