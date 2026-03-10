import os
import time
import json

import pandas as pd
import numpy as np

name_to_code = {
    'Alabama': 'AL',
    'Nebraska': 'NE',
    'Alaska': 'AK',
    'Nevada': 'NV',
    'Arizona': 'AZ',
    'New Hampshire': 'NH',
    'Arkansas': 'AR',
    'New Jersey': 'NJ',
    'California': 'CA',
    'New Mexico': 'NM',
    'Colorado': 'CO',
    'New York': 'NY',
    'Connecticut': 'CT',
    'North Carolina': 'NC',
    'Delaware': 'DE',
    'North Dakota': 'ND',
    'District Of Columbia': 'DC',
    'District of Columbia': 'DC',
    'Ohio': 'OH',
    'Florida': 'FL',
    'Oklahoma': 'OK',
    'Georgia': 'GA',
    'Oregon': 'OR',
    'Hawaii': 'HI',
    'Pennsylvania': 'PA',
    'Idaho': 'ID',
    'Puerto Rico': 'PR',
    'Illinois': 'IL',
    'Rhode Island': 'RI',
    'Indiana': 'IN',
    'South Carolina': 'SC',
    'Iowa': 'IA',
    'South Dakota': 'SD',
    'Kansas': 'KS',
    'Tennessee': 'TN',
    'Kentucky': 'KY',
    'Texas': 'TX',
    'Louisiana': 'LA',
    'Utah': 'UT',
    'Maine': 'ME',
    'Vermont': 'VT',
    'Maryland': 'MD',
    'Virginia': 'VA',
    'Massachusetts': 'MA',
    'Virgin Islands': 'VI',
    'Michigan': 'MI',
    'Washington': 'WA',
    'Minnesota': 'MN',
    'West Virginia': 'WV',
    'Mississippi': 'MS',
    'Wisconsin': 'WI',
    'Missouri': 'MO ',
    'Wyoming': 'WY',
    'Montana': 'MT',
}

class_assignment = {}

# Build

def build_installed_assets(data, verbose = False):

    t0 = time.time()

     # Merging power plant data with impacts data
    # print('1', np.around(time.time() - t0, 4))
    plants = merging_data(data['plants_2021'], data['impacts_parsed'])
    # print('2', np.around(time.time() - t0, 4))

    # Assigning and adjusting fuel costs
    plants = assign_fuel_costs(plants)
    # print('3', np.around(time.time() - t0, 4))
    plants = adjust_coal_generation_cost(plants)
    # print('4', np.around(time.time() - t0, 4))
    plants = adjust_oil_generation_cost(plants)
    # print('5', np.around(time.time() - t0, 4))
    plants = adjust_nuclear_generation_cost(plants)
    # print('6', np.around(time.time() - t0, 4))

    # Filling missing fuel costs
    plants = assign_em_rates(plants, data['plants_2020'])
    # print('7', np.around(time.time() - t0, 4))

    _print(f'Installed assets built: {time.time() - t0:.4} seconds', disp = verbose)

    return plants

def build_optional_assets(data, verbose = False):

    t0 = time.time()

    capex = {
        'capacity': {},
        'cost': {},
    }

    capex['capacity']['wind'], capex['capacity']['solar'] = ffill_ren_cap(
        data['onshore_wind_capacity'], data['solar_regional_capacity']
        )


    capex['cost']['wind'], capex['cost']['solar']  = ffill_ren_cost(
        data['onshore_wind_capital_cost'], data['solar_capital_cost']
        )
    
    capex['cost'] = renewable_transmission_cost(
        data['unit_cost'], data['regional_cost'], capex['cost']
        )

    _print(f'Optional assets built: {time.time() - t0:.4} seconds', disp = verbose)

    return capex

def build_lines(data, verbose = False):

    t0 = time.time()

    capacity = pd.DataFrame(
        data['transmission'], columns=["From", "To", "Capacity TTC (MW)"]
        )
   
    cost = pd.DataFrame(
        data['transmission'], columns=["From", "To", "Transmission Tariff (2016 mills/kWh)"]
        )
    
    # Create a pivot table to convert the DataFrame into a matrix
    capacity = capacity.pivot(
        index="From", columns="To", values="Capacity TTC (MW)"
        )
    
    cost = cost.pivot(
        index="From", columns="To", values="Transmission Tariff (2016 mills/kWh)"
        )
    
    # If there are missing values (NaN) in the matrix, you can fill them with 0
    capacity = capacity.fillna(0)
    cost = cost.fillna(0)

    _print(f'Lines built: {time.time() - t0:.4} seconds', disp = verbose)

    return {'capacity': capacity, 'cost': cost}

def build_profiles(data, verbose = False):

    t0 = time.time()

    profiles = {}

    profiles['wind'] = long_wide(data['onshore_wind_generation_profile'])
    profiles['solar'] = long_wide(data['solar_generation_profile'])
    profiles['load'] = long_wide_load(data['load_profile'])
    profiles['hydro'] = long_wide_hydro(data['hydro'])

    _print(f'Profiles built: {time.time() - t0:.4} seconds', disp = verbose)

    return profiles

def build_policies(data, verbose = False):

    t0 = time.time()

    policies = {}

    policies['rps'] = build_rps(data['rps'])

    _print(f'Policies built: {time.time() - t0:.4} seconds', disp = verbose)

    return policies

# Format

