"""
postprocess.py — Plots, CSVs, and summary reports for one seasonal iteration.

Can be imported by run_all.py or run standalone from saved JSON files:

    # run for a single iteration folder:
    python ev_charging_project/postprocess.py ev_charging_results/march

    # run for all iterations:
    python ev_charging_project/postprocess.py

Generated outputs (in <iter_dir>/plots/ and <iter_dir>/data/):
  Notebook-faithful plots
  ─────────────────────────────────────────────────────────────────
  ca_balance_gen_baseline.png          CA energy balance – generation panel (baseline)
  ca_balance_gen_ev.png                CA energy balance – generation panel (EV)
  ca_balance_import_baseline.png       net import/export (baseline)
  ca_balance_import_ev.png             net import/export (EV)
  ca_balance_storage_baseline.png      storage charge/discharge (baseline)
  ca_balance_storage_ev.png            storage charge/discharge (EV)
  ca_balance_residual_baseline.png     energy balance check (baseline)
  ca_balance_residual_ev.png           energy balance check (EV)
  ca_chart_gap_explained.png           original-vs-corrected chart gap
  transmission_saturation.png          two-way corridor saturation (EV scenario)
  marginal_gen_by_fuel_hourly.png      hourly marginal generation by fuel (all + WECC)
  fuel_hourly_timeseries/              per-fuel hourly marginal generation PNGs
  period_consequential_gen.png         period bar chart (consequential gen by fuel, WECC)
  hourly_co2_intensity_wecc.png        hourly consequential CO2 intensity (gCO2/kWh)
  marginal_unit_fuel_24x7.png          fuel of last (marginal) plant dispatched grid
  state_rps_fulfillment.png            state-level RPS jurisdiction stacked bar
  ─────────────────────────────────────────────────────────────────
  Summary plots (from previous version, kept)
  wecc_stacked_gen_baseline/ev.png
  wecc_co2_stacked_by_fuel.png
  wecc_co2_intensity.png
  capex_expansion_*.png
  marginal_co2_by_fuel.png
  consequential_emission_factor.png
  regions/region_*.png
  transmission_top20_baseline.png
"""

import os
import sys
import glob
import json
import textwrap
import warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.colors import ListedColormap
from matplotlib.patches import Patch

warnings.filterwarnings('ignore')

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ev_charging_project.config as config


# ===========================================================================
# Constants
# ===========================================================================

FUEL_COLORS = {
    'nuclear':     '#7D3C98',
    'hydro':       '#2980B9',
    'pump hydro':  '#5DADE2',
    'geothermal':  '#C0392B',
    'biomass':     '#27AE60',
    'wind':        '#1ABC9C',
    'solar':       '#F1C40F',
    'battery':     '#95A5A6',
    'natural gas': '#E67E22',
    'coal':        '#2C3E50',
    'oil':         '#34495E',
    'waste':       '#8E44AD',
    'non-fossil':  '#D35400',
    'import':      '#16A085',
    'unknown':     '#BDC3C7',
}

# Notebook-faithful fuel order for stacked plots
FUEL_ORDER = [
    'nuclear', 'hydro', 'pump hydro', 'geothermal', 'biomass',
    'natural gas', 'coal', 'oil', 'waste',
    'battery', 'import', 'non-fossil', 'unknown',
    'wind', 'solar',
]

EMISSION_FACTORS = {
    'coal':         {'co2': 3.36e-7, 'nox': 1.5e-9,  'so2': 3.0e-9},
    'natural gas':  {'co2': 2.0e-7,  'nox': 5.0e-10, 'so2': 1.0e-11},
    'oil':          {'co2': 2.7e-7,  'nox': 1.2e-9,  'so2': 2.5e-9},
    'biomass':      {'co2': 9.3e-8,  'nox': 5.0e-10, 'so2': 1.0e-10},
    'waste':        {'co2': 1.0e-7,  'nox': 6.0e-10, 'so2': 2.0e-10},
}

REGION_TO_STATE = {
    'WEC_BANC': 'CA', 'WEC_CALN': 'CA', 'WEC_LADW': 'CA',
    'WEC_SDGE': 'CA', 'WECC_IID': 'CA', 'WECC_SCE': 'CA',
    'WECC_AZ': 'AZ', 'WECC_NM': 'NM', 'WECC_CO': 'CO',
    'WECC_ID': 'ID', 'WECC_MT': 'MT', 'WECC_NV': 'NV',
    'WECC_OR': 'OR', 'WECC_UT': 'UT', 'WECC_WA': 'WA',
    'WECC_WY': 'WY', 'WECC_SD': 'SD', 'WECC_NE': 'NE',
    'WECC_TX': 'TX',
}

RENEWABLE_FUELS = {'wind', 'solar', 'hydro', 'geothermal', 'biomass', 'non-fossil'}

_STORAGE_FUELS = ('battery', 'pump hydro')


# ===========================================================================
# Helpers
# ===========================================================================

def _scalar(v):
    if isinstance(v, (list, tuple, np.ndarray)):
        return float(v[0]) if len(v) else 0.0
    return float(v or 0.0)


def _reindex_hours(df, col, num_hours):
    """Reindex a Series/DataFrame to cover all hours 0..num_hours-1."""
    return df.reindex(range(num_hours), fill_value=0)


def _fuel_stack_order(available_fuels):
    """Return fuels in preferred stack order, extras appended."""
    ordered = [f for f in FUEL_ORDER if f in available_fuels]
    for f in sorted(available_fuels):
        if f not in ordered:
            ordered.append(f)
    return ordered


# ===========================================================================
# 1. Generation extraction
# ===========================================================================

def extract_generation_by_fuel(solution, graph):
    """
    Return DataFrame:
      hour, region, asset, fuel, generation_kwh,
      co2_factor, nox_factor, so2_factor
    """
    rows = []
    for source, node in solution._node.items():
        gr_assets = graph._node.get(source, {}).get('assets', {}) if graph else {}
        for handle, asset in node.get('assets', {}).items():
            if 'base_load' in handle or asset.get('type') == 'load':
                continue
            generation = asset.get('net', [])
            if not generation:
                continue
            gr_asset    = gr_assets.get(handle, {})
            asset_class = gr_asset.get('_class')
            fuel = asset.get('fuel') or gr_asset.get('fuel')
            if fuel is None and asset_class == 'Store':
                fuel = ('pump hydro'
                        if 'pump' in handle.lower() or 'hydro' in handle.lower()
                        else 'battery')
            fuel = str(fuel or 'unknown').lower()

            is_storage = (asset_class == 'Store') or (fuel in _STORAGE_FUELS)
            if is_storage:
                generation = [max(0.0, g) for g in generation]
                if sum(generation) == 0:
                    continue

            co2 = gr_asset.get('co2')
            nox = gr_asset.get('nox')
            so2 = gr_asset.get('so2')
            if co2 is None:
                fb  = EMISSION_FACTORS.get(fuel, {})
                co2 = fb.get('co2', 0.0)
                nox = fb.get('nox', 0.0)
                so2 = fb.get('so2', 0.0)

            for hour, gen_w in enumerate(generation):
                rows.append({
                    'hour': hour, 'region': source, 'asset': handle, 'fuel': fuel,
                    'generation_kwh': gen_w / 1e3,
                    'co2_factor': float(co2 or 0),
                    'nox_factor': float(nox or 0),
                    'so2_factor': float(so2 or 0),
                })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['hour','region','asset','fuel','generation_kwh',
                 'co2_factor','nox_factor','so2_factor'])


# ===========================================================================
# 2. Region / aggregate balance helper
# ===========================================================================

def _collect_region_balance(solution, graph, region, num_hours):
    """Return per-hour GW-scale balance dict for one region."""
    load_W = np.zeros(num_hours); prod_W = np.zeros(num_hours)
    dis_W  = np.zeros(num_hours); chg_W  = np.zeros(num_hours)
    sf_Ws  = np.zeros(num_hours); wst_Ws = np.zeros(num_hours)

    node_sol = solution._node.get(region, {})
    node_gr  = graph._node.get(region, {}) if graph else {}

    short = np.array(node_sol.get('shortfall', [0]*num_hours), dtype=float)
    waste = np.array(node_sol.get('wastage',   [0]*num_hours), dtype=float)
    if len(short) == num_hours: sf_Ws  = short
    if len(waste) == num_hours: wst_Ws = waste

    for handle, asset in node_sol.get('assets', {}).items():
        net = asset.get('net', [])
        if not net or len(net) != num_hours:
            continue
        net_arr  = np.array(net, dtype=float)
        gr_asset = node_gr.get('assets', {}).get(handle, {})
        acls     = gr_asset.get('_class', '')
        fuel     = (gr_asset.get('fuel', '') or '').lower()
        if 'base_load' in handle.lower() or gr_asset.get('type') == 'load':
            load_W += np.abs(net_arr)
        elif acls == 'Store' or fuel in _STORAGE_FUELS:
            dis_W += np.maximum(net_arr, 0)
            chg_W += np.abs(np.minimum(net_arr, 0))
        else:
            prod_W += net_arr

    imp_W = np.zeros(num_hours); exp_W = np.zeros(num_hours)
    for src, adj in solution._adj.items():
        for tgt, edge in adj.items():
            for _, line in edge.get('lines', {}).items():
                tr = line.get('transmission', [])
                if not tr or len(tr) != num_hours:
                    continue
                eff   = line.get('efficiency', 1.0)
                t_arr = np.array(tr, dtype=float)
                if tgt == region and src != region:
                    imp_W += t_arr * eff
                elif src == region and tgt != region:
                    exp_W += t_arr

    d = {
        'load_GW':      load_W / 1e9, 'producer_GW':  prod_W / 1e9,
        'discharge_GW': dis_W  / 1e9, 'charge_GW':    chg_W  / 1e9,
        'import_GW':    imp_W  / 1e9, 'export_GW':    exp_W  / 1e9,
        'shortfall_GW': sf_Ws  / (1e9 * 3600),
        'wastage_GW':   wst_Ws / (1e9 * 3600),
    }
    d['storage_net_GW'] = d['discharge_GW'] - d['charge_GW']
    return d


def _collect_region_set_balance(solution, graph, regions_set, num_hours):
    """
    Aggregate balance arrays over a *set* of regions, also returning
    per-store details and gross import/export across the region boundary.
    """
    load_W = np.zeros(num_hours);  prod_W = np.zeros(num_hours)
    dis_W  = np.zeros(num_hours);  chg_W  = np.zeros(num_hours)
    sf_Ws  = np.zeros(num_hours);  wst_Ws = np.zeros(num_hours)
    imp_W  = np.zeros(num_hours);  exp_W  = np.zeros(num_hours)
    store_details = []

    for region in regions_set:
        node_sol = solution._node.get(region, {})
        node_gr  = graph._node.get(region, {}) if graph else {}

        short = np.array(node_sol.get('shortfall', [0]*num_hours), dtype=float)
        waste = np.array(node_sol.get('wastage',   [0]*num_hours), dtype=float)
        if len(short) == num_hours: sf_Ws  += short
        if len(waste) == num_hours: wst_Ws += waste

        for handle, asset in node_sol.get('assets', {}).items():
            net = asset.get('net', [])
            if not net or len(net) != num_hours:
                continue
            net_arr  = np.array(net, dtype=float)
            gr_asset = node_gr.get('assets', {}).get(handle, {})
            acls     = gr_asset.get('_class', '')
            fuel     = (gr_asset.get('fuel', '') or '').lower()
            if 'base_load' in handle.lower() or gr_asset.get('type') == 'load':
                load_W += np.abs(net_arr)
            elif acls == 'Store' or fuel in _STORAGE_FUELS:
                d_arr = np.maximum(net_arr, 0)
                c_arr = np.abs(np.minimum(net_arr, 0))
                dis_W += d_arr; chg_W += c_arr
                prod = np.array(asset.get('production',  [0]*num_hours), dtype=float)
                cons = np.array(asset.get('consumption', [0]*num_hours), dtype=float)
                lvl  = np.array(asset.get('level',       [0]*num_hours), dtype=float)
                cap  = gr_asset.get('installed_capacity', 0)
                store_details.append({
                    'region': region, 'handle': handle, 'fuel': fuel,
                    'capacity_MW': cap / 1e6,
                    'production': prod, 'consumption': cons, 'level': lvl,
                })
            else:
                prod_W += net_arr

    # Gross imports/exports crossing the set boundary
    for src, adj in solution._adj.items():
        for tgt, edge in adj.items():
            src_in = src in regions_set; tgt_in = tgt in regions_set
            if src_in == tgt_in:
                continue   # both inside or both outside
            for _, line in edge.get('lines', {}).items():
                tr = line.get('transmission', [])
                if not tr or len(tr) != num_hours:
                    continue
                eff   = line.get('efficiency', 1.0)
                t_arr = np.array(tr, dtype=float)
                if tgt_in and not src_in:
                    imp_W += t_arr * eff
                elif src_in and not tgt_in:
                    exp_W += t_arr

    return {
        'load_W': load_W, 'prod_W': prod_W,
        'dis_W': dis_W,   'chg_W': chg_W,
        'sf_Ws': sf_Ws,   'wst_Ws': wst_Ws,
        'imp_W': imp_W,   'exp_W': exp_W,
        'store_details': store_details,
    }


