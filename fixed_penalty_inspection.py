import pandas as pd
import pyomo.environ as pe

# -------- PENALTY VARIABLE INVESTIGATION --------
print("\n--- INSPECTING PENALTY VARIABLES (Shortfall & Wastage) ---\n")

model = None
if 'ev_network' in globals() and hasattr(ev_network, 'model'):
    model = ev_network.model

if model is not None:
    found_penalty = False
    
    # Search for shortfall and wastage variables (format: {region_name}::shortfall or {region_name}::wastage)
    for var_component in model.component_objects(pe.Var, active=True):
        name = var_component.name
        
        # Check for shortfall or wastage variables
        if '::shortfall' in name or '::wastage' in name:
            # Extract values for all time steps
            total_value = 0
            active_times = []
            
            for index in var_component:
                val = pe.value(var_component[index])
                if val is not None and val > 1e-6:  # Small threshold to avoid floating point noise
                    total_value += val
                    active_times.append((index, val))
            
            if total_value > 1e-6:
                found_penalty = True
                var_type = "SHORTFALL" if "shortfall" in name else "WASTAGE"
                print(f"⚠️ FOUND ACTIVE {var_type}: '{name}'")
                print(f"   Total Amount: {total_value/1e9:.2f} GWh (or {total_value/1e6:.2f} TWh)")
                print(f"   Active at {len(active_times)} time steps")
                if len(active_times) > 0:
                    print("   Sample times (first 5):")
                    for idx, val in active_times[:5]:
                        print(f"     t={idx}: {val/1e9:.4f} GWh ({val/1e6:.2f} MW)")
                print()
    
    if not found_penalty:
        print("✓ No active shortfall or wastage variables found (good - system is balanced).\n")
else:
    print("⚠️  'ev_network' or its model is not defined, skipping penalty variable inspection.\n")

# -------- ENERGY BALANCE CHECK AT HOUR 19 --------
print("\n--- ENERGY BALANCE AT HOUR 19 ---\n")
target_hour = 19
total_gen = 0
generation_by_asset = {}

if model is not None:
    # Search for production variables (format: {asset_name}::production)
    found_production = False
    
    for var_component in model.component_objects(pe.Var, active=True):
        name = var_component.name
        
        if '::production' in name:
            found_production = True
            asset_name = name.split('::')[0]
            
            # Get value at target hour
            if target_hour in var_component:
                val = pe.value(var_component[target_hour])
                if val is not None and val > 1e-6:  # Small threshold
                    generation_by_asset[asset_name] = val
                    total_gen += val
    
    if found_production:
        print(f"Generation at hour {target_hour}:")
        if len(generation_by_asset) > 0:
            # Sort by generation amount
            sorted_assets = sorted(generation_by_asset.items(), key=lambda x: x[1], reverse=True)
            for asset_name, val in sorted_assets[:10]:  # Show top 10
                print(f"  {asset_name:40s}: {val/1e9:8.4f} GW ({val/1e6:10.2f} MW)")
            if len(sorted_assets) > 10:
                print(f"  ... and {len(sorted_assets) - 10} more assets")
        else:
            print("  No generation at this hour")
    else:
        print("⚠️ Could not find any '::production' variables in the model.\n")
else:
    print("⚠️  'ev_network' or its model is not defined, skipping hourly balance check.\n")

print("-" * 60)
print(f"Total Generation at Hour {target_hour}: {total_gen/1e9:.4f} GW ({total_gen/1e6:.2f} MW)")
