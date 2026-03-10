# Compare CAPEX expansion (added capacity in MW) for generators and transmission lines
# REVISED: Calculate CAPEX expansion of each type within each WECC region
import numpy as np
import pandas as pd

def _scalar_capex(val):
    """Extract scalar capex value from list/array/number."""
    if isinstance(val, (list, tuple, np.ndarray)):
        return float(val[0]) if len(val) else 0.0
    return float(val or 0.0)

def get_generator_capex_MW(solution, graph):
    """
    Scan solution + graph and return a DataFrame of generator CAPEX expansions (MW)
    by region/asset/fuel.
    """
    rows = []
    for region, sol_node in solution._node.items():
        gr_node = graph._node.get(region, {})
        for handle, asset_sol in sol_node.get('assets', {}).items():
            gr_asset = gr_node.get('assets', {}).get(handle, {})
            if gr_asset.get('_class') != 'Producer':
                continue
            capex = _scalar_capex(asset_sol.get('capex', 0.0))
            if abs(capex) < 1e-3:
                continue  # skip effectively zero expansions
            fuel = (gr_asset.get('fuel') or 'unknown')
            rows.append({
                'region': region,
                'asset': handle,
                'fuel': fuel,
                'capex_MW': capex / 1e6,  # W -> MW
            })
    return pd.DataFrame(rows)

def get_transmission_capex_MW(solution, graph):
    """
    Scan solution + graph and return a DataFrame of transmission CAPEX expansions (MW)
    by corridor/line. Transmission is attributed to the source region.
    """
    rows = []
    for src, adj_sol in solution._adj.items():
        for tgt, edge_sol in adj_sol.items():
            if 'lines' not in edge_sol:
                continue
            edge_gr = graph._adj.get(src, {}).get(tgt, {})
            for line_name, line_sol in edge_sol['lines'].items():
                capex = _scalar_capex(line_sol.get('capex', 0.0))
                if abs(capex) < 1e-3:
                    continue
                rows.append({
                    'source': src,
                    'target': tgt,
                    'line': line_name,
                    'capex_MW': capex / 1e6,  # W -> MW
                })
    return pd.DataFrame(rows)

# --- MAIN (run after baseline_solution, baseline_graph, ev_solution, ev_graph exist) ---
try:
    gen_base = get_generator_capex_MW(baseline_solution, baseline_graph)
    tx_base = get_transmission_capex_MW(baseline_solution, baseline_graph)
    gen_ev = get_generator_capex_MW(ev_solution, ev_graph)
    tx_ev = get_transmission_capex_MW(ev_solution, ev_graph)
except NameError:
    print("Please run the optimization cells first so that "
          "'baseline_solution', 'baseline_graph', 'ev_solution', and 'ev_graph' exist.")