# ===========================================================================
# 3. CA energy balance panels  (notebook-faithful, each panel = separate PNG)
# ===========================================================================

def plot_ca_energy_balance_panels(
        baseline_solution, baseline_graph,
        ev_solution, ev_graph,
        baseline_gen, ev_gen,
        ev_charging_load_slice,
        num_hours, output_dir, cal_regions=None, month_label=''):
    """
    Port of notebook CA energy balance diagnostic.
    Saves every sub-panel as a separate PNG file.
    """
    os.makedirs(output_dir, exist_ok=True)
    hrs = np.arange(num_hours)

    if cal_regions is None:
        cal_regions = config.CALIFORNIA_REGIONS
    CAL_SET = set(cal_regions)

    ev_charge_load_GW = (
        np.array(ev_charging_load_slice) / 1e9
        if ev_charging_load_slice is not None and len(ev_charging_load_slice) == num_hours
        else np.zeros(num_hours)
    )

    def _gw(bal, key):   return bal[key + '_W'] / 1e9
    def _gw_e(bal, key): return bal[key + '_Ws'] / (1e9 * 3600)

    for solution, graph, gen_df, scenario, label in [
        (baseline_solution, baseline_graph, baseline_gen, 'baseline', 'Baseline'),
        (ev_solution,       ev_graph,       ev_gen,       'ev',       'EV Scenario'),
    ]:
        bal = _collect_region_set_balance(solution, graph, CAL_SET, num_hours)
        load_GW  = _gw(bal, 'load');   prod_GW  = _gw(bal, 'prod')
        dis_GW   = _gw(bal, 'dis');    chg_GW   = _gw(bal, 'chg')
        imp_GW   = _gw(bal, 'imp');    exp_GW   = _gw(bal, 'exp')
        sf_GW    = _gw_e(bal, 'sf');   wst_GW   = _gw_e(bal, 'wst')
        net_imp  = imp_GW - exp_GW
        stor_net = dis_GW - chg_GW
        balance  = prod_GW + stor_net + imp_GW - exp_GW + sf_GW - wst_GW - load_GW

        # Per-fuel stacking from gen DataFrame
        ca_gen = gen_df[gen_df['region'].isin(CAL_SET)].copy() if not gen_df.empty else pd.DataFrame()
        if not ca_gen.empty:
            pivot = (ca_gen.groupby(['hour', 'fuel'])['generation_kwh'].sum().reset_index()
                     .pivot(index='hour', columns='fuel', values='generation_kwh')
                     .reindex(hrs, fill_value=0).fillna(0))
            non_st = [f for f in pivot.columns if f not in _STORAGE_FUELS]
            gen_by_fuel = pivot[non_st] / 1e6  # kWh→GW(1-hr)
            stack_order = [f for f in FUEL_ORDER if f in gen_by_fuel.columns]
            for f in gen_by_fuel.columns:
                if f not in stack_order and gen_by_fuel[f].abs().max() > 0:
                    stack_order.append(f)
        else:
            gen_by_fuel = pd.DataFrame(index=hrs)
            stack_order = []

        # ── Panel A: stacked generation ──────────────────────────────────────
        fig, ax = plt.subplots(figsize=(18, 7))
        if stack_order:
            ys = [gen_by_fuel[f].values for f in stack_order]
            cs = [FUEL_COLORS.get(f, '#CCC') for f in stack_order]
            ax.stackplot(hrs, *ys, labels=stack_order, colors=cs, alpha=0.8)
            bottom = np.sum(np.array(ys), axis=0)
        else:
            bottom = np.zeros(num_hours)

        ax.fill_between(hrs, bottom, bottom + dis_GW,
                        color=FUEL_COLORS['battery'], alpha=0.6, label='Storage discharge')
        b2 = bottom + dis_GW
        ax.fill_between(hrs, b2, b2 + imp_GW,
                        color='#16A085', alpha=0.5, label='Gross imports')
        ax.fill_between(hrs, 0, -chg_GW,
                        color='#F39C12', alpha=0.6, label='Storage charging')
        ax.fill_between(hrs, -chg_GW, -chg_GW - exp_GW,
                        color='#E74C3C', alpha=0.5, label='Gross exports')

        if scenario == 'ev':
            ax.plot(hrs, load_GW - ev_charge_load_GW, 'k-',  lw=2.5, label='CA base load (baseline)')
            ax.plot(hrs, load_GW,                     'm--', lw=2.0, label='CA base load + EV charging')
        else:
            ax.plot(hrs, load_GW, 'k-', lw=2.5, label='Load (demand)')

        sf_mask = sf_GW > 1e-4
        if sf_mask.any():
            ax.fill_between(hrs, 0, sf_GW, where=sf_mask,
                            color='red', alpha=0.4, label=f'Shortfall ({sf_mask.sum()} hrs)')
        ax.axhline(0, color='gray', lw=0.5)
        ax.set_title(f'California: Energy Balance — {label} ({month_label})\n'
                     'Positive = supply | Negative = charging / exports',
                     fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Power (GW)')
        ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1), fontsize=8)
        ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3, axis='y')
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'ca_balance_gen_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

        # ── Panel B: net import/export ────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(18, 4))
        ax.fill_between(hrs, net_imp, where=net_imp >= 0,
                        color='#16A085', alpha=0.5, label='Net import (CA receiving)')
        ax.fill_between(hrs, net_imp, where=net_imp < 0,
                        color='#E74C3C', alpha=0.5, label='Net export (CA sending)')
        ax.plot(hrs, imp_GW,  'g--', lw=1, alpha=0.7, label='Gross import')
        ax.plot(hrs, -exp_GW, 'r--', lw=1, alpha=0.7, label='-Gross export')
        ax.axhline(0, color='black', lw=0.8)
        ax.set_title(f'California: Net Import/Export — {label} ({month_label})',
                     fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Power (GW)')
        ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1), fontsize=8)
        ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'ca_balance_import_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

        # ── Panel C: storage charge/discharge + level ─────────────────────────
        fig, ax = plt.subplots(figsize=(18, 4))
        ax.fill_between(hrs, 0, dis_GW,  color='#27AE60', alpha=0.7, label='Discharge (to grid)')
        ax.fill_between(hrs, 0, -chg_GW, color='#E67E22', alpha=0.7, label='Charge (from grid)')
        ax.axhline(0, color='gray', lw=0.5)
        ax.set_title(f'California: Storage Charge/Discharge — {label} ({month_label})',
                     fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Power (GW)')
        ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1), fontsize=8)
        ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3)
        if bal['store_details']:
            axb = ax.twinx()
            for sd in bal['store_details']:
                lvl_gwh = sd['level'] / (1e9 * 3600)
                axb.plot(hrs, lvl_gwh, '--', lw=1, alpha=0.7,
                         label=f"{sd['fuel']} ({sd['region']})")
            axb.set_ylabel('Stored Energy (GWh)', fontsize=9)
            axb.legend(loc='upper right', bbox_to_anchor=(1.22, 0.7), fontsize=7)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'ca_balance_storage_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

        # ── Panel D: balance residual ─────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(18, 3))
        ax.plot(hrs, balance * 1000, 'b-', lw=1, label='Balance residual (MW)')
        ax.axhline(0, color='red', lw=0.8, ls='--')
        ax.set_title(f'Energy Balance Check — {label} ({month_label})  '
                     '(gen+storage+import−export+shortfall−wastage−load ≈ 0)',
                     fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Residual (MW)')
        ax.legend(fontsize=9); ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'ca_balance_residual_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

    # ── Chart-gap-explained ───────────────────────────────────────────────────
    bal_b = _collect_region_set_balance(baseline_solution, baseline_graph, CAL_SET, num_hours)
    load_b   = bal_b['load_W'] / 1e9
    prod_b   = bal_b['prod_W'] / 1e9
    dis_b    = bal_b['dis_W']  / 1e9
    chg_b    = bal_b['chg_W']  / 1e9
    imp_b    = bal_b['imp_W']  / 1e9
    exp_b    = bal_b['exp_W']  / 1e9
    sf_b     = bal_b['sf_Ws']  / (1e9 * 3600)
    wst_b    = bal_b['wst_Ws'] / (1e9 * 3600)
    net_imp_b = imp_b - exp_b
    chart_supply  = prod_b + dis_b + net_imp_b
    true_supply   = prod_b + (dis_b - chg_b) + net_imp_b + sf_b - wst_b

    fig, (ax_old, ax_new) = plt.subplots(2, 1, figsize=(18, 10))
    ax_old.fill_between(hrs, chart_supply, alpha=0.4, color='steelblue',
                        label='Chart total (gen+discharge+net_import)')
    ax_old.plot(hrs, load_b, 'k-', lw=2, label='Load')
    ax_old.fill_between(hrs, load_b, chart_supply, where=load_b > chart_supply,
                        alpha=0.3, color='red', label='Gap: load > chart (shortfall)')
    ax_old.fill_between(hrs, load_b, chart_supply, where=chart_supply > load_b,
                        alpha=0.3, color='green', label='Gap: chart > load (hidden charging)')
    ax_old.set_title(f'Why the original CA chart is misleading ({month_label})\n'
                     'Red = shortfall | Green = hidden storage charging', fontweight='bold')
    ax_old.set_ylabel('Power (GW)'); ax_old.legend(fontsize=9)
    ax_old.set_xlim(0, num_hours - 1); ax_old.grid(True, alpha=0.3)

    ax_new.fill_between(hrs, true_supply, alpha=0.4, color='steelblue',
                        label='True supply (gen+storage_net+net_import+shortfall−wastage)')
    ax_new.plot(hrs, load_b, 'k-', lw=2, label='Load')
    ax_new.set_title('Corrected: true supply exactly equals load', fontweight='bold')
    ax_new.set_ylabel('Power (GW)'); ax_new.set_xlabel('Hour')
    ax_new.legend(fontsize=9); ax_new.set_xlim(0, num_hours - 1)
    ax_new.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, 'ca_chart_gap_explained.png'),
                dpi=150, bbox_inches='tight')
    plt.close(fig)

    print(f'  CA energy balance panels saved to: {output_dir}')


# ===========================================================================
# 4. WECC energy balance panels  (notebook-faithful, cell 25 — each panel separate PNG)
# ===========================================================================

