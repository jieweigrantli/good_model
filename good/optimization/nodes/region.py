from ..base.node import Node
from ..assets.load import Load
from ..assets.producer import Producer
import pyomo.environ as pyomo

class Region(Node):
    '''
    Nodes are the fundamental unit of analysis for the optimization. Nodes host assets
    and link terminals whose outputs must be balanced. The balancing constraint is nodal.

    Regions enforce energy balance at each time step. This can be enforced rigidly or
    permissively depending on shortfall and wastage parameters.

    Shortfall/wastage are included as a supplemental factor that allows for up to a 
    certain amount of wiggle room in the energy balance constraint and should receive a
    very high cost such that it will only be used if needed.

    The limits of shortfall are  [0, shortfall_capacity]
    The cost of shortfall is shortfall_cost

    The limits of wastage are  [0, wastage_capacity]
    The cost of wastage is wastage_cost
    '''
    def __init__(self, handle, **kwargs):
        
        super().__init__(handle, **kwargs)

        self.assets = kwargs.get('assets', {})
        self.imports = kwargs.get('imports', {})
        self.exports = kwargs.get('exports', {})

        self.shortfall_capacity = kwargs.get('shortfall_capacity', 0)
        self.shortfall_cost = kwargs.get('shortfall_cost', 1)

        self.wastage_capacity = kwargs.get('wastage_capacity', 0)
        self.wastage_cost = kwargs.get('wastage_cost', 1)

        # Step-down (transformer bank) rating in W, or None for no limit.
        #
        # Without this a node has unlimited throughput: power moves from the
        # transmission network into local load with no transformer between
        # them, so "this substation cannot deliver any more" is not a state
        # the model can represent. It is not a marginal omission -- PG&E's
        # published bank loadings run at a median 85.6% of rating, 41% of
        # substations are above 90% and 19% are already above 100%, so the
        # transformer binds across most of the system while the transmission
        # corridors above it still have headroom.
        #
        # The limit applies to *net* import, not gross flow, so a switching
        # station that passes power through at transmission voltage is
        # unaffected -- only energy actually stepped down to serve local load
        # crosses the transformer.
        self.transformer_capacity = kwargs.get('transformer_capacity', None)

    def parameters(self, model):

        for asset in self.assets.values():

            model = asset['object'].parameters(model)

        return model

    def variables(self, model):

        for asset in self.assets.values():

            model = asset['object'].variables(model)

        # Shortfall - avoids infeasibility due to insufficient supply
        handle = f"{self.handle}::shortfall"
        self.handles.append(handle)
        setattr(
            model, handle,
            pyomo.Var(
                model.steps,
                initialize = [0] * len(model.steps),
                bounds = (0, self.shortfall_capacity),
                ),
            )

        # Wastage - avoids infeasibility due to excess supply
        handle = f"{self.handle}::wastage"
        self.handles.append(handle)
        setattr(
            model, handle,
            pyomo.Var(
                model.steps,
                initialize = [0] * len(model.steps),
                bounds = (0, self.wastage_capacity),
                ),
            )

        return model

    def constraints(self, model):
        """Energy balance constraints"""

        for asset in self.assets.values():

            model = asset['object'].constraints(model)

        # Add constraints for all time steps
        for step in model.steps:

            # Energy
            asset_net_energy = sum(
                asset['object'].energy(model, step) for asset in self.assets.values()
            )

            # print(self.imports.keys())

            imported_energy = sum(
                import_line['object'].receive(model, step) \
                for import_edge in self.imports.values() \
                for import_line in import_edge['object'].lines.values()
                )

            exported_energy = sum(
                export_line['object'].transmit(model, step) \
                for export_edge in self.exports.values() \
                for export_line in export_edge['object'].lines.values()
                )

            shortfall = getattr(model, f"{self.handle}::shortfall")[step]
            wastage = getattr(model, f"{self.handle}::wastage")[step]
            
            net_energy = (
                asset_net_energy + imported_energy -
                exported_energy + shortfall - wastage
                )
            
            # Test the whole balance, not just the assets. A node whose assets
            # are all fixed-profile Loads (demand, EV load, profile-pinned
            # solar/wind) has a plain-float asset_net_energy, and the old
            # check skipped its balance entirely -- its demand was silently
            # ignored and its lines could export energy it never had. In the
            # nested WECC-CA model that was 2,641 of 3,060 nodes and 32% of all
            # demand. shortfall/wastage are always variables, so net_energy is
            # only constant for a node with no lines and no shortfall/wastage.
            if not isinstance(net_energy, (int, float)):

                setattr(
                    model, f"{self.handle}::balance:{step}",
                    pyomo.Constraint(expr = net_energy == 0)
                )

            # Step-down limit: net energy drawn from the network in this step
            # may not exceed what the transformer bank can carry. Skipped when
            # the node has no lines, since there is then nothing to limit.
            if self.transformer_capacity is not None:

                net_import = imported_energy - exported_energy

                if not isinstance(net_import, (int, float)):

                    setattr(
                        model, f"{self.handle}::transformer:{step}",
                        pyomo.Constraint(
                            expr = net_import <=
                                self.transformer_capacity * model.time_step
                        )
                    )

        return model

    def objective(self, model):
        """Sum the objectives of all assets"""

        net_asset_cost = sum(
            asset['object'].objective(model) for asset in self.assets.values()
            )

        imports_cost = sum(
            import_line['object'].objective(model) \
            for import_edge in self.imports.values() \
            for import_line in import_edge['object'].lines.values()
            )

        exports_cost = sum(
            export_line['object'].objective(model) \
            for export_edge in self.exports.values() \
            for export_line in export_edge['object'].lines.values()
            )

        shortfall_cost = sum(
            getattr(model, f"{self.handle}::shortfall")[step] for step in model.steps
            ) * self.shortfall_cost

        wastage_cost = sum(
            getattr(model, f"{self.handle}::wastage")[step] for step in model.steps
            ) * self.wastage_cost

        cost = (
            net_asset_cost + imports_cost - exports_cost + shortfall_cost + wastage_cost
            )

        return cost

    def solution(self, model):

        solution = {}

        for handle in self.handles:

            value = list(getattr(model, handle).extract_values().values())
            solution[handle.split('::')[1]] = value

        if hasattr(model, 'dual'):

            handle = f"{self.handle}::balance"

            duals = {str(k): model.dual[k] for k in model.dual.keys()}

            solution['clearing_price'] = (
                [v for k, v in duals.items() if handle in k]
                )

        return solution