else:
    # Filter to WECC regions only
    def wecc_gen(df):
        if df.empty or 'region' not in df.columns:
            return df
        return df[df['region'].str.startswith('WECC_')]
    def wecc_tx(df):
        if df.empty or 'source' not in df.columns:
            return df
        return df[df['source'].str.startswith('WECC_') & df['target'].str.startswith('WECC_')]

    gen_base = wecc_gen(gen_base)
    tx_base = wecc_tx(tx_base)
    gen_ev = wecc_gen(gen_ev)
    tx_ev = wecc_tx(tx_ev)

    # --- Per-region CAPEX by type ---
    # Get all WECC regions from graph so we show them even when CAPEX is zero/disabled
    all_wecc_regions = sorted([n for n in baseline_graph._node.keys() if str(n).startswith('WECC_')])

    def build_regional_summary(gen_df, tx_df, all_regions):
        """Build per-region summary: generators by fuel + transmission (attributed to source)."""
        regions = set(all_regions)
        if not gen_df.empty:
            regions.update(gen_df['region'].unique())
        if not tx_df.empty:
            regions.update(tx_df['source'].unique())
        regions = sorted(r for r in regions if str(r).startswith('WECC_'))

        rows = []
        for r in regions:
            row = {'region': r}
            # Generator CAPEX by fuel
            if not gen_df.empty:
                g = gen_df[gen_df['region'] == r]
                if not g.empty:
                    for fuel, grp in g.groupby('fuel'):
                        col = f'gen_{fuel}'
                        row[col] = grp['capex_MW'].sum()
                    row['gen_total'] = g['capex_MW'].sum()
                else:
                    row['gen_total'] = 0.0
            else:
                row['gen_total'] = 0.0

            # Transmission CAPEX (lines where this region is source)
            if not tx_df.empty:
                t = tx_df[tx_df['source'] == r]
                row['tx_total'] = t['capex_MW'].sum() if not t.empty else 0.0
            else:
                row['tx_total'] = 0.0

            row['total'] = row.get('gen_total', 0) + row.get('tx_total', 0)
            rows.append(row)

        out = pd.DataFrame(rows)
        # Ensure required columns exist when empty (avoids KeyError on merge)
        if out.empty:
            out = pd.DataFrame(columns=['region', 'gen_total', 'tx_total', 'total'])
        return out

    base_reg = build_regional_summary(gen_base, tx_base, all_wecc_regions)
    ev_reg = build_regional_summary(gen_ev, tx_ev, all_wecc_regions)

    # Merge baseline and EV for comparison
    def merge_regional(base_df, ev_df):
        # Empty DataFrames from pd.DataFrame([]) have no columns - ensure 'region' exists
        if base_df.empty:
            base_df = pd.DataFrame(columns=['region', 'gen_total', 'tx_total', 'total'])
        if ev_df.empty:
            ev_df = pd.DataFrame(columns=['region', 'gen_total', 'tx_total', 'total'])
        if 'region' not in base_df.columns or 'region' not in ev_df.columns:
            return pd.DataFrame(columns=['region'])
        base_df = base_df.rename(columns={c: f'{c}_base' for c in base_df.columns if c != 'region'})
        ev_df = ev_df.rename(columns={c: f'{c}_ev' for c in ev_df.columns if c != 'region'})
        m = base_df.merge(ev_df, on='region', how='outer').fillna(0)
        # Add delta columns
        if 'gen_total_base' in m.columns and 'gen_total_ev' in m.columns:
            m['gen_delta'] = m['gen_total_ev'] - m['gen_total_base']
        if 'tx_total_base' in m.columns and 'tx_total_ev' in m.columns:
            m['tx_delta'] = m['tx_total_ev'] - m['tx_total_base']
        if 'total_base' in m.columns and 'total_ev' in m.columns:
            m['total_delta'] = m['total_ev'] - m['total_base']
        return m

    merged = merge_regional(base_reg, ev_reg)

    # Display: summary table by region
    display_cols = ['region', 'gen_total_base', 'gen_total_ev', 'gen_delta',
                    'tx_total_base', 'tx_total_ev', 'tx_delta',
                    'total_base', 'total_ev', 'total_delta']
    display_cols = [c for c in display_cols if c in merged.columns]
    summary_table = merged[display_cols].copy()
    summary_table.columns = [c.replace('_base', ' (Base)').replace('_ev', ' (EV)').replace('_delta', ' (Δ)') for c in summary_table.columns]

    print("\nCAPEX EXPANSION (MW) BY WECC REGION AND TYPE")
    print("=" * 80)
    print(summary_table.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))

    # Generator breakdown by fuel within each region (EV scenario)
    if not gen_ev.empty:
        gen_pivot = gen_ev.pivot_table(index='region', columns='fuel', values='capex_MW', aggfunc='sum').fillna(0)
        gen_pivot['total'] = gen_pivot.sum(axis=1)
        gen_pivot = gen_pivot.sort_values('total', ascending=False)
        print("\nGenerator CAPEX by fuel type within each WECC region (EV scenario, MW):")
        print(gen_pivot.to_string(float_format=lambda x: f"{x:,.3f}"))

    # Transmission by corridor (EV scenario)
    if not tx_ev.empty:
        tx_by_corridor = tx_ev.groupby(['source', 'target'])['capex_MW'].sum().reset_index()
        tx_by_corridor = tx_by_corridor.sort_values('capex_MW', ascending=False)
        print("\nTransmission CAPEX by corridor (EV scenario, MW):")
        print(tx_by_corridor.head(15).to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
