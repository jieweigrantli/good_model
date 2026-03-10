"""Add WECC-specific marginal generation plots to EV_Charging_Marginal_Emissions.ipynb"""
import json

nb_path = "EV_Charging_Marginal_Emissions.ipynb"
try:
    with open(nb_path, "r", encoding="utf-8") as f:
        nb = json.load(f)
except json.JSONDecodeError as e:
    print(f"JSON error: {e}")
    exit(1)

# Find cell 45 (0-indexed) - the one with Plot 1
cell = nb["cells"][45]
src = "".join(cell["source"])
old = """        fuel_file = os.path.join(fuel_plot_dir, f'hourly_marginal_{fuel}.png')
        plt.savefig(fuel_file, dpi=150)
        plt.close(fig)
        print(f'Saved fuel-by-fuel plot for {fuel} to {fuel_file}')"""

wecc_add = '''

# Plot 1 WECC: Same plots but for WECC regions only (region.startswith('WECC_'))
if 'region' in marginal_df.columns:
    marginal_wecc = marginal_df[marginal_df['region'].str.startswith('WECC_')]
    hourly_results_wecc = marginal_wecc.groupby(['hour', 'fuel']).agg({
        'marginal_generation_kwh': 'sum',
        'co2_emissions_kg': 'sum',
        'nox_emissions_kg': 'sum',
        'so2_emissions_kg': 'sum'
    }).reset_index()
    fuel_totals_wecc = hourly_results_wecc.groupby('fuel')['marginal_generation_kwh'].sum()
    nonzero_fuels_wecc = [f for f, v in fuel_totals_wecc.items() if abs(v) > 1e-9]
    
    fig, ax = plt.subplots(figsize=(14, 6))
    for fuel in sorted(nonzero_fuels_wecc):
        fuel_data = hourly_results_wecc[hourly_results_wecc['fuel'] == fuel]
        if len(fuel_data) > 0:
            ax.plot(fuel_data['hour'], fuel_data['marginal_generation_kwh'] / 1e6,
                    label=fuel, marker='o', markersize=3)
    ax.set_xlabel('Hour')
    ax.set_ylabel('Marginal Generation (GWh)')
    ax.set_title('Marginal Generation by Fuel Type - WECC Only (7-Day Period)')
    ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(f'{output_dir}/hourly_marginal_generation_7days_wecc.png', dpi=150)
    plt.show()
    
    fuel_plot_dir_wecc = os.path.join(output_dir, "fuel_hourly_timeseries_wecc")
    os.makedirs(fuel_plot_dir_wecc, exist_ok=True)
    for fuel in sorted(nonzero_fuels_wecc):
        fuel_data = hourly_results_wecc[hourly_results_wecc['fuel'] == fuel]
        if len(fuel_data) > 0:
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(fuel_data['hour'], fuel_data['marginal_generation_kwh'] / 1e6,
                    label=fuel, marker='o', color='tab:blue')
            ax.set_xlabel('Hour')
            ax.set_ylabel('Marginal Generation (GWh)')
            ax.set_title(f'Hourly Marginal Generation for {fuel} - WECC Only (7-Day Period)')
            ax.grid(True, alpha=0.3)
            ax.legend()
            plt.tight_layout()
            fuel_file = os.path.join(fuel_plot_dir_wecc, f'hourly_marginal_{fuel}.png')
            plt.savefig(fuel_file, dpi=150)
            plt.close(fig)
            print(f'Saved WECC fuel plot for {fuel} to {fuel_file}')
else:
    print("marginal_df has no 'region' column - cannot create WECC-specific plots")
'''

if old in src:
    new_src = src.replace(old, old + wecc_add)
    lines = new_src.split("\n")
    cell["source"] = [line + "\n" if i < len(lines) - 1 else (line if line else "") for i, line in enumerate(lines)]
    with open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)
    print("Added WECC plots successfully")
else:
    print("Could not find target string")
    print("First 200 chars of old:", repr(old[:200]))
