import json

import numpy as np
import networkx as nx

from .graph import graph_from_nlg

# Building a graph to use as a Network input

def build_graph(assets, lines, profiles, **kwargs):

    nodes = build_nodes(assets, profiles)
    edges = build_edges(lines)

    graph = nx.DiGraph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from(edges)

    # graph = graph_from_nlg({'nodes': nodes, 'links': lines}, directed = True)

    return graph

def build_edges(lines):

    pairs = list(set([(v['source'], v['target']) for k, v in lines.items()]))

    edges = []

    for source, target in pairs:

        edge = {
            'id': f'{source}:{target}',
            '_class': 'Link',
            'lines': {k: v for k, v in lines.items() \
            if (v['source'] == source and v['target'] == target)
            },
        }

        edges.append((source, target, edge))

    return edges

def build_nodes(assets, profiles):

    # Getting unique regions
    regions = np.unique([p['region'] for p in assets.values()])

    nodes = []

    for region in regions:

        node = {
        'id': region,
        '_class': 'Region'
        }

        # Adding assets
        node['assets'] = {k: v for k, v in assets.items() if v['region'] == region}

        # Adding profiles
        node['profiles'] = (
            {k: v for k, v in profiles.items() if k.split(':')[0] == region}
            )

        nodes.append((region, node))

    return nodes