def format_installed_assets(assets, scale, verbose = False):

    t0 = time.time()

    non_dispatchable_types = ["Geothermal", "Onshore Wind", "Solar PV", "Solar Thermal"]

    tags = {
        'Hydro': 'hydro',
        'Geothermal': 'geothermal',
        'Onshore Wind': 'wind',
        'Pumped Storage': 'storage',
        'Solar PV': 'solar',
        'Solar Thermal': 'solar',
        'Energy Storage': 'storage',
        'Offshore Wind': 'wind',
        'New Battery Storage': 'storage',
    }

    fuels = {
        'Coal': 'coal',
        'IMPORT': 'import',
        'Oil': 'oil',
        'NaturalGas': 'natural gas',
        'Hydro': 'hydro',
        'Non-Fossil': 'non-fossil',
        'MSW': 'waste',
        'Geothermal': 'geothermal',
        'Wind': 'wind',
        'Fwaste': 'waste',
        'Biomass': 'biomass',
        'LF Gas': 'waste',
        'Pumps': 'pump hydro',
        'Solar': 'solar',
        'Tires': 'tires',
        'EnerStor': 'battery',
        'Nuclear': 'nuclear',
    }

    classes = {
        'generator': 'Producer',
        'hydro': 'Producer',
        'geothermal': 'Producer',
        'solar': 'Load',
        'wind': 'Load',
        'storage': 'Store',
    }

    data = {}

    for idx, row in assets.iterrows():

        dispatchable = True
        renewable = False

        if row['PlantType'] in non_dispatchable_types:

            dispatchable = False
            renewable = True

        plant_type = tags.get(row['PlantType'], 'generator')

        if plant_type == "hydro":

            capacity_factor = .4

        else:

            capacity_factor = 1

        profile = f"{row['RegionName']}:{plant_type}:"

        data[f'installed_{idx}'] = {
            'oris_code': (
                int(np.nan_to_num(row['ORISPL'])) if row['ORISPL'] is not np.nan else 'none',
                )[0],
            'egrid_id': row['UniqueID'] if row['UniqueID'] is not np.nan else 'none',
            'type': plant_type,
            'fuel': fuels[row['FuelType']],
            '_class': classes[plant_type],
            'profile': profile,
            'region': row['RegionName'],
            'jurisdiction': name_to_code[row['StateName']],
            'nerc': row['NERC'],
            'utility': row['UTLSRVNM'],
            'x': row['LON'],
            'y': row['LAT'],
            'installed_capacity': row['Capacity'] * 1e6, # [W]
            'capacity_factor': capacity_factor,
            'dispatchable': dispatchable,
            'combinable': True,
            'renewable': renewable,
            'extensible': False,
            'capex_capacity': 0,
            'capex_cost': 0,
            'operating_cost': np.nan_to_num(row['Fuel_VOM_Cost']) / 3.6e9,
            'heat_rate': np.nan_to_num(row['HeatRate']) / 3412,
            'nox': np.nan_to_num(row['PLNOXRTA']) * 0.453592 / 3.6e9,
            'so2': np.nan_to_num(row['PLSO2RTA']) * 0.453592 / 3.6e9,
            'co2': np.nan_to_num(row['PLCO2RTA']) * 0.453592 / 3.6e9,
            'ch4': np.nan_to_num(row['PLCH4RTA']) * 0.453592 / 3.6e9,
            'n2o': np.nan_to_num(row['PLN2ORTA']) * 0.453592 / 3.6e9,
            'pm': np.nan_to_num(row['PLPMTRO']) * 0.453592 / 3.6e9,
        }

    regions, indices = np.unique(assets['RegionName'], return_index = True)
    jurisdictions = assets['StateName'].to_numpy()[indices]

    for idx, region in enumerate(regions):

        profile = f"{region}:load"
        capacity = scale.get(profile, None)

        if capacity is None:

            continue

        data[f'base_load_{region}'] = {
            'oris_code': 'none',
            'egrid_id': 'none',
            'type': 'load',
            '_class': 'Load',
            'profile': profile,
            'region': region,
            'jurisdiction': None,
            'installed_capacity': capacity,
            'capacity_factor': 1,
            'dispatchable': False,
            'combinable': False,
            'renewable': False,
            'extensible': False,
            'capex_capacity': 0,
            'capex_cost': 0,
            'operating_cost': 0,
            'heat_rate': 0,
            'nox': 0,
            'so2': 0,
            'co2': 0,
            'ch4': 0,
            'n2o': 0,
            'pm': 0,
        }

    _print(f'Installed assets formatted: {time.time() - t0:.4} seconds', disp = verbose)

    return data

