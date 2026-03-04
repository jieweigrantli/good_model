"""
Analyze which dispatched plants/fuels cause marginal emission intensity > 15,000 g CO2/kWh.
"""
import pandas as pd
import numpy as np

# 1. Per-fuel emission intensity from period totals (actual plant emission factors)
print("=" * 60)
print("PER-FUEL EMISSION INTENSITY (from marginal generation)")
print("=" * 60)
p = pd.read_csv('ev_charging_results/period_marginal_generation_emissions.csv')
for _, row in p.iterrows():
    gen = row['marginal_generation_kwh']
    co2 = row['co2_emissions_kg']
    if gen > 0 and co2 > 0:
        intensity_g = (co2 * 1000) / gen  # g CO2/kWh
        print(f"  {row['fuel']:15s}: {intensity_g:>7.0f} g CO2/kWh  (gen={gen/1e6:>8.2f} GWh, co2={co2/1e6:.2f} Mkg)")
    elif gen != 0 and co2 > 0:
        intensity_g = (co2 * 1000) / gen if gen != 0 else 0
        print(f"  {row['fuel']:15s}: {intensity_g:>7.0f} g CO2/kWh  (gen={gen/1e6:>8.2f} GWh - NEGATIVE, co2={co2/1e6:.2f} Mkg)")

# 2. Hourly intensity = total CO2 / EV energy (or marginal gen when EV load unknown)
# The 15,000 g/kWh comes from: total_CO2 / total_EV_energy. Need EV load.
# Check if we have EV load in any output - likely need to load from notebook state.
# For now: find hours where CO2 is high and identify contributing fuels.
print("\n" + "=" * 60)
print("HOURLY CO2 CONTRIBUTION - TOP 20 HOURS BY CO2 EMISSIONS")
print("=" * 60)
h = pd.read_csv('ev_charging_results/hourly_marginal_generation_emissions.csv')
hourly_co2 = h.groupby('hour')['co2_emissions_kg'].sum()
top_hours = hourly_co2.nlargest(20)
for hr, co2 in top_hours.items():
    fuels = h[h['hour'] == hr]
    fuels_pos = fuels[fuels['co2_emissions_kg'] > 0].sort_values('co2_emissions_kg', ascending=False)
    contrib = fuels_pos[['fuel', 'marginal_generation_kwh', 'co2_emissions_kg']].to_string(index=False)
    print(f"\nHour {hr}: Total CO2 = {co2/1000:.1f} tonnes")
    for _, r in fuels_pos.iterrows():
        intensity = (r['co2_emissions_kg']*1000)/r['marginal_generation_kwh'] if r['marginal_generation_kwh'] > 0 else 0
        print(f"    {r['fuel']:15s}: gen={r['marginal_generation_kwh']/1000:>8.2f} MWh, co2={r['co2_emissions_kg']/1000:>6.1f} t, intensity={intensity:.0f} g/kWh")

# 3. WECC plants with highest emission factors (from detailed results)
print("\n" + "=" * 60)
print("WECC PLANTS WITH HIGHEST EMISSION FACTORS (co2_factor)")
print("=" * 60)
d = pd.read_csv('ev_charging_results/detailed_hourly_marginal_results.csv')
# co2_factor is kg/J. Convert to g/kWh: kg/J * 1e6 g/kg * 3.6e6 J/kWh = 3.6e12 * co2_factor
asset_max = d[d['co2_factor'] > 0].groupby(['region', 'asset', 'fuel'])['co2_factor'].max().reset_index()
asset_max['g_per_kWh'] = asset_max['co2_factor'] * 3.6e9  # 3.6e6 J/kWh * 1000 g/kg
asset_max = asset_max.sort_values('g_per_kWh', ascending=False)
print(asset_max.head(30).to_string(index=False))
print(f"\nMax plant intensity: {asset_max['g_per_kWh'].max():.0f} g CO2/kWh")
print(f"Fuels with intensity > 1000 g/kWh: {len(asset_max[asset_max['g_per_kWh'] > 1000])} assets")

# 4. Which high-intensity plants were actually DISPATCHED?
print("\n" + "=" * 60)
print("WECC PLANTS WITH INTENSITY > 15,000 g/kWh (were they dispatched?)")
print("=" * 60)
d['g_per_kWh'] = d['co2_factor'] * 3.6e9
high_int = d[d['g_per_kWh'] > 15000]
dispatched = high_int[high_int['marginal_generation_kwh'] != 0]
if len(dispatched) == 0:
    print("  NONE of the plants with intensity > 15,000 g/kWh were dispatched.")
    print("  The Utah natural gas plants (installed_17181-17184) have this intensity but had ZERO marginal generation.")
    print("  The ~15,000 g/kWh average comes from: total_marginal_CO2 / total_EV_energy (system-level ratio),")
    print("  not from any single plant's emission factor.")
else:
    print(dispatched.groupby(['region','asset','fuel']).agg({
        'marginal_generation_kwh':'sum','co2_emissions_kg':'sum','g_per_kWh':'first'
    }).to_string())
