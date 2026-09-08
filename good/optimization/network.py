__all__ = ['Network']

import time

import numpy as np
import pandas as pd
import networkx as nx
import pyomo.environ as pyomo
import pyomo.opt as opt
import pyomo.util.model_size as model_size
from pyomo.common.errors import ApplicationError

from copy import deepcopy

from . import *
members = dir()

from ..utilities import cprint
from ..graph import remove_self_edges

class Network:

    def __init__(self, **kwargs):

        self.verbose = kwargs.get('verbose', False)
        self.steps = kwargs.get('steps', (0, 1))
        self.time_step = kwargs.get('time_step', 3600.) # [s]
        self.amortization_period = kwargs.get('amortization_period', 31536000) # [s]

        self.shortfall_capacity = kwargs.get('shortfall_capacity', None)
        self.shortfall_cost = kwargs.get('shortfall_cost', None)

        self.wastage_capacity = kwargs.get('wastage_capacity', None)
        self.wastage_cost = kwargs.get('wastage_cost', None)

        self.graph = nx.DiGraph()
        self.assets = {}
        self.lines = {}
        self.policies = {}

    def size(self):

        return model_size.build_model_size_report(self.model)
    
    @property
    def solution(self):
        """
        Property that automatically builds the solution graph when accessed.
        The solution is cached after first access.
        """
        if not hasattr(self, '_solution'):
            self.build_solution_full()
        return self._solution
    
    @solution.setter
    def solution(self, value):
        """Setter for solution property"""
        self._solution = value

    def build_solution_full(self):

        nodes = []
        edges = []

        for source, node in self.graph._node.items():

            solution = node['object'].solution(self.model)

            solution_node = {
                **solution, **{k: v for k, v in node.items() if k != 'object'}
                }

            solution_node['assets'] = {}

            for key, asset in node['assets'].items():

                solution = asset['object'].solution(self.model)

                solution_node['assets'][key] = {
                    **solution, **{k: v for k, v in asset.items() if k != 'object'}
                    }

            nodes.append((source, solution_node))

            for target, edge in self.graph._adj[source].items():

                solution = edge['object'].solution(self.model)

                solution_edge = {
                    **solution, **{k: v for k, v in edge.items() if k != 'object'}
                }

                solution_edge['lines'] = {}

                for key, line in edge['lines'].items():

                    solution = line['object'].solution(self.model)

                    solution_edge['lines'][key] = {
                        **solution, **{k: v for k, v in line.items() if k != 'object'}
                        }

                edges.append((source, target, solution_edge))

        self._solution = self.graph.__class__()
        self._solution.add_nodes_from(nodes)
        self._solution.add_edges_from(edges)
    
    def solution_graph(self):

        t0 = time.time()

        nodes = []
        edges = []

        for source, node in self.graph._node.items():

            solution = node['object'].solution(self.model)

            solution_node = solution

            solution_node['assets'] = {}

            for key, asset in node['assets'].items():

                solution = asset['object'].solution(self.model)

                solution_node['assets'][key] = solution

            nodes.append((source, solution_node))

            for target, edge in self.graph._adj[source].items():

                solution = edge['object'].solution(self.model)

                solution_edge = solution

                solution_edge['lines'] = {}

                for key, line in edge['lines'].items():

                    solution = line['object'].solution(self.model)

                    solution_edge['lines'][key] = solution

                edges.append((source, target, solution_edge))

        solution = self.graph.__class__()
        solution.add_nodes_from(nodes)
        solution.add_edges_from(edges)

        cprint(f'Solution Graph Built: {time.time() - t0}', self.verbose)

        return solution

    def solution_dataframe(self, solution = None):

        t0 = time.time()

        if solution == None:

            solution = self.solution_graph()

        columns = {}

        for source, node in solution._node.items():

            _adj = solution._adj[source]

            node_columns = {}

            for key, value in node.items():

                if type(value) == list:

                    node_columns[f'{source}::{key}'] = value

            columns = {**columns, **node_columns}

            for handle, asset in node['assets'].items():

                asset_columns = {}

                for key, value in asset.items():
        
                    if type(value) == list:
        
                        asset_columns[f'{source}:{handle}::{key}'] = value
        
                columns = {**columns, **asset_columns}

            for target, edge in _adj.items():

                edge_columns = {}

                for key, value in edge.items():
        
                    if type(value) == list:
        
                        edge_columns[f'{source}:{target}::{key}'] = value
        
                columns = {**columns, **edge_columns}
            
                for handle, line in edge['lines'].items():

                    line_columns = {}
        
                    for key, value in line.items():
        
                        if type(value) == list:
            
                            line_columns[f'{source}:{target}:{handle}::{key}'] = value
            
                    columns = {**columns, **line_columns}

        time_steps = max([len(v) for k, v in columns.items()])

        for key, value in columns.items():

            if len(value) == 1:

                columns[key] = value + [0] * (time_steps - 1)

        df = pd.DataFrame.from_dict(columns)

        cprint(f'Solution DataFrame Built: {time.time() - t0}', self.verbose)

        return df

    def solve(self, **kwargs):

        self.verbose = kwargs.get('verbose', self.verbose)
        tee = kwargs.get('tee', False)
        solver_kw = kwargs.get('solver', {'_name': 'glpk'})

        # Handle both string and dict formats for solver specification
        if isinstance(solver_kw, str):
            solver = opt.SolverFactory(solver_kw)
            solver_name = solver_kw
        else:
            #Generating the solver object
            solver = opt.SolverFactory(**solver_kw)
            solver_name = solver_kw.get('_name') or solver_kw.get('name', 'unknown')
        # solver = opt.SolverFactory('cplex_direct')

        # Check if solver is available
        if not solver.available():
            
            # Provide solver-specific installation instructions
            solver_instructions = {
                'cbc': "Install via conda: conda install -c conda-forge coincbc, or download from https://www.coin-or.org/download/binary/Cbc/",
                'glpk': "Install via conda: conda install -c conda-forge glpk, or download from https://www.gnu.org/software/glpk/",
                'gurobi': "Install Gurobi Optimizer and obtain a license (free academic license available). Install via: pip install gurobipy, then download from https://www.gurobi.com/downloads/",
                'cplex': "Install IBM CPLEX Optimizer and obtain a license. Download from https://www.ibm.com/products/ilog-cplex-optimization-studio",
            }
            
            instruction = solver_instructions.get(solver_name.lower(), 
                "Please install the solver executable and ensure it's in your system PATH.")
            
            # Check for alternative solvers that might be available
            alternative_solvers = ['glpk', 'cbc', 'cplex', 'gurobi']
            available_alternatives = []
            for alt_solver in alternative_solvers:
                if alt_solver != solver_name.lower():
                    try:
                        test_solver = opt.SolverFactory(alt_solver)
                        if test_solver.available():
                            available_alternatives.append(alt_solver)
                    except Exception:
                        pass
            
            alt_msg = ""
            if available_alternatives:
                alt_msg = f" Alternatively, you can use one of these available solvers: {', '.join(available_alternatives)}"
            
            raise ApplicationError(
                f"No executable found for solver '{solver_name}'. "
                f"{instruction}.{alt_msg}"
            )

        self.model.dual = pyomo.Suffix(direction = pyomo.Suffix.IMPORT)

        # Building and solving as a linear problem. load_solutions=False so we
        # can inspect the termination condition ourselves before Pyomo raises
        # on a non-loadable status (e.g. 'aborted' from a hit TimeLimit).
        t0 = time.time()
        self.result = solver.solve(self.model, tee = tee, load_solutions = False)
        cprint(f'Problem Solved: {time.time() - t0}', self.verbose)

        term = self.result.solver.termination_condition
        status = self.result.solver.status
        cprint(f'Solver status: {status}; termination condition: {term}', self.verbose)

        # Mirrors the gate Pyomo's own model.solutions.load_from() applies
        # (it raises ValueError('bad status: aborted') on anything but
        # ok/warning); we check it ourselves first so callers get the actual
        # termination_condition/status instead of an opaque ValueError.
        loadable_status = {opt.SolverStatus.ok, opt.SolverStatus.warning}
        if status not in loadable_status or not self.result.solution:
            raise RuntimeError(
                f"Solver did not return a loadable solution "
                f"(status={status}, termination_condition={term})."
            )
        self.model.solutions.load_from(self.result)

        # Clear solution cache so it gets rebuilt if accessed
        if hasattr(self, '_solution'):
            delattr(self, '_solution')

    def build(self):

        self.model = pyomo.ConcreteModel()

        # Define time steps
        self.model.steps = pyomo.Set(
            initialize = list(range(self.steps[1] - self.steps[0]))
            )

        self.model.start = pyomo.Param(
            initialize = self.steps[0], domain = pyomo.Integers
            )

        self.model.stop = pyomo.Param(
            initialize = self.steps[-1], domain = pyomo.Integers
            )

        self.model.time_step = pyomo.Param(initialize = self.time_step)

        duration = len(self.model.steps) * self.model.time_step

        amortization = duration / self.amortization_period

        self.model.amortization = pyomo.Param(
            initialize = amortization
            )
        # self.model.total_time = len(self.model.steps) * self.model.time_step

        # t0 = time.time()
        self.feasibility_parameters()
        self.assign_edge_objects()
        # cprint(f'Parameters Built: {time.time() - t0}', self.verbose)

        t0 = time.time()
        self.build_parameters()
        cprint(f'Parameters Built: {time.time() - t0}', self.verbose)

        t0 = time.time()
        self.build_variables()
        cprint(f'Variables Built: {time.time() - t0}', self.verbose)

        t0 = time.time()
        self.build_constraints()
        cprint(f'Constraints Built: {time.time() - t0}', self.verbose)

        t0 = time.time()
        self.build_objective()
        cprint(f'Objective Built: {time.time() - t0}', self.verbose)

    def feasibility_parameters(self):

        for source, node in self.graph._node.items():

            if self.shortfall_capacity is not None:

                node['object'].shortfall_capacity = self.shortfall_capacity

            if self.shortfall_cost is not None:

                node['object'].shortfall_cost = self.shortfall_cost

            if self.wastage_capacity is not None:

                node['object'].wastage_capacity = self.wastage_capacity

            if self.wastage_cost is not None:

                node['object'].wastage_cost = self.wastage_cost

    def assign_edge_objects(self):

        for source, node in self.graph._node.items():

            node['imports'] = {}
            node['exports'] = {}

        for source, _adj in self.graph._adj.items():

            source_node = self.graph._node[source]

            for target, edge in _adj.items():

                target_node = self.graph._node[target]

                source_node['object'].exports[target] = edge
                target_node['object'].imports[source] = edge

    def build_objective(self):

        cost = 0

        for source, node in self.graph._node.items():

            cost += node['object'].objective(self.model)

            for target, edge in self.graph._adj[source].items():

                cost += edge['object'].objective(self.model)

        for policy in self.policies.values():

            cost += policy['object'].objective(self.model)

        self.model.objective = pyomo.Objective(
            expr = cost, sense = pyomo.minimize
            )

    def build_constraints(self):

        for source, node in self.graph._node.items():

            self.model = node['object'].constraints(self.model)

            for target, edge in self.graph._adj[source].items():

                self.model = edge['object'].constraints(self.model)

        for policy in self.policies.values():

            self.model = policy['object'].constraints(self.model)

    def build_variables(self):

        for source, node in self.graph._node.items():

            self.model = node['object'].variables(self.model)

            for target, edge in self.graph._adj[source].items():

                self.model = edge['object'].variables(self.model)

        for policy in self.policies.values():

            self.model = policy['object'].variables(self.model)

    def build_parameters(self):

        for source, node in self.graph._node.items():

            self.model = node['object'].parameters(self.model)

            for target, edge in self.graph._adj[source].items():

                self.model = edge['object'].parameters(self.model)

        for policy in self.policies.values():

            self.model = policy['object'].parameters(self.model)

    def from_graph(self, graph = nx.DiGraph(), policies = {}, **kwargs):

        graph = deepcopy(graph)
        policies = deepcopy(policies)

        graph = remove_self_edges(graph)

        for source, node in graph._node.items():

            _class = node.pop('_class')
            profiles = node.pop('profiles', {})

            assets = node.get('assets', {})

            self.add(_class, source, **node)

            for key, asset in assets.items():

                _class = asset.pop('_class')

                asset['node'] = source

                if isinstance(asset.get('profile', ''), str):

                    asset['profile'] = profiles.get(asset.get('profile', ''), None)

                self.add(_class, key, **asset)

        for source, _adj in graph._adj.items():
            for target, edge in _adj.items():

                _class = edge.pop('_class')

                edge['source'] = source
                edge['target'] = target

                lines = edge.get('lines', {})

                self.add(_class, f"{source}_{target}", **edge)

                for key, line in lines.items():

                    _class = line.pop('_class')

                    line['edge'] = (source, target)

                    self.add(_class, key, **line)

        for key, policy in policies.items():

            _class = policy.pop('_class')
            policy['assets'] = self.assets

            self.add(_class, key, **policy)

        return self

    def add(self, _class, handle, **kwargs):
        '''
        Adds and object to the network
        '''

        # Processing class
        if isinstance(_class, str):
            if _class in members:

                _class = eval(_class)

            else:

                raise GOOD_ClassNotFound

        # What is the base class of the object?
        _base = _class.__base__

        if _base is Node:

            # Add a node
            self.add_node(_class, handle, **kwargs)

        elif _base is Edge:

            source = kwargs.pop('source', None)
            target = kwargs.pop('target', None)

            # Add an edge
            self.add_edge(_class, handle, source, target, **kwargs)

        elif _base is Asset:

            node = kwargs.pop('node', '')

            # Add an asset
            self.add_asset(_class, handle, node, **kwargs)

        elif _base is Line:

            edge = kwargs.pop('edge', ('', ''))

            # Add a line
            self.add_line(_class, handle, edge, **kwargs)

        elif _base is Policy:

            # Add an policy
            self.add_policy(_class, handle, **kwargs)

        else:

            raise GOOD_InvalidBaseClass

    def add_asset(self, _class, handle, node, **kwargs):

        kwargs['object'] = _class(handle, **kwargs)

        # Checking for node
        if node not in self.graph.nodes:

            raise GOOD_NodeNotFound(node)

        self.assets[handle] = kwargs

        self.graph._node[node]['object'].assets[handle] = kwargs

    def add_line(self, _class, handle, edge, **kwargs):

        kwargs['object'] = _class(handle, **kwargs)

        # Checking for node
        if edge not in self.graph.edges:

            raise GOOD_EdgeNotFound(edges)

        self.lines[handle] = kwargs

        self.graph._adj[edge[0]][edge[1]]['object'].lines[handle] = kwargs

    def add_policy(self, _class, handle, **kwargs):

        kwargs['object'] = _class(handle, **kwargs)

        self.policies[handle] = kwargs

    def add_node(self, _class, handle, **kwargs):

        obj = _class(handle, **kwargs)

        self.graph.add_node(handle, object = obj, **kwargs)

    def add_edge(self, _class, handle, source, target, **kwargs):

        obj = _class(handle, **kwargs)

        self.graph.add_edge(source, target, object = obj, **kwargs)