def format_optional_assets(capex, verbose = False):

    t0 = time.time()

    classes = {
        'generator': 'Producer',
        'hydro': 'Producer',
        'geothermal': 'Producer',
        'solar': 'Load',
        'wind': 'Load',
        'storage': 'Store',
    }

    data = {}

    k = -1

    for plant_type in ['wind', 'solar']:

        capacity = capex['capacity'][plant_type].copy()
        cost = capex['cost'][plant_type].copy()

        for idx in range(1, 7):

            capacity[str(idx)] = capacity[str(idx)].astype(str)
            cost[str(idx)] = cost[str(idx)].astype(str)

            capacity[idx] = capacity[str(idx)].str.replace(',', '').astype(float)
            cost[idx] = cost[str(idx)].str.replace(',', '').astype(float)

        capacity.index = capacity.apply(
            lambda r: f"{r['IPM Region']}:{r['State']}:{r['Resource Class']}", axis = 1,
            ).to_list()

        capacity = capacity.to_dict(orient = 'index')

        cost.index = cost.apply(
            lambda r: f"{r['IPM Region']}:{r['State']}:{r['Resource Class']}", axis = 1,
            ).to_list()

        cost = cost.to_dict(orient = 'index')

        for key in capacity.keys():

            # print(capacity[key])

            if key not in cost:

                continue

            capex_capacity = [
                capacity[key][idx] * 1e6 for idx in range(1, 7) \
                if not np.isnan(capacity[key][idx])
                ]

            capex_cost = [
                cost[key][idx] / 1e6 for idx in range(1, 7) if not np.isnan(cost[key][idx])
                ]

            if (not capex_capacity) or (not capex_cost):

                continue

            k += 1

            profile = (
                f"{capacity[key]['IPM Region']}:{plant_type}:{capacity[key]['Resource Class']}"
                )

            for idx in range(len(capex_capacity)):

                data[f'optional_{k}_{idx}'] = {
                    'oris_code': 'none',
                    'egrid_id': 'none',
                    'type': f'{plant_type}',
                    'fuel': plant_type,
                    '_class': classes[plant_type],
                    'profile': profile,
                    'region': capacity[key]['IPM Region'],
                    'jurisdiction': capacity[key]['State'],
                    'installed_capacity': 0,
                    'capacity_factor': 1,
                    'dispatchable': False,
                    'combinable': False,
                    'renewable': True,
                    'extensible': True,
                    'capex_capacity': capex_capacity[idx],
                    'capex_cost': capex_cost[idx],
                    'operating_cost': 0,
                    'heat_rate': 0,
                    'nox': 0,
                    'so2': 0,
                    'co2': 0,
                    'ch4': 0,
                    'n2o': 0,
                    'pm': 0,
                }
    
    regions = np.unique([p['region'] for p in data.values()])

    for idx, region in enumerate(regions):

        # profile = f"{region}:hydro"

        if capacity is None:

            continue

        data[f'optional_storage_{idx}'] = {
            'oris_code': 'none',
            'egrid_id': 'none',
            'type': 'battery',
            '_class': 'Store',
            # 'profile': profile,
            'region': region,
            'jurisdiction': None,
            'installed_capacity': 0,
            'capacity_factor': 1,
            'dispatchable': True,
            'combinable': False,
            'renewable': False,
            'extensible': True,
            'capex_capacity': np.inf,
            'capex_cost': 1200 / (4 * 3.6e6),
            'operating_cost': 0,
            'heat_rate': 0,
            'nox': 0,
            'so2': 0,
            'co2': 0,
            'ch4': 0,
            'n2o': 0,
            'pm': 0,
        }

    _print(f'Optional assets formatted: {time.time() - t0:.4} seconds', disp = verbose)

    return data

def format_lines(transmission, verbose = False):

    t0 = time.time()

    links = {}

    k = -1

    # Iterate through each row and column to extract data
    for source, row in transmission['capacity'].iterrows():
        if source in transmission['cost'].index:
            for target, capacity in row.items():
                if target in transmission['cost'].columns:

                    if ('CN_' in source) or ('CN_' in target):

                        continue

                    k += 1

                    cost = transmission['cost'].loc[source, target]

                    if capacity == 0:

                        continue

                    # Append data to link_example list
                    links[f'line_{k}'] = {
                        'source': source,
                        'target': target,
                        'type': 'line',
                        '_class': 'Transmission',
                        'installed_capacity': capacity * 1e6,
                        'operating_cost': cost / 3.6e9,
                        'dispatchable': True,
                        'extensible': False,
                        # Optimizer reads capex_limit for line expansion.
                        # Keep capex_capacity for backward compatibility with existing tooling.
                        'capex_limit': 0,
                        'capex_capacity': 0,
                        'capex_cost': 0,
                        }

    _print(f'Lines formatted: {time.time() - t0:.4} seconds', disp = verbose)

    return links

def format_profiles(profiles, verbose = False):

    t0 = time.time()

    data = {}
    scale = {}

    region = ''

    for idx, row in profiles['solar'].iterrows():

        row_region = row['Region Name']

        data = nested_add(
            data, row['Profile'], f"{row_region}:solar:{row['Resource Class']}"
            )

        if row_region != region:

            region = row_region

            data = nested_add(
                data, row['Profile'], f"{row_region}:solar:"
                )

    region = ''

    for idx, row in profiles['wind'].iterrows():

        row_region = row['Region Name']

        data = nested_add(
            data, row['Profile'], f"{row_region}:wind:{row['Resource Class']}"
            )

        if row_region != region:

            region = row_region

            data = nested_add(
                data, row['Profile'], f"{row_region}:wind:"
                )

    for idx, row in profiles['load'].iterrows():

        profile = row['Profile']
        capacity = min(row['Profile'])

        data = nested_add(
            data, row['Profile'] / capacity, f"{row['Region']}:load"
            )

        scale = nested_add(
            scale, capacity, f"{row['Region']}:load"
            )

    for idx, row in profiles['hydro'].iterrows():

        profile = row['Profile']
        capacity = max(row['Profile'])

        data = nested_add(
            data, row['Profile'] / capacity, f"{row['Region']}:hydro"
            )

        scale = nested_add(
            scale, capacity, f"{row['Region']}:hydro"
            )

    _print(f'Profiles formatted: {time.time() - t0:.4} seconds', disp = verbose)

    return data, scale

