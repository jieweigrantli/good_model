import time
import json

import numpy as np
import networkx as nx

### MOVE THIS STUFF OUT OF SRC ###

default_nx_kwargs = {
    'weight': 'cost',
    'resolution': 1.1,
    'cutoff': 1,
}

default_fields = {
    'heat_rate': 3412 / 2000,
    'operating_cost': 3.6e9 / 2000,
    'co2': 1 / (0.453592 / 3.6e9) / 10,
}

def communities(plants, fields = default_fields, nx_kwargs = default_nx_kwargs):

    # print(fields)

    k, v = np.unique([p['region'] for p in plants.values()], return_inverse = True)
    
    region_indices = {
        k[idx]: np.arange(0, len(v), 1, dtype = int)[v == idx] for idx in range(k.shape[0])
    }

    communities = []

    for region, indices in region_indices.items():
        
        region_plants = np.array([v for v in plants.values()])[indices]

        links = []
        
        for source in region_plants:
            for target in region_plants:
                if source['id'] != target['id']:
                    if source['type'] == target['type']:
        
                        distance = 0
            
                        for key, value in fields.items():
            
                            distance += (source[key] - target[key]) * value

                        cost = np.exp(-distance)

                        if cost < 1:

                            continue

                        if cost > 1e10:

                            cost = 1e10
            
                        link = (
                            source['id'],
                            target['id'],
                            {'cost': cost},
                        )
            
                        links.append(link)

        graph = nx.Graph()
        graph.add_edges_from(links)

        communities += (
            list(c) for c in list(
                nx.community.greedy_modularity_communities(graph, **nx_kwargs))
        )

    return communities

def combine_values(values, weights, fun):

    if callable(fun):

        return fun(values)

    elif isinstance(fun, str):

        if fun == 'first':

            return values[0]

        if fun == 'all':

            return values

        if fun == 'sum':

            return sum(values)

        if fun == 'mean':

            n = len(values)
            denominator = 1 if sum(weights) == 0 else sum(weights)

            return sum([values[idx] * weights[idx] for idx in range(n)]) / denominator

    return values

def combine(plants, communities, **kwargs):

    weight = kwargs.get('weight', 'installed_capacity')
    functions = kwargs.get('functions', {})

    combined = []

    for idx, community in enumerate(communities):

        members = [plants[key] for key in community]
        weights = [m[weight] for m in members]
        sum_weight = sum(weights)

        if sum_weight == 0:

            sum_weight = 1

        plant = {
            'id': f"combined_{idx}",
            "components": community,
            weight: sum([m[weight] for m in members])
            }

        for key, val in members[0].items():

            if key in ['id', weight]:

                continue

            if key not in functions:

                continue

            values = [m.get(key, None) for m in members if key in m]

            plant[key] = combine_values(values, weights, functions[key])

        combined.append(plant)

    return combined