def get_wecc_stackplot_bundle(
        baseline_solution, baseline_graph,
        ev_solution, ev_graph,
        baseline_gen, ev_gen,
        ev_charging_load_slice,
        num_hours):
    """
    Closed-system WECC data for the generation/stack panel (fuels, storage, loads).
    Shared by plot_wecc_energy_balance_panels and aggregate_postprocess.
    """
    hrs = np.arange(num_hours)
    wecc_set_b = {r for r in baseline_solution._node if str(r).startswith('WEC')}
    wecc_set_e = {r for r in ev_solution._node if str(r).startswith('WEC')}

    ev_charge_load_GW = (
        np.asarray(ev_charging_load_slice, dtype=float) / 1e9
        if ev_charging_load_slice is not None and len(ev_charging_load_slice) == num_hours
        else np.zeros(num_hours)
    )

    def _wecc_closed_balance(solution, graph, wecc_set):
        load_W = np.zeros(num_hours);  prod_W = np.zeros(num_hours)
        dis_W  = np.zeros(num_hours);  chg_W  = np.zeros(num_hours)
        sf_Ws  = np.zeros(num_hours);  wst_Ws = np.zeros(num_hours)
        store_details = []

        for region in wecc_set:
            node_sol = solution._node.get(region, {})
            node_gr  = graph._node.get(region, {}) if graph else {}

            short = np.array(node_sol.get('shortfall', [0]*num_hours), dtype=float)
            waste = np.array(node_sol.get('wastage',   [0]*num_hours), dtype=float)
            if len(short) == num_hours: sf_Ws  += short
            if len(waste) == num_hours: wst_Ws += waste

            for handle, asset in node_sol.get('assets', {}).items():
                net = asset.get('net', [])
                if not net or len(net) != num_hours:
                    continue
                net_arr  = np.array(net, dtype=float)
                gr_asset = node_gr.get('assets', {}).get(handle, {})
                acls     = gr_asset.get('_class', '')
                fuel     = (gr_asset.get('fuel', '') or '').lower()
                if 'base_load' in handle.lower() or gr_asset.get('type') == 'load':
                    load_W += np.abs(net_arr)
                elif acls == 'Store' or fuel in _STORAGE_FUELS:
                    d_arr = np.maximum(net_arr, 0)
                    c_arr = np.abs(np.minimum(net_arr, 0))
                    dis_W += d_arr; chg_W += c_arr
                    prod = np.array(asset.get('production',  [0]*num_hours), dtype=float)
                    cons = np.array(asset.get('consumption', [0]*num_hours), dtype=float)
                    lvl  = np.array(asset.get('level',       [0]*num_hours), dtype=float)
                    cap  = gr_asset.get('installed_capacity', 0)
                    store_details.append({
                        'region': region, 'handle': handle, 'fuel': fuel,
                        'capacity_MW': cap / 1e6,
                        'production': prod, 'consumption': cons, 'level': lvl,
                    })
                else:
                    prod_W += net_arr

        load_GW = load_W / 1e9;    prod_GW = prod_W / 1e9
        dis_GW  = dis_W  / 1e9;    chg_GW  = chg_W  / 1e9
        sf_GW   = sf_Ws  / (1e9 * 3600)
        wst_GW  = wst_Ws / (1e9 * 3600)
        stor_net = dis_GW - chg_GW
        balance  = prod_GW + stor_net + sf_GW - wst_GW - load_GW
        return dict(
            load_GW=load_GW, prod_GW=prod_GW,
            dis_GW=dis_GW,   chg_GW=chg_GW,
            sf_GW=sf_GW,     wst_GW=wst_GW,
            stor_net_GW=stor_net, balance=balance,
            store_details=store_details,
        )

    b = _wecc_closed_balance(baseline_solution, baseline_graph, wecc_set_b)
    e = _wecc_closed_balance(ev_solution,       ev_graph,       wecc_set_e)

    def _fuel_pivot(gen_df, wecc_set):
        fg = gen_df[gen_df['region'].isin(wecc_set)].copy() if not gen_df.empty else pd.DataFrame()
        if fg.empty:
            return pd.DataFrame(index=hrs), []
        pivot = (
            fg.groupby(['hour', 'fuel'])['generation_kwh'].sum().reset_index()
            .pivot(index='hour', columns='fuel', values='generation_kwh')
            .reindex(hrs, fill_value=0).fillna(0)
        )
        non_st = [f for f in pivot.columns if f not in _STORAGE_FUELS]
        gdf    = pivot[non_st] / 1e6   # kWh → GW (1-h steps)
        fo     = [f for f in FUEL_ORDER if f in gdf.columns and gdf[f].abs().max() > 0]
        for f in gdf.columns:
            if f not in fo and gdf[f].abs().max() > 0:
                fo.append(f)
        return gdf, fo

    b_gdf, b_fo = _fuel_pivot(baseline_gen, wecc_set_b)
    e_gdf, e_fo = _fuel_pivot(ev_gen,       wecc_set_e)

    return dict(
        hrs=hrs,
        b=b, e=e,
        b_gdf=b_gdf, b_fo=b_fo,
        e_gdf=e_gdf, e_fo=e_fo,
        ev_charge_load_GW=ev_charge_load_GW,
    )


def plot_wecc_energy_balance_panels(
        baseline_solution, baseline_graph,
        ev_solution, ev_graph,
        baseline_gen, ev_gen,
        ev_charging_load_slice,
        num_hours, output_dir, month_label=''):
    """
    Port of notebook WECC energy balance diagnostic (cell 25).
    WECC is treated as a *closed system* — no imports/exports in the balance.
    Saves each panel as a separate PNG for both baseline and EV scenarios:
      wecc_balance_gen_baseline.png / wecc_balance_gen_ev.png
      wecc_balance_import_baseline.png / wecc_balance_import_ev.png  (all-zero panel)
      wecc_balance_storage_baseline.png / wecc_balance_storage_ev.png
      wecc_balance_residual_baseline.png / wecc_balance_residual_ev.png
    """
    os.makedirs(output_dir, exist_ok=True)
    bundle = get_wecc_stackplot_bundle(
        baseline_solution, baseline_graph,
        ev_solution, ev_graph,
        baseline_gen, ev_gen,
        ev_charging_load_slice,
        num_hours,
    )
    hrs = bundle['hrs']
    b, e = bundle['b'], bundle['e']
    b_gdf, b_fo = bundle['b_gdf'], bundle['b_fo']
    e_gdf, e_fo = bundle['e_gdf'], bundle['e_fo']
    ev_charge_load_GW = bundle['ev_charge_load_GW']

    for bal, gdf, fo, scenario, label, extra_load in [
        (b, b_gdf, b_fo, 'baseline', 'Baseline',    None),
        (e, e_gdf, e_fo, 'ev',       'EV Scenario', b['load_GW']),
    ]:
        # ── Panel A: stacked generation ──────────────────────────────────────
        fig, ax = plt.subplots(figsize=(18, 7))
        if fo:
            ys = [gdf[f].values for f in fo]
            cs = [FUEL_COLORS.get(f, '#CCC') for f in fo]
            ax.stackplot(hrs, *ys, labels=fo, colors=cs, alpha=0.8)
            bottom = np.sum(np.array(ys), axis=0)
        else:
            bottom = np.zeros(num_hours)

        ax.fill_between(hrs, bottom, bottom + bal['dis_GW'],
                        color=FUEL_COLORS['battery'], alpha=0.6, label='Storage discharge')
        ax.fill_between(hrs, 0, -bal['chg_GW'],
                        color='#F39C12', alpha=0.6, label='Storage charging')

        if extra_load is not None:
            # EV scenario: show baseline load (black) and baseline+EV (magenta)
            ax.plot(hrs, extra_load,                    'k-',  lw=2.5, label='WECC base load (baseline)')
            ax.plot(hrs, extra_load + ev_charge_load_GW,'m--', lw=2.0, label='WECC base load + EV charging')
        else:
            ax.plot(hrs, bal['load_GW'], 'k-', lw=2.5, label='Load (demand)')

        sf_mask = bal['sf_GW'] > 0.01
        if sf_mask.any():
            ax.scatter(hrs[sf_mask], bal['load_GW'][sf_mask],
                       color='red', s=30, zorder=5, label=f'Shortfall ({sf_mask.sum()} hrs)')

        ax.axhline(0, color='gray', lw=0.5)
        ax.set_title(f'WECC Energy Balance — {label} ({month_label})\n'
                     'No imports/exports — closed system', fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Power (GW)')
        ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1), fontsize=8)
        ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3, axis='y')
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'wecc_balance_gen_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

        # ── Panel B: net import/export (zero — closed system) ────────────────
        fig, ax = plt.subplots(figsize=(18, 3))
        ax.axhline(0, color='black', lw=1.2)
        ax.set_ylim(-1, 1)
        ax.set_title(f'WECC Net Import/Export — {label} ({month_label})  '
                     '(zero: WECC is a closed system)', fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Power (GW)')
        ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'wecc_balance_import_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

        # ── Panel C: storage charge/discharge + level ─────────────────────────
        fig, ax = plt.subplots(figsize=(18, 4))
        ax.fill_between(hrs, 0,  bal['dis_GW'], color='#27AE60', alpha=0.7, label='Discharge (to grid)')
        ax.fill_between(hrs, 0, -bal['chg_GW'], color='#E67E22', alpha=0.7, label='Charge (from grid)')
        ax.axhline(0, color='gray', lw=0.5)
        ax.set_title(f'WECC Storage Charge/Discharge — {label} ({month_label})', fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Power (GW)')
        ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1), fontsize=8)
        ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3)
        if bal['store_details']:
            axb = ax.twinx()
            for sd in bal['store_details']:
                lvl_gwh = sd['level'] / (1e9 * 3600)
                axb.plot(hrs, lvl_gwh, '--', lw=0.8, alpha=0.6,
                         label=f"{sd['fuel']} ({sd['region']})")
            axb.set_ylabel('Stored Energy (GWh)', fontsize=9)
            axb.legend(loc='upper right', bbox_to_anchor=(1.22, 0.7), fontsize=6)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'wecc_balance_storage_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

        # ── Panel D: balance residual ─────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(18, 3))
        ax.plot(hrs, bal['balance'] * 1000, 'b-', lw=1, label='Balance residual (MW)')
        ax.axhline(0, color='red', lw=0.8, ls='--')
        ax.set_title(f'WECC Energy Balance Check — {label} ({month_label})  '
                     '(gen+storage_net+shortfall−wastage−load ≈ 0)', fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('Residual (MW)')
        ax.legend(fontsize=9); ax.set_xlim(0, num_hours - 1); ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, f'wecc_balance_residual_{scenario}.png'),
                    dpi=150, bbox_inches='tight')
        plt.close(fig)

    print(f'  WECC energy balance panels saved to: {output_dir}')


# ===========================================================================
# 5. Two-way transmission corridor saturation  (notebook-faithful)
# ===========================================================================