def format_policies(policies, verbose = False):

    t0 = time.time()

    data = {}

    k = -1

    for jurisdiction, ratio in policies['rps'].items():

        k += 1

        data[f'rps_{jurisdiction}'] = {
            'type': f'rps',
            '_class': 'Portfolio_Standard',
            'ratio': ratio,
            'inclusion_criteria': {
                "renewable": "lambda a: a.get('renewable', False)",
                "not_battery": "lambda a: a.get('_class', '') != 'Store'",
                "not_load": "lambda a: a.get('type', '') != 'load'",
                "jurisdiction": f"lambda a: a.get('jurisdiction', '') == '{jurisdiction}'",
            },
            'exclusion_criteria': {
                "renewable": "lambda a: not a.get('renewable', False)",
                "not_battery": "lambda a: a.get('_class', '') != 'Store'",
                "not_load": "lambda a: a.get('type', '') != 'load'",
                "jurisdiction": f"lambda a: a.get('jurisdiction', '') == '{jurisdiction}'",
            },
        }

    _print(f'Policies formatted: {time.time() - t0:.4} seconds', disp = verbose)

    return data

# Write

def write_assets(data, output_path = '', filename = 'assets.json', verbose = False):

    t0 = time.time()

    with open(output_path + filename, 'w') as file:

        json.dump(data, file, indent = 4, cls = NpEncoder)

    _print(f'Assets written: {time.time() - t0:.4} seconds', disp = verbose)

def write_lines(data, output_path = '', filename = 'lines.json', verbose = False):

    t0 = time.time()

    with open(output_path + filename, 'w') as file:

        json.dump(data, file, indent = 4, cls = NpEncoder)

    _print(f'Lines written: {time.time() - t0:.4} seconds', disp = verbose)

def write_profiles(data, output_path = '', foldername = 'profiles/', verbose = False):

    t0 = time.time()

    directory = output_path + foldername

    if not os.path.exists(directory):

        os.makedirs(directory)

    for key, val in data.items():

        filename = directory + key.replace(':', '--') + '.json'

        with open(filename, 'w') as file:

            json.dump(val, file, indent = 4, cls = NpEncoder)

    _print(f'Profles written: {time.time() - t0:.4} seconds', disp = verbose)

def write_policies(data, output_path = '', filename = 'policies.json', verbose = False):

    t0 = time.time()

    with open(output_path + filename, 'w') as file:

        json.dump(data, file, indent = 4, cls = NpEncoder)

    _print(f'Policies written: {time.time() - t0:.4} seconds', disp = verbose)

# Processing functions

class NpEncoder(json.JSONEncoder):
    '''
    Encoder to allow for numpy types to be converted to default types for
    JSON serialization. For use with json.dump(s)/load(s).
    '''
    def default(self, obj):

        if isinstance(obj, np.integer):

            return int(obj)

        if isinstance(obj, np.floating):

            return float(obj)

        if isinstance(obj, np.ndarray):

            return obj.tolist()

        return super(NpEncoder, self).default(obj)

def _print(string, disp = True):

    if disp:

        print(string)

def nested_add(dictionary, value, *keys):
    '''
    Add entry to dictionary and create required nesting
    '''

    level = dictionary

    for key in keys[:-1]:

        if key not in level:

            level[key] = {}

        level = level[key]

    level[keys[-1]] = value
        
    return dictionary

def build_rps(df, states = None, year = 2025):

    if states == None:

        states = df['st'].unique()

    data = {}

    for state in states:

        df_s = df[df['st'] == state]

        data[state] = np.interp(year, df_s['t'], df_s['rps_all'])

    return data

def renewable_transmission_cost(unit_cost, region_cost, capital_cost, year = '2023'):

    unit_capital_cost = unit_cost[unit_cost["cost"] == 'Capital(2016$/kW)']
    # unit_capital_cost['year'] = unit_capital_cost.apply()

    # Selecting relevant rows based on user input
    selected_rows = unit_cost[unit_cost['year'].str.contains(year)]
    selected_rows = selected_rows[selected_rows["cost"] == 'Capital(2016$/kW)']
    selected_rows = selected_rows[["SolarPhotovoltaic", "OnshoreWind"]]


    # Creating a new DataFrame for regional costs
    regional_cost_selected = (
        region_cost[['ModelRegion', 'OnshoreWind', 'SolarPV']].copy()
        )
    regional_cost_selected['OnshoreWind'] = (
        regional_cost_selected['OnshoreWind'] * selected_rows['OnshoreWind'].iloc[0]
        )
    regional_cost_selected['SolarPV'] = (
        regional_cost_selected['SolarPV'] * selected_rows['SolarPhotovoltaic'].iloc[0]
        )
    regional_cost_selected = (
        regional_cost_selected.rename(columns={"ModelRegion": "IPM Region"})
        )

    # Merging regional cost information with wind and solar capital cost DataFrames
    capital_cost['wind'] = pd.merge(
        capital_cost['wind'],
        regional_cost_selected[["IPM Region", "OnshoreWind"]],
        how="left",
        on="IPM Region",
        )

    capital_cost['solar'] = pd.merge(
        capital_cost['solar'],
        regional_cost_selected[["IPM Region", "SolarPV"]],
        how="left",
        on="IPM Region",
        )

    # Summing values and removing redundant columns for wind and solar capital costs
    capital_cost['wind'].iloc[:, 3:9] = (
        capital_cost['wind'].iloc[:, 3:9].add(capital_cost['wind'].iloc[:, -1], axis=0)
        )

    capital_cost['solar'].iloc[:, 3:9] = (
        capital_cost['solar'].iloc[:, 3:9].add(
            capital_cost['solar'].iloc[:, -1], axis=0
            )
        )

    capital_cost['wind'] = capital_cost['wind'].iloc[:, :-1]
    capital_cost['solar'] = capital_cost['solar'].iloc[:, :-1]

    return capital_cost

def ffill_ren_cap(Wind_onshore_capacity_df, Solar_regional_capacity_df):
    Wind_onshore_capacity_df['IPM Region'].ffill(inplace=True)
    Wind_onshore_capacity_df['State'].ffill(inplace=True)
    Solar_regional_capacity_df['IPM Region'].ffill(inplace=True)
    Solar_regional_capacity_df['State'].ffill(inplace=True)
    return Wind_onshore_capacity_df, Solar_regional_capacity_df

def ffill_ren_cost(Wind_onshore_cost_df, Solar_regional_cost_df):
    Wind_onshore_cost_df['IPM Region'].ffill(inplace=True)
    Wind_onshore_cost_df['State'].ffill(inplace=True)
    Solar_regional_cost_df['IPM Region'].ffill(inplace=True)
    Solar_regional_cost_df['State'].ffill(inplace=True)

    return Wind_onshore_cost_df, Solar_regional_cost_df

def long_wide_load(df):

    for key in df.keys():

        if 'Hour' in key:

            df[key] = df[key].astype(str).str.replace(',', '').astype(float) * -1e6

    # Define the columns to keep (first four columns)
    columns_to_keep = ['Region']

    # Group by the first four columns and concatenate columns 6 to 26
    result_df = df.groupby(columns_to_keep).apply(
        lambda x: x.iloc[:, 3:].values.flatten()
        ).reset_index()

    result_df = result_df.rename(columns={result_df.columns[1]: "Profile"})

    # Convert the Profile column to a list of lists
    result_df['Profile'] = result_df['Profile'].tolist()

    return result_df

def long_wide_hydro(df):

    df = df[df['PlantType'] == 'Hydro']

    # Define the columns to keep (first four columns)
    columns_to_keep = ['Region']

    # Group by the first four columns and concatenate columns 6 to 26
    result_df = df.groupby(columns_to_keep).apply(
        lambda x: x.iloc[:, 3:].values.flatten()
        ).reset_index()

    result_df = result_df.rename(columns={result_df.columns[1]: "Profile"})

    # Convert the Profile column to a list of lists
    result_df['Profile'] = result_df['Profile'].tolist()

    return result_df

def long_wide(df):

    # Define the columns to keep (first four columns)
    columns_to_keep = ['Region Name', 'State Name', 'Resource Class']

    # Group by the first four columns and concatenate columns 6 to 26,
    # and convert the kwh/MW to MWh/MW
    result_df = df.groupby(columns_to_keep).apply(
        lambda x: (x.iloc[:, 6:] / 1000).values.flatten()
        ).reset_index()

    result_df = result_df.rename(columns={result_df.columns[3]: "Profile"})

    # Convert the Profile column to a list of lists
    result_df['Profile'] = result_df['Profile'].tolist()


    return result_df