def plot_transmission_saturation(ev_solution, num_hours, output_dir, month_label=''):
    """
    Port of notebook two-way transmission saturation analysis.
    Plots per-corridor saturation % time-series for corridors with ≥20% peak.
    """
    os.makedirs(output_dir, exist_ok=True)
    hrs = np.arange(num_hours)

    corridors = {}
    for source, target, edge_data in ev_solution.edges(data=True):
        for line_handle, line in edge_data.get('lines', {}).items():
            flow = line.get('transmission', [])
            if not flow:
                continue
            cap = float(line.get('installed_capacity', 0) or 0)
            capex_v = line.get('capex', 0)
            if isinstance(capex_v, (list, np.ndarray)):
                capex_v = float(capex_v[0]) if len(capex_v) else 0
            capacity = cap + float(capex_v or 0)
            key = (min(source, target), max(source, target))
            corridors.setdefault(key, []).append({
                'source': source, 'target': target, 'line': line_handle,
                'capacity_MW': capacity / 1e6,
                'flow_MW': np.array(flow, dtype=float) / 1e6,
            })

    # Corridors with ≥20% peak saturation
    plot_corr = {
        k: v for k, v in sorted(corridors.items())
        if any(l['capacity_MW'] > 0 and
               l['flow_MW'].max() / l['capacity_MW'] >= 0.20
               for l in v)
    }

    # Simultaneous two-way flow report
    sim_lines = []
    for key, lines in sorted(corridors.items()):
        fwd = [l for l in lines if l['source'] == key[0]]
        rev = [l for l in lines if l['source'] == key[1]]
        if not fwd or not rev:
            continue
        ff = sum(l['flow_MW'] for l in fwd)
        rf = sum(l['flow_MW'] for l in rev)
        both = int(np.sum((ff > 0.01) & (rf > 0.01)))
        if both > 0:
            sim_lines.append(f'  {key[0]} <-> {key[1]}: simultaneous flow in '
                             f'{both}/{num_hours} hours')

    if not plot_corr:
        print('  Transmission saturation: no corridors ≥20% peak saturation.')
        return

    ncols = 3
    nrows = int(np.ceil(len(plot_corr) / ncols))
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(6 * ncols, 3.5 * nrows), squeeze=False)

    for idx, (key, lines) in enumerate(sorted(plot_corr.items())):
        ax = axes[idx // ncols][idx % ncols]
        for l in lines:
            direction = f"{l['source']}→{l['target']}"
            sat = (l['flow_MW'] / l['capacity_MW'] * 100
                   if l['capacity_MW'] > 0 else l['flow_MW'] * 0)
            ax.plot(hrs, sat, label=f"{direction} ({l['capacity_MW']:.0f} MW)", lw=1)
        ax.set_title(f"{key[0]} ↔ {key[1]}", fontsize=9)
        ax.set_ylabel('Saturation (%)', fontsize=8)
        ax.set_xlabel('Hour', fontsize=8)
        ax.set_ylim(-5, 110)
        ax.axhline(100, color='red',    ls='--', lw=0.7, alpha=0.5)
        ax.axhline(80,  color='orange', ls=':',  lw=0.7, alpha=0.5)
        ax.legend(fontsize=6); ax.grid(True, alpha=0.2)
        ax.tick_params(labelsize=7)

    for idx in range(len(plot_corr), nrows * ncols):
        axes[idx // ncols][idx % ncols].set_visible(False)

    note = ''
    if sim_lines:
        note = '\n(simultaneous two-way flow corridors: ' + ', '.join(
            f"{l.strip().split(':')[0]}" for l in sim_lines[:3]) + ('…' if len(sim_lines) > 3 else '') + ')'
    fig.suptitle(f'Transmission Corridor Saturation — EV Scenario ({month_label}){note}',
                 fontsize=12, fontweight='bold')
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    fpath = os.path.join(output_dir, 'transmission_saturation.png')
    plt.savefig(fpath, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  Saved: {fpath}  ({len(plot_corr)} corridors ≥20%)')
    if sim_lines:
        print('\n'.join(['  Simultaneous two-way flow:'] + sim_lines))


# ===========================================================================
# 5. Marginal generation by fuel  (notebook-faithful, cell 48)
# ===========================================================================

def plot_marginal_generation_by_fuel(marginal_df, hourly_results, num_hours,
                                     output_dir, month_label=''):
    """
    Port of notebook cell 48.
    Saves:
      - marginal_gen_by_fuel_hourly.png         (all regions combined)
      - marginal_gen_by_fuel_hourly_wecc.png    (WECC regions only)
      - fuel_hourly_timeseries/hourly_marginal_<fuel>.png
      - fuel_hourly_timeseries_wecc/...
    """
    os.makedirs(output_dir, exist_ok=True)

    def _plot_combined(hr_df, title, fpath):
        fuel_totals = hr_df.groupby('fuel')['marginal_generation_kwh'].sum()
        nonzero = [f for f, v in fuel_totals.items() if abs(v) > 1e-9]
        fig, ax = plt.subplots(figsize=(14, 6))
        for fuel in sorted(nonzero):
            fd = hr_df[hr_df['fuel'] == fuel]
            ax.plot(fd['hour'], fd['marginal_generation_kwh'] / 1e6,
                    label=fuel, marker='o', markersize=3)
        ax.set_xlabel('Hour'); ax.set_ylabel('Marginal Generation (GWh)')
        ax.set_title(title, fontweight='bold')
        ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(fpath, dpi=150, bbox_inches='tight')
        plt.close(fig)

    def _plot_per_fuel(hr_df, subdir, title_suffix):
        os.makedirs(subdir, exist_ok=True)
        fuel_totals = hr_df.groupby('fuel')['marginal_generation_kwh'].sum()
        for fuel, v in fuel_totals.items():
            if abs(v) <= 1e-9:
                continue
            fd = hr_df[hr_df['fuel'] == fuel]
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(fd['hour'], fd['marginal_generation_kwh'] / 1e6,
                    label=fuel, marker='o', color='tab:blue')
            ax.set_xlabel('Hour'); ax.set_ylabel('Marginal Generation (GWh)')
            ax.set_title(f'Hourly Marginal Generation — {fuel} {title_suffix}',
                         fontweight='bold')
            ax.grid(True, alpha=0.3); ax.legend()
            plt.tight_layout()
            plt.savefig(os.path.join(subdir, f'hourly_marginal_{fuel}.png'),
                        dpi=150, bbox_inches='tight')
            plt.close(fig)

    # All regions
    _plot_combined(
        hourly_results,
        f'Marginal Generation by Fuel Type — {month_label}',
        os.path.join(output_dir, 'marginal_gen_by_fuel_hourly.png'))
    _plot_per_fuel(
        hourly_results,
        os.path.join(output_dir, 'fuel_hourly_timeseries'),
        f'({month_label})')

    # WECC only
    if not marginal_df.empty and 'region' in marginal_df.columns:
        mw = marginal_df[marginal_df['region'].str.startswith('WEC')].copy()
        hr_wecc = (mw.groupby(['hour', 'fuel'])
                   .agg(marginal_generation_kwh=('marginal_generation_kwh', 'sum'),
                        co2_emissions_kg=('co2_emissions_kg', 'sum'))
                   .reset_index())
        _plot_combined(
            hr_wecc,
            f'Consequential Generation by Fuel — WECC Only ({month_label})',
            os.path.join(output_dir, 'consequential_gen_by_fuel_hourly_wecc.png'))
        _plot_per_fuel(
            hr_wecc,
            os.path.join(output_dir, 'fuel_hourly_timeseries_wecc'),
            f'WECC ({month_label})')

    print(f'  Consequential gen by fuel saved to: {output_dir}')


# ===========================================================================
# 6. Period consequential generation bar chart  (notebook-faithful, cell 50)
# ===========================================================================

def plot_period_consequential_generation(period_results, output_dir, month_label=''):
    """Port of notebook cell 50 – horizontal bar chart of period totals."""
    os.makedirs(output_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 8))

    df = period_results.copy()
    df['marginal_generation_gwh'] = df['marginal_generation_kwh'] / 1e6
    df = df[df['marginal_generation_gwh'].abs() > 0.001]

    if df.empty:
        ax.text(0.5, 0.5, 'No significant data', ha='center', va='center',
                fontsize=14, transform=ax.transAxes)
    else:
        df = df.sort_values('marginal_generation_gwh', key=abs, ascending=False)
        colors = ['#2ecc71' if x > 0 else '#e74c3c'
                  for x in df['marginal_generation_gwh']]
        ax.barh(df['fuel'], df['marginal_generation_gwh'],
                color=colors, alpha=0.7, edgecolor='black', linewidth=0.5)
        max_abs = df['marginal_generation_gwh'].abs().max()
        for i, (fuel, val) in enumerate(zip(df['fuel'], df['marginal_generation_gwh'])):
            lx = val + (max_abs * 0.02 if val >= 0 else -max_abs * 0.02)
            ax.text(lx, i, f'{val:.2f}', va='center', fontsize=9)
        ax.axvline(0, color='black', lw=1.5)
        ax.set_xlabel('Consequential Generation (GWh)'); ax.set_ylabel('Fuel Type')
        ax.set_title(f'Total Consequential Generation by Fuel — WECC ({month_label})',
                     fontsize=14, fontweight='bold')
        ax.legend(handles=[
            Patch(facecolor='#2ecc71', alpha=0.7, label='Increased'),
            Patch(facecolor='#e74c3c', alpha=0.7, label='Decreased'),
        ], loc='lower right', fontsize=10)
        ax.grid(True, alpha=0.3, axis='x')

    plt.tight_layout()
    fpath = os.path.join(output_dir, 'period_consequential_gen.png')
    plt.savefig(fpath, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  Saved: {fpath}')


# ===========================================================================
# 7. Hourly CO2 intensity WECC  (notebook-faithful, cell 52)
# ===========================================================================

def plot_hourly_co2_intensity_wecc(marginal_df, num_hours, output_dir, month_label=''):
    """Port of notebook cell 52 – gCO2/kWh intensity for WECC regions."""
    os.makedirs(output_dir, exist_ok=True)
    hrs = np.arange(num_hours)

    if marginal_df.empty or 'region' not in marginal_df.columns:
        return

    mw = marginal_df[marginal_df['region'].str.startswith('WEC')].copy()
    hourly = (mw.groupby('hour')
              .agg(marginal_generation_kwh=('marginal_generation_kwh', 'sum'),
                   co2_emissions_kg=('co2_emissions_kg', 'sum'))
              .reset_index()
              .set_index('hour')
              .reindex(range(num_hours), fill_value=0)
              .reset_index())

    gen  = hourly['marginal_generation_kwh'].values
    co2  = hourly['co2_emissions_kg'].values
    gen_nz = np.where(gen == 0, np.nan, gen)
    intensity = (co2 * 1000) / gen_nz   # g CO2 / kWh

    avg = np.nanmean(intensity)
    print(f'  Avg consequential CO2 intensity (WECC): {avg:.2f} gCO2/kWh')

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(hrs, intensity, lw=1.5, marker='o', markersize=3)
    ax.set_xlim(0, num_hours - 1); ax.set_xlabel('Hour')
    ax.set_ylabel('Marginal CO₂ Intensity (gCO₂/kWh)')
    ax.set_title(
        f'Hourly Consequential CO₂ Intensity — EV Charging (WECC) ({month_label})\n'
        f'Average: {avg:.2f} gCO₂/kWh  '
        '(NaN gaps = hours with zero consequential EV generation)',
        fontweight='bold')
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    fpath = os.path.join(output_dir, 'hourly_co2_intensity_wecc.png')
    plt.savefig(fpath, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  Saved: {fpath}')


# ===========================================================================
# 8. Fuel of last (marginal) plant dispatched  (notebook-faithful, cell 53)
# ===========================================================================

def plot_marginal_unit_fuel_grid(baseline_solution, baseline_graph,
                                 ev_solution, ev_graph,
                                 num_hours, output_dir, month_label=''):
    """
    Port of notebook cell 53.
    24×N_days grid showing the fuel of the most-expensive dispatched producer.
    """
    os.makedirs(output_dir, exist_ok=True)

    def _marginal_fuel_per_hour(solution, graph, num_hours):
        mf = [''] * num_hours
        for h in range(num_hours):
            best_cost = -1.0; best_fuel = ''
            for region, sol_node in solution._node.items():
                gr_node = graph._node.get(region, {}) if graph else {}
                for handle, asset in sol_node.get('assets', {}).items():
                    gr_asset = gr_node.get('assets', {}).get(handle, {})
                    fuel = (gr_asset.get('fuel') or '').lower()
                    if fuel in _STORAGE_FUELS:
                        continue
                    if gr_asset.get('_class') != 'Producer':
                        continue
                    net = asset.get('net', [])
                    if not net or len(net) <= h:
                        continue
                    prod = float(net[h]) if net[h] is not None else 0
                    if prod <= 0:
                        continue
                    cost = float(gr_asset.get('operating_cost', 0) or 0)
                    if cost > best_cost:
                        best_cost = cost; best_fuel = fuel or 'unknown'
            mf[h] = best_fuel if best_fuel else 'none'
        return mf

    n_days = num_hours // 24
    mf_b = _marginal_fuel_per_hour(baseline_solution, baseline_graph, num_hours)
    mf_e = _marginal_fuel_per_hour(ev_solution,       ev_graph,       num_hours)

    def to_grid(mf):
        g = [['none'] * 24 for _ in range(n_days)]
        for h, f in enumerate(mf):
            if h >= n_days * 24:
                break
            g[h // 24][h % 24] = (f or 'none').strip() or 'none'
        return g

    grid_b = to_grid(mf_b); grid_e = to_grid(mf_e)
    all_fuels = sorted(set(f for row in grid_b + grid_e for f in row))
    if 'none' not in all_fuels:
        all_fuels = ['none'] + [x for x in all_fuels if x != 'none']
    f2i = {f: i for i, f in enumerate(all_fuels)}
    cmap_list = [FUEL_COLORS.get(f, '#888888') for f in all_fuels]

    def g2mat(grid):
        return np.array([[f2i.get(f, 0) for f in row] for row in grid])

    mat_b = g2mat(grid_b); mat_e = g2mat(grid_e)
    n_fuels = max(len(f2i), 1)
    cmap = ListedColormap(cmap_list)

    fig, axes = plt.subplots(2, 1, figsize=(16, max(6, n_days * 0.8)))
    for ax, mat, title in [
        (axes[0], mat_b, f'Baseline — Fuel of Last Plant ({month_label})'),
        (axes[1], mat_e, f'EV Scenario — Fuel of Last Plant ({month_label})'),
    ]:
        im = ax.imshow(mat, cmap=cmap, aspect='auto', vmin=0, vmax=n_fuels - 0.01)
        ax.set_yticks(range(n_days))
        ax.set_yticklabels([f'Day {d+1}' for d in range(n_days)])
        ax.set_xticks(range(24)); ax.set_xticklabels(range(24))
        ax.set_xlabel('Hour of Day'); ax.set_ylabel('Day')
        ax.set_title(title, fontsize=12, fontweight='bold')

    cbar_ax = fig.add_axes([0.92, 0.15, 0.02, 0.7])
    cbar = fig.colorbar(im, cax=cbar_ax, ticks=range(n_fuels))
    cbar.ax.set_yticklabels(all_fuels, fontsize=8)
    plt.tight_layout(rect=[0, 0, 0.9, 1])
    fpath = os.path.join(output_dir, 'marginal_unit_fuel_grid.png')
    plt.savefig(fpath, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  Saved: {fpath}')


# ===========================================================================
# 9. State-level RPS — jurisdiction-based  (notebook-faithful, cell 59)
# ===========================================================================

def _extract_rps_targets(policies_obj):
    out = {}
    if not isinstance(policies_obj, dict):
        return out
    for key, p in policies_obj.items():
        if not str(key).startswith('rps_'):
            continue
        st = str(key).split('rps_', 1)[1]
        ratio = p.get('ratio') if isinstance(p, dict) else None
        if ratio is not None:
            out[st] = float(ratio)
    return out


def _source_bucket(meta):
    fuel = str(meta.get('fuel', '') or '').lower()
    typ  = str(meta.get('type', '') or '').lower()
    hid  = str(meta.get('id',   '') or '').lower()
    token = ' '.join([fuel, typ, hid])
    if any(k in token for k in ('hydro', 'water', 'hydroelectric', 'runofriver')):
        return 'hydro'
    if 'solar' in token:   return 'solar'
    if 'wind'  in token:   return 'wind'
    if any(k in token for k in ('biomass', 'bio', 'wood', 'landfill', 'msw')):
        return 'biomass'
    if 'geo'   in token:   return 'geothermal'
    if 'nuclear' in token: return 'nuclear'
    if 'coal'  in token:   return 'coal'
    if 'gas'   in token or 'ng' in token: return 'gas'
    if any(k in token for k in ('oil', 'diesel', 'petro')): return 'oil'
    return 'other'


def _safe_capex_frac(asset_sol, gr_asset):
    capex = max(_scalar(asset_sol.get('capex', 0.0)), 0.0)
    exist = max(_scalar(gr_asset.get('capacity', 0.0)), 0.0)
    if capex <= 0:  return 0.0
    if exist <= 1e-9: return 1.0
    return float(np.clip(capex / (exist + capex), 0.0, 1.0))


_RPS_COMP_ORDER = [
    'solar_existing','solar_capex','wind_existing','wind_capex',
    'hydro_renewable','geothermal','biomass','other_renewable',
    'hydro_nonrenewable','nuclear','gas','coal','oil','other_nonrenewable',
]
_RPS_COMP_COLORS = {
    'solar_existing':      '#F6C85F', 'solar_capex':          '#F28E2B',
    'wind_existing':       '#86BCB6', 'wind_capex':           '#00A6D6',
    'hydro_renewable':     '#1f78b4', 'geothermal':           '#8c564b',
    'biomass':             '#59A14F', 'other_renewable':       '#B5CF6B',
    'hydro_nonrenewable':  '#4F81BD', 'nuclear':              '#B07AA1',
    'gas':                 '#9c755f', 'coal':                 '#4D4D4D',
    'oil':                 '#7f7f7f', 'other_nonrenewable':   '#A0A0A0',
}
_RPS_COMP_LABELS = {
    'solar_existing': 'Solar (existing)',   'solar_capex': 'Solar (CAPEX)',
    'wind_existing':  'Wind (existing)',    'wind_capex':  'Wind (CAPEX)',
    'hydro_renewable': 'Hydro (renewable)', 'geothermal': 'Geothermal',
    'biomass': 'Biomass',                   'other_renewable': 'Other renewables',
    'hydro_nonrenewable': 'Hydro (non-ren)','nuclear': 'Nuclear',
    'gas': 'Gas',                           'coal': 'Coal',
    'oil': 'Oil',                           'other_nonrenewable': 'Other non-ren',
}


def _state_gen_by_jurisdiction(solution, graph):
    """
    Compute per-state generation components using asset jurisdiction field.
    Returns DataFrame indexed by state with component % columns.
    """
    per_state = {}
    for region, sol_node in solution._node.items():
        gr_node = graph._node.get(region, {}) if graph else {}
        for handle, asset_sol in sol_node.get('assets', {}).items():
            net = asset_sol.get('net', [])
            if not net:
                continue
            gr_asset = gr_node.get('assets', {}).get(handle, {}) if gr_node else {}
            # Build merged metadata
            meta = {
                'fuel':         gr_asset.get('fuel') or asset_sol.get('fuel'),
                'type':         gr_asset.get('type') or asset_sol.get('type'),
                '_class':       gr_asset.get('_class') or asset_sol.get('_class'),
                'renewable':    gr_asset.get('renewable') or asset_sol.get('renewable'),
                'jurisdiction': gr_asset.get('jurisdiction') or asset_sol.get('jurisdiction'),
                'operating_cost': gr_asset.get('operating_cost', 0),
                'id': handle,
            }
            state = str(meta.get('jurisdiction') or '').upper()
            if len(state) != 2 or not state.isalpha():
                continue
            cls = str(meta.get('_class') or '')
            if cls == 'Store':
                continue
            if str(meta.get('type') or '').lower() == 'load':
                continue

            gen_w = np.maximum(np.array(net, dtype=float), 0.0)
            e_ws  = float(gen_w.sum() * 3600.0)
            if e_ws <= 0:
                continue

            per_state.setdefault(state, {k: 0.0 for k in _RPS_COMP_ORDER})
            per_state[state].setdefault('_total_ws', 0.0)
            per_state[state].setdefault('_ren_ws', 0.0)
            per_state[state].setdefault('_nonren_ws', 0.0)

            sb   = _source_bucket(meta)
            is_r = bool(meta.get('renewable', False))

            comp = per_state[state]
            if sb == 'solar':
                frac = _safe_capex_frac(asset_sol, gr_asset)
                comp['solar_capex']    += e_ws * frac
                comp['solar_existing'] += e_ws * (1 - frac)
            elif sb == 'wind':
                frac = _safe_capex_frac(asset_sol, gr_asset)
                comp['wind_capex']    += e_ws * frac
                comp['wind_existing'] += e_ws * (1 - frac)
            elif sb == 'hydro':
                comp['hydro_renewable' if is_r else 'hydro_nonrenewable'] += e_ws
            elif sb in ('geothermal', 'biomass', 'nuclear', 'gas', 'coal', 'oil'):
                comp[sb] += e_ws
            else:
                comp['other_renewable' if is_r else 'other_nonrenewable'] += e_ws

            per_state[state]['_total_ws'] += e_ws
            if is_r:
                per_state[state]['_ren_ws'] += e_ws
            else:
                per_state[state]['_nonren_ws'] += e_ws

    rows = []
    for state in sorted(per_state):
        total = per_state[state]['_total_ws']
        if total <= 0:
            continue
        row = {'state': state,
               'total_gen_ws': total,
               'renewable_pct': 100 * per_state[state]['_ren_ws'] / total}
        for k in _RPS_COMP_ORDER:
            row[f'{k}_pct'] = 100 * per_state[state].get(k, 0) / total
        rows.append(row)
    return pd.DataFrame(rows).sort_values('state') if rows else pd.DataFrame()


def _plot_rps_stack_ax(ax, df, rps_targets, title):
    if df.empty:
        ax.text(0.5, 0.5, 'No state data', ha='center', va='center',
                transform=ax.transAxes)
        ax.set_title(title)
        return
    x = np.arange(len(df))
    bottom = np.zeros(len(df))
    handles_labels = []
    for k in _RPS_COMP_ORDER:
        col = f'{k}_pct'
        vals = df[col].to_numpy(dtype=float) if col in df.columns else np.zeros(len(df))
        if np.allclose(vals, 0):
            continue
        color = _RPS_COMP_COLORS.get(k, '#CCC')
        label = _RPS_COMP_LABELS.get(k, k)
        bar = ax.bar(x, vals, bottom=bottom, width=0.75, color=color, label=label)
        handles_labels.append((bar, label))
        bottom += vals
    for i, st in enumerate(df['state']):
        if st in rps_targets:
            y = 100.0 * rps_targets[st]
            ax.plot([i - 0.375, i + 0.375], [y, y],
                    color='black', lw=3, solid_capstyle='butt', zorder=5)
    ax.set_xticks(x)
    ax.set_xticklabels(df['state'], rotation=45, ha='right')
    ax.set_ylim(0, 100)
    ax.set_ylabel('Share of In-State Generation (%)')
    ax.set_title(title, fontsize=12, fontweight='bold')
    ax.grid(True, axis='y', alpha=0.3)


def plot_rps_jurisdiction(baseline_solution, baseline_graph,
                          ev_solution, ev_graph,
                          policies, output_dir, month_label=''):
    """
    Port of notebook cell 59 – jurisdiction-based RPS stacked bar.
    Saves state_rps_fulfillment.png and rps_compliance_table.csv.
    """
    os.makedirs(output_dir, exist_ok=True)
    rps_targets = _extract_rps_targets(policies)

    base_df = _state_gen_by_jurisdiction(baseline_solution, baseline_graph)
    ev_df   = _state_gen_by_jurisdiction(ev_solution,       ev_graph)

    if base_df.empty and ev_df.empty:
        print('  RPS: no jurisdiction-scoped assets found.')
        return

    # Side-by-side or single panel
    if not base_df.empty and not ev_df.empty:
        fig, axes = plt.subplots(1, 2, figsize=(24, 8), sharey=True)
        _plot_rps_stack_ax(axes[0], base_df, rps_targets,
                           f'Baseline: Model-Side RPS vs Target ({month_label})')
        _plot_rps_stack_ax(axes[1], ev_df,   rps_targets,
                           f'EV Scenario: Model-Side RPS vs Target ({month_label})')
        h, l = axes[1].get_legend_handles_labels()
        rps_line = plt.Line2D([0], [0], color='black', lw=3)
        axes[1].legend(h + [rps_line], l + ['RPS target'],
                       loc='upper left', fontsize=8, ncol=2)
    else:
        use_df = ev_df if not ev_df.empty else base_df
        lbl    = 'EV Scenario' if not ev_df.empty else 'Baseline'
        fig, ax = plt.subplots(1, 1, figsize=(14, 8))
        _plot_rps_stack_ax(ax, use_df, rps_targets,
                           f'{lbl}: Model-Side RPS vs Target ({month_label})')
        h, l = ax.get_legend_handles_labels()
        ax.legend(h + [plt.Line2D([0],[0],color='black',lw=3)],
                  l + ['RPS target'], loc='upper left', fontsize=8, ncol=2)

    plt.tight_layout()
    fpath = os.path.join(output_dir, 'state_rps_fulfillment.png')
    plt.savefig(fpath, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'  Saved: {fpath}')

    # Compliance table
    rows = []
    for df, scen in [(base_df, 'baseline'), (ev_df, 'ev')]:
        if df.empty:
            continue
        for _, row in df.iterrows():
            st = row['state']
            tgt = rps_targets.get(st)
            rows.append({
                'state': st, 'scenario': scen,
                'renewable_pct': round(row['renewable_pct'], 2),
                'target_pct': round(tgt * 100, 2) if tgt else None,
                'gap_pct': round(row['renewable_pct'] - tgt * 100, 4) if tgt else None,
                'compliant': (row['renewable_pct'] >= tgt * 100) if tgt else None,
            })
    if rows:
        rps_csv = pd.DataFrame(rows)
        rps_csv.to_csv(os.path.join(output_dir, 'rps_compliance_table.csv'), index=False)
        print(rps_csv.to_string(index=False))
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ===========================================================================
# 10. WECC stacked generation
# ===========================================================================

def plot_wecc_stacked_generation(baseline_gen, ev_gen, num_hours, output_dir, month_label=''):
    os.makedirs(output_dir, exist_ok=True)
    hrs = np.arange(num_hours)

    def _pivot(gdf):
        return (gdf.groupby(['hour','fuel'])['generation_kwh'].sum().reset_index()
                .pivot(index='hour', columns='fuel', values='generation_kwh')
                .reindex(hrs, fill_value=0).fillna(0) / 1e6)

    for gdf, label, suffix in [
        (baseline_gen, 'Baseline', 'baseline'),
        (ev_gen,       'EV',       'ev'),
    ]:
        piv = _pivot(gdf)
        fo  = _fuel_stack_order(set(piv.columns))
        fo  = [f for f in fo if piv[f].abs().max() > 0]
        fig, ax = plt.subplots(figsize=(18, 5))
        if fo:
            ax.stackplot(hrs, *[piv[f].values for f in fo],
                         labels=fo,
                         colors=[FUEL_COLORS.get(f,'#CCC') for f in fo], alpha=0.85)
        ax.set_title(f'WECC Stacked Generation — {label} ({month_label})', fontweight='bold')
        ax.set_xlabel('Hour'); ax.set_ylabel('GW')
        ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1), fontsize=7)
        ax.grid(True, alpha=0.3, axis='y'); ax.set_xlim(0, num_hours - 1)
        plt.tight_layout()
        fp = os.path.join(output_dir, f'wecc_stacked_gen_{suffix}.png')
        plt.savefig(fp, dpi=140, bbox_inches='tight'); plt.close(fig)
        print(f'  Saved: {fp}')


# ===========================================================================
# 11. Transmission table + top-20 chart
# ===========================================================================

def save_transmission_table(baseline_solution, ev_solution, num_hours, output_dir,
                            month_label=''):
    os.makedirs(output_dir, exist_ok=True)

    def _flows(sol):
        rows = []
        for src, adj in sol._adj.items():
            for tgt, edge in adj.items():
                for lname, line in edge.get('lines', {}).items():
                    tr = line.get('transmission', [])
                    if not tr or len(tr) != num_hours:
                        continue
                    arr = np.array(tr, dtype=float)
                    cap = line.get('installed_capacity', np.nan)
                    rows.append({
                        'source': src, 'target': tgt, 'line': lname,
                        'mean_GW': arr.mean() / 1e9, 'max_GW': arr.max() / 1e9,
                        'utilisation': arr.mean() / cap if cap and cap > 0 else np.nan,
                    })
        return pd.DataFrame(rows)

    for sol, lbl in [(baseline_solution, 'baseline'), (ev_solution, 'ev')]:
        df = _flows(sol)
        fp = os.path.join(output_dir, f'transmission_flows_{lbl}.csv')
        df.to_csv(fp, index=False); print(f'  Saved: {fp}')

    base_flows = _flows(baseline_solution)
    if not base_flows.empty:
        top = base_flows.nlargest(min(20, len(base_flows)), 'mean_GW').copy()
        top['corridor'] = top['source'] + '→' + top['target']
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.barh(top['corridor'], top['mean_GW'], color='steelblue', alpha=0.8, label='Mean flow')
        ax.barh(top['corridor'], top['max_GW'],  color='orange', alpha=0.5, label='Max flow')
        ax.set_xlabel('GW'); ax.invert_yaxis()
        ax.set_title(f'Top-20 Transmission Corridors — Baseline ({month_label})', fontweight='bold')
        ax.legend(); plt.tight_layout()
        fp = os.path.join(output_dir, 'transmission_top20_baseline.png')
        plt.savefig(fp, dpi=130, bbox_inches='tight'); plt.close(fig)
        print(f'  Saved: {fp}')


# ===========================================================================
# 12. CAPEX expansion
# ===========================================================================

def compute_capex_table(baseline_solution, baseline_graph, ev_solution, ev_graph):
    rows_b, rows_e = [], []
    for sol, gr, rows in [
        (baseline_solution, baseline_graph, rows_b),
        (ev_solution,       ev_graph,       rows_e),
    ]:
        for region, node in sol._node.items():
            gr_node = gr._node.get(region, {}) if gr else {}
            for handle, asset in node.get('assets', {}).items():
                if not handle.startswith('optional_'):
                    continue
                capex_w = _scalar(asset.get('capex', 0))
                if capex_w < 1.0:
                    continue
                gr_asset = gr_node.get('assets', {}).get(handle, {})
                fuel = str(gr_asset.get('fuel') or gr_asset.get('type') or 'unknown').lower()
                rows.append({'region': region, 'asset': handle, 'fuel': fuel,
                             'capex_MW': capex_w / 1e6})
    return pd.DataFrame(rows_b), pd.DataFrame(rows_e)


def plot_capex_expansion(df_b, df_e, output_dir, month_label=''):
    os.makedirs(output_dir, exist_ok=True)
    for df, label, suffix in [(df_b,'Baseline','baseline'),(df_e,'EV','ev')]:
        if df.empty:
            continue
        summary = df.groupby('fuel')['capex_MW'].sum().sort_values(ascending=True)
        fig, ax = plt.subplots(figsize=(8, max(4, len(summary) * 0.5)))
        summary.plot.barh(ax=ax,
                         color=[FUEL_COLORS.get(f,'#CCC') for f in summary.index], alpha=0.85)
        for bar in ax.patches:
            ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                    f'{bar.get_width():.0f} MW', va='center', fontsize=8)
        ax.set_title(f'CAPEX Expansion — {label} ({month_label})', fontweight='bold')
        ax.set_xlabel('MW added'); plt.tight_layout()
        fp = os.path.join(output_dir, f'capex_expansion_{suffix}.png')
        plt.savefig(fp, dpi=130, bbox_inches='tight'); plt.close(fig)
        print(f'  Saved: {fp}')

    if not df_b.empty or not df_e.empty:
        sb = df_b.groupby('fuel')['capex_MW'].sum() if not df_b.empty else pd.Series()
        se = df_e.groupby('fuel')['capex_MW'].sum() if not df_e.empty else pd.Series()
        idx = sorted(set(sb.index) | set(se.index))
        sb  = sb.reindex(idx, fill_value=0); se = se.reindex(idx, fill_value=0)
        x   = np.arange(len(idx))
        fig, ax = plt.subplots(figsize=(max(8, len(idx) * 1.2), 5))
        ax.bar(x - 0.2, sb.values, 0.38, label='Baseline',
               color=[FUEL_COLORS.get(f,'#CCC') for f in idx], alpha=0.8)
        ax.bar(x + 0.2, se.values, 0.38, label='EV',
               color=[FUEL_COLORS.get(f,'#CCC') for f in idx], alpha=0.5)
        ax.set_xticks(x); ax.set_xticklabels(idx, rotation=30, ha='right')
        ax.set_ylabel('MW added')
        ax.set_title(f'CAPEX Expansion: Baseline vs EV ({month_label})', fontweight='bold')
        ax.legend(); plt.tight_layout()
        fp = os.path.join(output_dir, 'capex_expansion_comparison.png')
        plt.savefig(fp, dpi=130, bbox_inches='tight'); plt.close(fig)
        print(f'  Saved: {fp}')


# ===========================================================================
# 13. Marginal emissions (notebook-compatible column names)
# ===========================================================================

def compute_marginal_emissions(baseline_gen, ev_gen):
    """
    Return (marginal_df, hourly_results, period_results).
    Column names match notebook: marginal_generation_kwh, co2_emissions_kg, etc.
    """
    def _agg(df, sfx):
        return (df.groupby(['hour','region','fuel'])
                .agg(**{
                    f'gen_kwh_{sfx}': ('generation_kwh', 'sum'),
                    f'co2_factor_{sfx}': ('co2_factor', 'first'),
                    f'nox_factor_{sfx}': ('nox_factor', 'first'),
                    f'so2_factor_{sfx}': ('so2_factor', 'first'),
                }).reset_index())

    ba = _agg(baseline_gen, 'base')
    ea = _agg(ev_gen,       'ev')
    m  = ea.merge(ba, on=['hour','region','fuel'], how='outer').fillna(0)

    m['marginal_generation_kwh'] = m['gen_kwh_ev'] - m['gen_kwh_base']
    for pol in ('co2','nox','so2'):
        col = f'{pol}_factor'
        m[col] = m[f'{pol}_factor_ev'].where(m[f'{pol}_factor_ev'] != 0,
                                             m[f'{pol}_factor_base'])
        m[f'{pol}_emissions_kg'] = (m['marginal_generation_kwh'] * 3.6e6 * m[col])

    m = m[~m['fuel'].isin(_STORAGE_FUELS)].copy()

    agg_cols = ['marginal_generation_kwh','co2_emissions_kg',
                'nox_emissions_kg','so2_emissions_kg']
    hourly = m.groupby(['hour','fuel'])[agg_cols].sum().reset_index()
    period = m.groupby('fuel')[agg_cols].sum().reset_index()
    return m, hourly, period


# ===========================================================================
# 14. Marginal CO2 stacked + consequential emission factor
# ===========================================================================

def plot_marginal_co2_by_fuel(hourly, num_hours, output_dir, month_label=''):
    os.makedirs(output_dir, exist_ok=True)
    pivot = (hourly.pivot(index='hour', columns='fuel', values='co2_emissions_kg')
             .fillna(0).reindex(range(num_hours), fill_value=0))
    hrs = np.arange(num_hours)
    positives = [f for f in pivot.columns if pivot[f].sum() >= 0 and pivot[f].abs().max() > 0]
    negatives = [f for f in pivot.columns if pivot[f].sum() < 0]
    fig, ax = plt.subplots(figsize=(18, 5))
    if positives:
        ax.stackplot(hrs, *[pivot[f].values for f in positives], labels=positives,
                     colors=[FUEL_COLORS.get(f,'#CCC') for f in positives], alpha=0.85)
    for f in negatives:
        ax.fill_between(hrs, 0, pivot[f].values, label=f,
                        color=FUEL_COLORS.get(f,'#CCC'), alpha=0.6)
    ax.axhline(0, color='black', lw=0.8, ls='--')
    ax.set_title(f'Hourly Marginal CO₂ (EV − Baseline)  {month_label}', fontweight='bold')
    ax.set_xlabel('Hour'); ax.set_ylabel('CO₂ (kg/h)')
    ax.legend(loc='upper left', bbox_to_anchor=(1.01,1), fontsize=7)
    ax.grid(True, alpha=0.3, axis='y'); ax.set_xlim(0, num_hours - 1)
    plt.tight_layout()
    fp = os.path.join(output_dir, 'marginal_co2_by_fuel.png')
    plt.savefig(fp, dpi=140, bbox_inches='tight'); plt.close(fig)
    print(f'  Saved: {fp}')


def plot_consequential_emission_factor(hourly, ev_charging_load_slice,
                                       num_hours, output_dir, month_label=''):
    os.makedirs(output_dir, exist_ok=True)
    total_co2 = (hourly.groupby('hour')['co2_emissions_kg']
                 .sum().reindex(range(num_hours), fill_value=0).values)
    ev_kwh = ev_charging_load_slice / 1e3 if ev_charging_load_slice is not None else np.zeros(num_hours)
    with np.errstate(divide='ignore', invalid='ignore'):
        factor = np.where(ev_kwh > 1e-6, total_co2 / ev_kwh, 0.0)
    hrs = np.arange(num_hours)
    fig, ax = plt.subplots(figsize=(16, 4))
    ax.plot(hrs, factor, color='crimson', lw=1.5)
    ax.fill_between(hrs, 0, factor, alpha=0.2, color='crimson')
    ax.axhline(0, color='black', lw=0.8, ls='--')
    ax.set_title(f'Consequential Emission Factor (kg CO₂/kWh EV)  {month_label}',
                 fontweight='bold')
    ax.set_xlabel('Hour'); ax.set_ylabel('kg CO₂/kWh')
    ax.grid(True, alpha=0.3); ax.set_xlim(0, num_hours - 1)
    plt.tight_layout()
    fp = os.path.join(output_dir, 'consequential_emission_factor.png')
    plt.savefig(fp, dpi=140, bbox_inches='tight'); plt.close(fig)
    print(f'  Saved: {fp}')


# ===========================================================================
# 15. WECC-wide CO2 stacked + intensity
# ===========================================================================

def _wecc_hourly_co2(gen_df, num_hours):
    if gen_df is None or gen_df.empty:
        return pd.DataFrame(index=range(num_hours)), np.zeros(num_hours), np.zeros(num_hours)
    g = gen_df.copy()
    g['co2_kg'] = g['generation_kwh'] * 3.6e6 * g['co2_factor'].fillna(0)
    pivot = (g.groupby(['hour','fuel'])['co2_kg'].sum().reset_index()
             .pivot(index='hour', columns='fuel', values='co2_kg')
             .fillna(0).reindex(range(num_hours), fill_value=0)) / 1000
    total_t = pivot.sum(axis=1).values
    gen_mwh = (g.groupby('hour')['generation_kwh'].sum()
               .reindex(range(num_hours), fill_value=0).values / 1e3)
    with np.errstate(divide='ignore', invalid='ignore'):
        intensity = np.where(gen_mwh > 1e-9, total_t * 1e3 / (gen_mwh * 1e3), 0.0)
    return pivot, total_t, intensity


def plot_wecc_emissions(baseline_gen, ev_gen, num_hours, output_dir, month_label=''):
    os.makedirs(output_dir, exist_ok=True)
    hrs = np.arange(num_hours)
    bp, bt, bi = _wecc_hourly_co2(baseline_gen, num_hours)
    ep, et, ei = _wecc_hourly_co2(ev_gen,       num_hours)
    all_fuels = sorted(set(bp.columns) | set(ep.columns))
    fo = [f for f in FUEL_ORDER if f in all_fuels and
          (bp.get(f, pd.Series()).max() > 0 or ep.get(f, pd.Series()).max() > 0)]

    fig, axes = plt.subplots(1, 2, figsize=(20, 5), sharey=True)
    fig.suptitle(f'WECC Hourly CO₂ Emissions by Fuel ({month_label})', fontweight='bold')
    for ax, piv, lbl in [(axes[0],bp,'Baseline'),(axes[1],ep,'EV')]:
        ys = [piv.get(f, pd.Series([0]*num_hours)).values for f in fo]
        if ys:
            ax.stackplot(hrs, *ys, labels=fo,
                         colors=[FUEL_COLORS.get(f,'#CCC') for f in fo], alpha=0.85)
        ax.set_title(lbl); ax.set_xlabel('Hour'); ax.set_ylabel('tCO₂/h')
        ax.grid(True, alpha=0.3, axis='y'); ax.set_xlim(0, num_hours - 1)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc='upper right', bbox_to_anchor=(1.0, 0.95), fontsize=7)
    plt.tight_layout(rect=[0, 0, 0.88, 1])
    fp = os.path.join(output_dir, 'wecc_co2_stacked_by_fuel.png')
    plt.savefig(fp, dpi=140, bbox_inches='tight'); plt.close(fig)
    print(f'  Saved: {fp}')

    fig, ax = plt.subplots(figsize=(16, 4))
    ax.plot(hrs, bi, label='Baseline', color='steelblue', lw=1.5)
    ax.plot(hrs, ei, label='EV',       color='crimson',   lw=1.5)
    ax.set_title(f'WECC Average CO₂ Intensity ({month_label})', fontweight='bold')
    ax.set_xlabel('Hour'); ax.set_ylabel('kg CO₂/kWh')
    ax.legend(); ax.grid(True, alpha=0.3); ax.set_xlim(0, num_hours - 1)
    plt.tight_layout()
    fp = os.path.join(output_dir, 'wecc_co2_intensity.png')
    plt.savefig(fp, dpi=140, bbox_inches='tight'); plt.close(fig)
    print(f'  Saved: {fp}')


# ===========================================================================
# 16. Per-region stacked generation
# ===========================================================================

def plot_regions(baseline_solution, baseline_graph,
                 ev_solution, ev_graph,
                 baseline_gen, ev_gen,
                 num_hours, output_dir, month_label=''):
    os.makedirs(output_dir, exist_ok=True)
    hrs = np.arange(num_hours)
    all_regions = sorted(set(baseline_solution._node.keys()) |
                         set(ev_solution._node.keys()))
    for region in all_regions:
        db = _collect_region_balance(baseline_solution, baseline_graph, region, num_hours)
        de = _collect_region_balance(ev_solution,       ev_graph,       region, num_hours)
        fig, axes = plt.subplots(1, 2, figsize=(20, 6), sharey=True)
        fig.suptitle(f'{region}  —  {month_label}', fontweight='bold')
        for ax, d, gdf, lbl in [
            (axes[0], db, baseline_gen, 'Baseline'),
            (axes[1], de, ev_gen,       'EV'),
        ]:
            rg = gdf[gdf['region'] == region].copy() if not gdf.empty else pd.DataFrame()
            if not rg.empty:
                pivot = (rg.groupby(['hour','fuel'])['generation_kwh'].sum().reset_index()
                         .pivot(index='hour', columns='fuel', values='generation_kwh')
                         .reindex(hrs, fill_value=0).fillna(0))
                fo = [f for f in FUEL_ORDER if f in pivot.columns
                      and pivot[f].abs().max() > 0]
                for f in pivot.columns:
                    if f not in fo and pivot[f].abs().max() > 0:
                        fo.append(f)
                ys = [pivot[f].values / 1e6 for f in fo]
                cs = [FUEL_COLORS.get(f,'#CCC') for f in fo]
                if ys:
                    ax.stackplot(hrs, *ys, labels=fo, colors=cs, alpha=0.85)
                    bottom = np.sum(np.array(ys), axis=0)
                else:
                    bottom = np.zeros(num_hours)
            else:
                bottom = np.zeros(num_hours)
            ax.fill_between(hrs, bottom, bottom + d['discharge_GW'],
                            color=FUEL_COLORS['battery'], alpha=0.6, label='Discharge')
            b2 = bottom + d['discharge_GW']
            ax.fill_between(hrs, b2, b2 + d['import_GW'],
                            color=FUEL_COLORS['import'], alpha=0.5, label='Gross import')
            ax.fill_between(hrs, 0, -d['charge_GW'],
                            color='#F39C12', alpha=0.6, label='Storage charge')
            ax.fill_between(hrs, -d['charge_GW'], -d['charge_GW'] - d['export_GW'],
                            color='#E74C3C', alpha=0.5, label='Gross export')
            ax.plot(hrs, d['load_GW'], 'k-', lw=1.5, label='Load')
            sf = d['shortfall_GW']
            if sf.max() > 1e-4:
                ax.fill_between(hrs, 0, sf, where=sf > 1e-4,
                                color='red', alpha=0.4, label='Shortfall')
            ax.set_title(lbl); ax.set_xlabel('Hour'); ax.set_ylabel('GW')
            ax.grid(True, alpha=0.3, axis='y'); ax.set_xlim(0, num_hours - 1)
        h, l = axes[0].get_legend_handles_labels()
        fig.legend(h, l, loc='upper right', bbox_to_anchor=(1.0, 0.95), fontsize=7)
        plt.tight_layout(rect=[0, 0, 0.88, 1])
        fp = os.path.join(output_dir, f'region_{region}.png')
        plt.savefig(fp, dpi=130, bbox_inches='tight'); plt.close(fig)
    print(f'  Region plots saved to: {output_dir}')


# ===========================================================================
# 17. CSV saving
# ===========================================================================

def save_csvs(hourly, period, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    hourly.to_csv(os.path.join(output_dir, 'hourly_consequential_emissions.csv'), index=False)
    period.to_csv(os.path.join(output_dir, 'period_consequential_emissions.csv'), index=False)
    print(f'  CSVs saved to {output_dir}')


# ===========================================================================
# 18. Summary TXT
# ===========================================================================

def save_summary_txt(iteration_cfg, config_obj,
                     baseline_obj, ev_obj,
                     capex_base_df, capex_ev_df,
                     period_marginal, rps_df,
                     output_dir):
    os.makedirs(output_dir, exist_ok=True)
    lines = []

    def sec(t):
        lines.extend(['', '=' * 70, f'  {t}', '=' * 70])

    sec('ITERATION CONFIGURATION')
    lines += [
        f"  Season          : {iteration_cfg.get('month', '?')}",
        f"  Start hour      : {iteration_cfg.get('start_hour', '?')}",
        f"  Num hours       : {config_obj.NUM_HOURS}",
        f"  CAPEX expansion : {config_obj.ENABLE_CAPEX_EXPANSION}",
        f"  Amortization    : {config_obj.NETWORK_KW['amortization_period'] / 31536000:.0f} yr",
        f"  Solver          : {config_obj.SOLVER_KW['solver']['_name']} "
        f"(solver_io={config_obj.SOLVER_KW['solver'].get('solver_io','lp')})",
    ]

    sec('OBJECTIVE VALUES')
    lines += [
        f"  Baseline : {baseline_obj:.4e}",
        f"  EV       : {ev_obj:.4e}",
        f"  Diff     : {ev_obj - baseline_obj:.4e}",
    ]

    sec('CAPEX EXPANSION')
    for df, lbl in [(capex_base_df, 'Baseline'), (capex_ev_df, 'EV')]:
        if df.empty:
            lines.append(f"  {lbl}: no expansion")
        else:
            summary = df.groupby('fuel')['capex_MW'].sum()
            lines.append(f"  {lbl}: {summary.sum():.1f} MW total")
            for fuel, mw in summary.items():
                lines.append(f"    {fuel:20s}: {mw:10.1f} MW")

    sec('MARGINAL GENERATION & EMISSIONS')
    if not period_marginal.empty:
        lines.append(period_marginal.to_string(index=False))
        tc = period_marginal['co2_emissions_kg'].sum() / 1e6
        tg = period_marginal['marginal_generation_kwh'].sum() / 1e6
        lines += [f"\n  Total gen : {tg:.2f} GWh",
                  f"  Total CO2 : {tc:.4f} million kg"]

    if rps_df is not None and not (hasattr(rps_df, 'empty') and rps_df.empty):
        sec('RPS COMPLIANCE')
        lines.append(pd.DataFrame(rps_df).to_string(index=False)
                     if not isinstance(rps_df, str) else rps_df)

    tp = os.path.join(output_dir, 'iteration_summary.txt')
    with open(tp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'  Summary: {tp}')


# ===========================================================================
# 19. Main entry point (called by run_all.py)
# ===========================================================================

def run_postprocess(baseline_solution, baseline_graph,
                    ev_solution, ev_graph,
                    baseline_obj, ev_obj,
                    ev_charging_load_slice,
                    policies,
                    iteration_cfg,
                    output_dir):
    """
    Run all post-processing for one iteration.
    Called by run_all.py with in-memory solution graphs.
    """
    print('\n' + '=' * 72)
    print(f'  POST-PROCESSING  {iteration_cfg.get("month","")}')
    print('=' * 72)

    num_hours   = config.NUM_HOURS
    month_label = iteration_cfg.get('month', '')
    plots_dir   = os.path.join(output_dir, 'plots')
    data_dir    = os.path.join(output_dir, 'data')
    os.makedirs(plots_dir, exist_ok=True)
    os.makedirs(data_dir,  exist_ok=True)

    # Extract generation
    print('Extracting generation by fuel...')
    baseline_gen = extract_generation_by_fuel(baseline_solution, baseline_graph)
    ev_gen       = extract_generation_by_fuel(ev_solution,       ev_graph)
    baseline_gen.to_csv(os.path.join(data_dir, 'baseline_generation.csv'), index=False)
    ev_gen.to_csv(      os.path.join(data_dir, 'ev_generation.csv'),       index=False)

    # Marginal emissions
    print('Computing marginal emissions...')
    marginal_df, hourly, period = compute_marginal_emissions(baseline_gen, ev_gen)
    save_csvs(hourly, period, data_dir)

    # CAPEX table
    print('Computing CAPEX table...')
    capex_b, capex_e = compute_capex_table(
        baseline_solution, baseline_graph, ev_solution, ev_graph)
    capex_b.to_csv(os.path.join(data_dir, 'capex_baseline.csv'), index=False)
    capex_e.to_csv(os.path.join(data_dir, 'capex_ev.csv'),       index=False)

    print('Generating plots...')
    # Summary/overview plots
    plot_wecc_stacked_generation(baseline_gen, ev_gen, num_hours, plots_dir, month_label)
    plot_capex_expansion(capex_b, capex_e, plots_dir, month_label)
    plot_marginal_co2_by_fuel(hourly, num_hours, plots_dir, month_label)
    plot_consequential_emission_factor(hourly, ev_charging_load_slice,
                                       num_hours, plots_dir, month_label)
    plot_wecc_emissions(baseline_gen, ev_gen, num_hours, plots_dir, month_label)
    save_transmission_table(baseline_solution, ev_solution, num_hours, plots_dir, month_label)
    plot_regions(baseline_solution, baseline_graph, ev_solution, ev_graph,
                 baseline_gen, ev_gen, num_hours,
                 os.path.join(plots_dir, 'regions'), month_label)

    # Notebook-faithful plots
    plot_ca_energy_balance_panels(
        baseline_solution, baseline_graph, ev_solution, ev_graph,
        baseline_gen, ev_gen, ev_charging_load_slice,
        num_hours, os.path.join(plots_dir, 'ca_balance'), None, month_label)
    plot_wecc_energy_balance_panels(
        baseline_solution, baseline_graph, ev_solution, ev_graph,
        baseline_gen, ev_gen, ev_charging_load_slice,
        num_hours, os.path.join(plots_dir, 'wecc_balance'), month_label)
    plot_transmission_saturation(ev_solution, num_hours, plots_dir, month_label)
    plot_marginal_generation_by_fuel(
        marginal_df, hourly, num_hours, plots_dir, month_label)
    plot_period_consequential_generation(period, plots_dir, month_label)
    plot_hourly_co2_intensity_wecc(marginal_df, num_hours, plots_dir, month_label)
    plot_marginal_unit_fuel_grid(
        baseline_solution, baseline_graph, ev_solution, ev_graph,
        num_hours, plots_dir, month_label)
    rps_df = plot_rps_jurisdiction(
        baseline_solution, baseline_graph, ev_solution, ev_graph,
        policies, plots_dir, month_label)

    save_summary_txt(
        iteration_cfg, config,
        baseline_obj, ev_obj,
        capex_b, capex_e,
        period, rps_df,
        output_dir,
    )

    print(f'  Post-processing complete → {output_dir}')
    return baseline_gen, ev_gen, marginal_df, hourly, period


# ===========================================================================
# 20. Standalone mode  — run from saved JSON files
# ===========================================================================

def _find_latest_json(folder, prefix):
    """Return path of most-recently-created matching JSON, or None."""
    pattern = os.path.join(folder, f'{prefix}_solution_*.json')
    files   = sorted(glob.glob(pattern))
    return files[-1] if files else None


def _read_obj_txt(folder, prefix):
    pattern = os.path.join(folder, f'{prefix}_objective_*.txt')
    files   = sorted(glob.glob(pattern))
    if not files:
        return 0.0
    with open(files[-1]) as f:
        return float(f.read().strip())


def run_postprocess_from_dir(iter_output_dir):
    """
    Run all post-processing from a saved iteration output directory.

    Expects:
      <iter_output_dir>/
        baseline/  baseline_solution_*.json  baseline_objective_*.txt
        ev/        ev_solution_*.json         ev_objective_*.txt

    Reloads the data graph from config.GRAPH_FILE for metadata.
    """
    from ev_charging_project.utils import (
        load_solution_json, load_ev_profile_for_hours,
        prepare_graph, slice_graph_profiles,
    )

    import good
    from good.reload import deep_reload

    iter_output_dir = os.path.abspath(iter_output_dir)
    season = os.path.basename(iter_output_dir)
    print(f'\nStandalone postprocess for: {iter_output_dir}')

    # ── Load solutions ────────────────────────────────────────────────────────
    b_path = _find_latest_json(os.path.join(iter_output_dir, 'baseline'), 'baseline')
    e_path = _find_latest_json(os.path.join(iter_output_dir, 'ev'),       'ev')
    if not b_path or not e_path:
        raise FileNotFoundError(
            f'Could not find solution JSONs in {iter_output_dir}/baseline/ or /ev/\n'
            f'  baseline: {b_path}\n  ev: {e_path}')

    print(f'  Loading baseline: {b_path}')
    baseline_solution, b_meta = load_solution_json(b_path)
    print(f'  Loading EV:       {e_path}')
    ev_solution,       e_meta = load_solution_json(e_path)

    start_hour = b_meta['start_hour']
    num_hours  = b_meta['num_hours']
    baseline_obj = _read_obj_txt(os.path.join(iter_output_dir, 'baseline'), 'baseline')
    ev_obj       = _read_obj_txt(os.path.join(iter_output_dir, 'ev'),       'ev')

    # ── Load data graph (for metadata: _class, fuel, jurisdiction, etc.) ─────
    print('  Loading data graph...')
    deep_reload(good)
    raw_graph  = good.graph.graph_from_json(config.GRAPH_FILE)
    data_graph = prepare_graph(raw_graph, config)
    # Slice to same window so profile lengths match
    baseline_graph = slice_graph_profiles(data_graph, start_hour, num_hours)
    ev_graph       = slice_graph_profiles(data_graph, start_hour, num_hours)
    # (ev_graph doesn't have the EV load injected, but that only affects Load
    #  asset profiles — metadata like _class/fuel/jurisdiction is identical)

    # ── Load EV profile slice ─────────────────────────────────────────────────
    ev_charging_load = load_ev_profile_for_hours(config, start_hour, num_hours)

    # ── Policies ──────────────────────────────────────────────────────────────
    policies = (good.utilities.read_json(config.POLICIES_FILE)
                if os.path.exists(config.POLICIES_FILE) else {})

    # ── Match to ITERATIONS config or build a synthetic one ───────────────────
    iteration_cfg = {'name': season, 'month': season.capitalize(),
                     'start_hour': start_hour}
    for it in config.ITERATIONS:
        if it['name'] == season:
            iteration_cfg = it
            break

    # ── Run full postprocess ───────────────────────────────────────────────────
    # Temporarily override NUM_HOURS in config so helpers use the right value
    _orig_nh = config.NUM_HOURS
    config.NUM_HOURS = num_hours
    try:
        run_postprocess(
            baseline_solution, baseline_graph,
            ev_solution,       ev_graph,
            baseline_obj,      ev_obj,
            ev_charging_load,
            policies,
            iteration_cfg,
            iter_output_dir,
        )
    finally:
        config.NUM_HOURS = _orig_nh


# ===========================================================================
# CLI entry point
# ===========================================================================

if __name__ == '__main__':
    """
    Usage:
      # All iterations under config.OUTPUT_DIR:
      python ev_charging_project/postprocess.py

      # One specific iteration folder:
      python ev_charging_project/postprocess.py ev_charging_results/march
    """
    import argparse
    parser = argparse.ArgumentParser(
        description='Re-run postprocessing from saved solution JSONs.')
    parser.add_argument(
        'iter_dirs', nargs='*',
        help='Iteration output dir(s). Defaults to all subdirs of OUTPUT_DIR.')
    args = parser.parse_args()

    if args.iter_dirs:
        dirs = [os.path.abspath(d) for d in args.iter_dirs]
    else:
        base = os.path.join(_ROOT, config.OUTPUT_DIR)
        dirs = sorted(
            d for d in (os.path.join(base, n) for n in os.listdir(base)
                        if os.path.isdir(os.path.join(base, n)))
            if os.path.isdir(os.path.join(d, 'baseline'))
            and os.path.isdir(os.path.join(d, 'ev'))
        )

    if not dirs:
        print(f'No iteration directories found under {config.OUTPUT_DIR}.')
        sys.exit(0)

    print(f'Will re-postprocess {len(dirs)} iteration(s):')
    for d in dirs:
        print(f'  {d}')

    for d in dirs:
        try:
            run_postprocess_from_dir(d)
        except Exception as exc:
            print(f'\nERROR processing {d}: {exc}')
            import traceback; traceback.print_exc()

    try:
        from ev_charging_project.aggregate_postprocess import run_aggregate_postprocess
        run_aggregate_postprocess(config.OUTPUT_DIR)
    except Exception as exc:
        print(f'\nAggregate postprocess: {exc}')