def assign_em_rates(input_df, input_df_old):

    input_df.loc[input_df["FuelType"].isin(["Pumps", "Hydro", "Geothermal", "Non-Fossil", "EnerStor", "Nuclear", "Solar", "Wind"]), ["PLCO2RTA", "PLSO2RTA", "PLCH4RTA", "PLN2ORTA", "PLNOXRTA"]] = 0
    for r in range(input_df.shape[0]):
        if np.isnan(input_df.at[r, 'PLCO2RTA']):
            # Expand search to the same state if no similar plants found in the same state
            similar_rows = input_df[(input_df['FuelType'] == input_df.at[r, 'FuelType']) &
                                    (input_df['StateName'] == input_df.at[r, 'StateName']) &
                                    (input_df['PlantType'] == input_df.at[r, 'PlantType']) &
                                    (input_df['Capacity'] > input_df.at[r, 'Capacity'] * 0.85) &
                                    (input_df['Capacity'] < input_df.at[r, 'Capacity'] * 1.15) &
                                    (input_df['HeatRate'] > input_df.at[r, 'HeatRate'] * 0.85) &
                                    (input_df['HeatRate'] < input_df.at[r, 'HeatRate'] * 1.15)]

            input_df.loc[r, ['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']] = similar_rows[['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']].mean()

        if np.isnan(input_df.at[r, 'PLCO2RTA']):
            # Expand search to the same NERC region if no similar plants found in the same NERC region
            similar_rows = input_df[(input_df['FuelType'] == input_df.at[r, 'FuelType']) &
                                    (input_df['NERC'] == input_df.at[r, 'NERC']) &
                                    (input_df['PlantType'] == input_df.at[r, 'PlantType']) &
                                    (input_df['Capacity'] > input_df.at[r, 'Capacity'] * 0.85) &
                                    (input_df['Capacity'] < input_df.at[r, 'Capacity'] * 1.15) &
                                    (input_df['HeatRate'] > input_df.at[r, 'HeatRate'] * 0.85) &
                                    (input_df['HeatRate'] < input_df.at[r, 'HeatRate'] * 1.15)]

            input_df.loc[r, ['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']] = similar_rows[['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']].mean()

        if np.isnan(input_df.at[r, 'PLCO2RTA']):
            # Expand search to all similar plants if no similar plants found in entire state
            similar_rows = input_df[(input_df['FuelType'] == input_df.at[r, 'FuelType']) &
                                    (input_df['PlantType'] == input_df.at[r, 'PlantType']) &
                                    (input_df['Capacity'] > input_df.at[r, 'Capacity'] * 0.85) &
                                    (input_df['Capacity'] < input_df.at[r, 'Capacity'] * 1.15) &
                                    (input_df['HeatRate'] > input_df.at[r, 'HeatRate'] * 0.85) &
                                    (input_df['HeatRate'] < input_df.at[r, 'HeatRate'] * 1.15)]

            input_df.loc[r, ['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']] = similar_rows[['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']].mean()


        if np.isnan(input_df.at[r, 'PLCO2RTA']):
            # Expand search to the same state if no similar plants found in the same state
            similar_rows = input_df[(input_df['FuelType'] == input_df.at[r, 'FuelType']) &
                                    (input_df['StateName'] == input_df.at[r, 'StateName']) &
                                    (input_df['PlantType'] == input_df.at[r, 'PlantType'])]

            input_df.loc[r, ['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']] = similar_rows[['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']].mean()

        if np.isnan(input_df.at[r, 'PLCO2RTA']):
            # Expand search to the same NERC region if no similar plants found in the same NERC region
            similar_rows = input_df[(input_df['FuelType'] == input_df.at[r, 'FuelType']) &
                                    (input_df['NERC'] == input_df.at[r, 'NERC']) &
                                    (input_df['PlantType'] == input_df.at[r, 'PlantType'])]

            input_df.loc[r, ['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']] = similar_rows[['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']].mean()

        if np.isnan(input_df.at[r, 'PLCO2RTA']):
            # Expand search to all similar plants if no similar plants found in entire state
            similar_rows = input_df[(input_df['FuelType'] == input_df.at[r, 'FuelType']) &
                                    (input_df['PlantType'] == input_df.at[r, 'PlantType'])]

            input_df.loc[r, ['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']] = similar_rows[['PLCO2RTA', 'PLNOXRTA', 'PLCH4RTA', 'PLN2ORTA', 'PLSO2RTA']].mean()

        if pd.isna(input_df.at[r, 'PLCO2RTA']) and input_df.at[r, 'Capacity'] < 50:
            input_df.at[r, 'PLCO2RTA'] = 0
            input_df.at[r, 'PLNOXRTA'] = 0
            input_df.at[r, 'PLCH4RTA'] = 0
            input_df.at[r, 'PLN2ORTA'] = 0
            input_df.at[r, 'PLSO2RTA'] = 0
    # PM emissions
    input_df['PLPMTRO'] = np.select(
        [(input_df['FuelType'] == 'Coal') & (input_df['PLPRMFL'] == 'RC'),
         (input_df['FuelType'] == 'Coal') & (input_df['PLPRMFL'] != 'RC'),
         (input_df['FuelType'] == 'Oil') & (input_df['PLPRMFL'] != 'WO'),
         (input_df['FuelType'] == 'NaturalGas'),
         (input_df['FuelType'] == 'LF Gas'),
         (input_df['FuelType'] == 'Biomass') & (input_df['PLPRMFL'].isin(['WDL', 'WDS'])),
         (input_df['FuelType'] == 'Oil') & (input_df['PLPRMFL'] == 'WO')],
        [0.08, 0.04, 1.4 / 145, 5.7 / 1020, 0.55 / 96.75, 0.017, 65 / 145],
        default=0)

    input_df['PLPMTRO'] = input_df['PLPMTRO'] * input_df['HeatRate'] / 1000

    # Adjust emissions for outliers based on conditions
    for r in range(input_df.shape[0]):

        if input_df.at[r, 'FuelType'] != 'Oil' and input_df.at[r, 'PLCO2RTA'] > 5000:
            if input_df.at[r, 'PLNGENAN'] > 1000:
                orispl = input_df.at[r, 'ORISPL']
                old_values = input_df_old[(input_df_old['ORISPL'] == orispl)]
                if not old_values.empty:
                    input_df.loc[r, ['PLCO2RTA', 'PLSO2RTA', 'PLCH4RTA', 'PLN2ORTA', 'PLNOXRTA']] = old_values[
                        ['PLCO2RTA', 'PLSO2RTA', 'PLCH4RTA', 'PLN2ORTA', 'PLNOXRTA']].values[0]

        if input_df.at[r, 'FuelType'] != 'Oil' and input_df.at[r, 'PLCO2RTA'] > 250000:
            input_df.loc[r, ['PLCO2RTA', 'PLSO2RTA', 'PLCH4RTA', 'PLN2ORTA', 'PLNOXRTA']] = input_df.loc[r, ['PLCO2RTA', 'PLSO2RTA', 'PLCH4RTA', 'PLN2ORTA', 'PLNOXRTA']] / 1000

    return input_df

def map_fuel_type(row_input):
    plant_type = row_input["PlantType"]
    egrid_primaryfuel = row_input["PLPRMFL"]  # Replace this with the actual egrid_primaryfuel column
    if plant_type == 'Coal Steam':
        return 'Coal'
    elif plant_type == 'Nuclear':
        return 'Nuclear'
    elif plant_type == 'O/G Steam':
        return 'Oil'
    elif plant_type == 'Biomass':
        return 'Biomass'
    elif plant_type == 'IMPORT':
        return 'IMPORT'
    elif plant_type == 'IGCC' or (plant_type == 'Combined Cycle' or egrid_primaryfuel == 'NaturalGas'):
        return 'NaturalGas'
    elif plant_type == 'Combined Cycle' and egrid_primaryfuel == 'NaturalGas':
        return 'NaturalGas'
    elif plant_type == 'Geothermal':
        return 'Geothermal'
    elif (plant_type == 'Combustion Turbine') and (egrid_primaryfuel == 'NG'):
        return 'NaturalGas'
    elif (plant_type == 'Combustion Turbine') and (egrid_primaryfuel == 'DFO'):
        return 'Oil'
    elif (plant_type == 'Combustion Turbine') and (egrid_primaryfuel == 'WDS'):
        return 'Biomass'
    else:
        return row_input["FuelType"]

def adjust_coal_generation_cost(df, target_mean = 23):
    # Filter only the rows with FuelType 'Coal'
    coal_data = df[df['FuelType'] == 'Coal'].copy()
    
    # Calculate the current mean
    current_mean = coal_data['Fuel_VOM_Cost'].mean()

    # Adjust the costs to have the target mean
    adjustment_factor = target_mean / current_mean
    coal_data['adjusted_cost'] = coal_data['Fuel_VOM_Cost'] * adjustment_factor

    # Replace the original Fuel_VOM_Cost with the adjusted values
    df.loc[df['FuelType'] == 'Coal', 'Fuel_VOM_Cost'] = coal_data['adjusted_cost']

    return df

def adjust_oil_generation_cost(df, target_mean = 32):
    # Filter only the rows with FuelType 'Oil'
    oil_data = df[df['FuelType'] == 'Oil'].copy()

    # Calculate the current mean
    current_mean = oil_data['Fuel_VOM_Cost'].mean()

    # Adjust the costs to have the target mean
    adjustment_factor = target_mean / current_mean
    oil_data['adjusted_cost'] = oil_data['Fuel_VOM_Cost'] * adjustment_factor

    # Replace the original Fuel_VOM_Cost with the adjusted values
    df.loc[df['FuelType'] == 'Oil', 'Fuel_VOM_Cost'] = oil_data['adjusted_cost']

    return df

def adjust_nuclear_generation_cost(df, target_mean = 21.2):
    # Filter only the rows with FuelType 'Nuclear'
    nuclear_data = df[df['FuelType'] == 'Nuclear'].copy()

    # Calculate the current mean
    current_mean = nuclear_data['Fuel_VOM_Cost'].mean()

    # Adjust the costs to have the target mean
    adjustment_factor = target_mean / current_mean
    nuclear_data['adjusted_cost'] = nuclear_data['Fuel_VOM_Cost'] * adjustment_factor

    # Replace the original Fuel_VOM_Cost with the adjusted values
    df.loc[df['FuelType'] == 'Nuclear', 'Fuel_VOM_Cost'] = nuclear_data['adjusted_cost']

    return df

def assign_fuel_costs(input_df):
    selected_columns = [
        "UniqueID", "ORISPL", "PLNGENAN",  "RegionName", "StateName", "CountyName", "NERC",
        "PlantType", "FuelType", "FossilUnit", "Capacity", "Firing", "Bottom",
        "EMFControls", "FOMCost" , "FuelUseTotal", "FuelCostTotal", "VOMCostTotal",
        "UTLSRVNM", "SUBRGN", "FIPSST", "FIPSCNTY", "LAT", "LON", "PLPRMFL", "PLNOXRTA",
        "PLSO2RTA", "PLCO2RTA", "PLCH4RTA", "PLN2ORTA", "HeatRate"
        ]

    merged_short = input_df[selected_columns].copy()
    merged_short["FuelCost[$/MWh]"] = (
        (merged_short["FuelCostTotal"] / (merged_short["FuelUseTotal"] + 1)) *
        merged_short["HeatRate"]
        ) / 1000
    # Define the plant types that should use VOMCostTotal directly
    plant_types_direct_vom = [
        "Solar", "Solar PV", "Wind", "Hydro", "Energy Storage", "Solar Thermal",
        "New Battery Storage", "Offshore Wind"
        ]

    # Calculate VOMCost[$/MWh] with conditions
    merged_short["VOMCost[$/MWh]"] = np.where(
        merged_short["PlantType"].isin(plant_types_direct_vom),
        merged_short["VOMCostTotal"],
        (
            ((merged_short["VOMCostTotal"] / (merged_short["FuelUseTotal"] + 1)) *
                merged_short["HeatRate"]) / 1000
            )
    )

    # Calculate FOMCost[$/MWh] with conditions
    merged_short["FOMCost[$/MWh]"] = np.where(
        merged_short["PlantType"].isin(plant_types_direct_vom),
        merged_short["FOMCost"],
        (((merged_short["FOMCost"] * 1e6)) / (merged_short["Capacity"] * 8760))
    )

    # Add FOMCost[$/MWh] to VOMCost[$/MWh] if PlantType is Nuclear
    merged_short["VOMCost[$/MWh]"] = np.where(
        merged_short["PlantType"] == "Nuclear",
        merged_short["VOMCost[$/MWh]"] + merged_short["FOMCost[$/MWh]"],
        merged_short["VOMCost[$/MWh]"]
    )

    # Identify rows with NaN values in the "NERC" column
    nan_indices = merged_short[merged_short['NERC'].isna()].index

    # Fill NaN values in "NERC" column with mode of "NERC" for the same region
    for idx in nan_indices:

        region = merged_short.at[idx, 'RegionName']
        region_mode = merged_short[merged_short['RegionName'] == region]['NERC'].mode()

        if not region_mode.empty:

            merged_short.at[idx, 'NERC'] = region_mode.iloc[0]

    merged_short["FuelType"] = merged_short.apply(map_fuel_type, axis=1)
    merged_short = (
        merged_short[~(
            merged_short["FuelType"].isna() & (merged_short["PlantType"] != "IMPORT")
            )].reset_index(drop=True)
        )

    # return

    for idx, row in merged_short.iterrows():

        fuel_type = row['FuelType']
        fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['FuelCost[$/MWh]'] > 1)]['FuelCost[$/MWh]']
        mean_cost = fuel_costs.mean()
        std_cost = fuel_costs.std()
        threshold = mean_cost - (1/2) * std_cost

        if threshold < 0:

            threshold = 0

        if row['FuelCost[$/MWh]'] <= threshold:
            non_zero_costs = merged_short[(merged_short['FuelType'] == row['FuelType']) & (merged_short['RegionName'] == row['RegionName']) & (merged_short['FuelCost[$/MWh]'] > threshold)]['FuelCost[$/MWh]']
            
            if not non_zero_costs.empty:
                merged_short.at[idx, 'FuelCost[$/MWh]'] = np.random.choice(non_zero_costs)

            if merged_short.at[idx, 'FuelCost[$/MWh]'] <= threshold:

                state_fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['StateName'] == row['StateName']) & (merged_short['FuelCost[$/MWh]'] > threshold)]['FuelCost[$/MWh]']
                non_zero_costs = state_fuel_costs[state_fuel_costs > threshold]

                if not non_zero_costs.empty:

                    merged_short.at[idx, 'FuelCost[$/MWh]'] = np.random.choice(non_zero_costs)

                if merged_short.at[idx, 'FuelCost[$/MWh]'] <= threshold:

                    adj_fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['NERC'] == row['NERC']) & (merged_short['FuelCost[$/MWh]'] > threshold)]['FuelCost[$/MWh]']
                    non_zero_costs = adj_fuel_costs[adj_fuel_costs > threshold]

                    if not non_zero_costs.empty:

                        merged_short.at[idx, 'FuelCost[$/MWh]'] = np.random.choice(non_zero_costs)

                    if merged_short.at[idx, 'FuelCost[$/MWh]'] <= threshold:

                        all_fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['FuelCost[$/MWh]'] > threshold)]['FuelCost[$/MWh]']
                        non_zero_costs = all_fuel_costs[all_fuel_costs > threshold]

                        if not non_zero_costs.empty:

                            merged_short.at[idx, 'FuelCost[$/MWh]'] = np.random.choice(non_zero_costs)

    for idx, row in merged_short.iterrows():

        fuel_type = row['FuelType']
        fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['VOMCost[$/MWh]'] > 1)]['VOMCost[$/MWh]']
        mean_cost = fuel_costs.mean()
        std_cost = fuel_costs.std()
        threshold = mean_cost - (2) * std_cost

        if threshold < 0:

            threshold = 0

        if row['VOMCost[$/MWh]'] <= threshold:

            non_zero_costs = merged_short[(merged_short['FuelType'] == row['FuelType']) & (merged_short['RegionName'] == row['RegionName']) & (merged_short['VOMCost[$/MWh]'] > threshold)]['VOMCost[$/MWh]']
            
            if not non_zero_costs.empty:

                merged_short.at[idx, 'VOMCost[$/MWh]'] = np.random.choice(non_zero_costs)

            if merged_short.at[idx, 'VOMCost[$/MWh]'] <= threshold:

                state_fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['StateName'] == row['StateName']) & (merged_short['VOMCost[$/MWh]'] > threshold)]['VOMCost[$/MWh]']
                non_zero_costs = state_fuel_costs[state_fuel_costs > threshold]

                if not non_zero_costs.empty:

                    merged_short.at[idx, 'VOMCost[$/MWh]'] = np.random.choice(non_zero_costs)

                if merged_short.at[idx, 'VOMCost[$/MWh]'] <= threshold:

                    adj_fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['NERC'] == row['NERC']) & (merged_short['VOMCost[$/MWh]'] > threshold)]['VOMCost[$/MWh]']
                    non_zero_costs = adj_fuel_costs[adj_fuel_costs > threshold]

                    if not non_zero_costs.empty:

                        merged_short.at[idx, 'VOMCost[$/MWh]'] = np.random.choice(non_zero_costs)

                    if merged_short.at[idx, 'VOMCost[$/MWh]'] <= threshold:

                        all_fuel_costs = merged_short[(merged_short['FuelType'] == fuel_type) & (merged_short['VOMCost[$/MWh]'] > threshold)]['VOMCost[$/MWh]']
                        non_zero_costs = all_fuel_costs[all_fuel_costs > threshold]

                        if not non_zero_costs.empty:

                            merged_short.at[idx, 'VOMCost[$/MWh]'] = np.random.choice(non_zero_costs)

    merged_short["Fuel_VOM_Cost"] = merged_short["FuelCost[$/MWh]"] + merged_short["VOMCost[$/MWh]"]

    return merged_short

def merging_data(plant, parsed):

    parsed.loc[:, "ORISCode"] = parsed["ORISCode"].copy()
    parsed["ORISPL"] = parsed["ORISCode"]
    merged = pd.merge(parsed, plant, how = "left", on = "ORISPL")
    merged = merged.dropna(how = 'all')

    return merged