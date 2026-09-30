import numpy as np
import pandas as pd
import xarray as xr

from ...schema import RegionParams
from ..base import Node


class Region(Node):
    '''
    A balancing region. In every step, energy injected by the region's assets
    and imported over lines must equal energy consumed and exported:

        sum(injections) + shortfall - wastage = demand

    ``shortfall`` is unserved energy priced at ``shortfall_cost`` (value of
    lost load) and ``wastage`` is surplus that cannot be used or curtailed,
    priced at ``wastage_cost``. Both are bounded by their ``*_capacity``.
    Unset values take the Network's defaults.

    The dual of the balance constraint, divided by the step length, is the
    region's clearing price in $/MWh.
    '''

    Params = RegionParams

    def setting(self, net, name):

        value = getattr(self.p, name)

        return getattr(net, name) if value is None else value

    @classmethod
    def build(cls, net, objs):

        m = net.model
        index = net.regions

        def region_param(name):

            by_handle = {o.handle: o.setting(net, name) for o in objs}

            return xr.DataArray([float(by_handle[r]) for r in index], coords=[index])

        shape = (len(index), len(net.time))

        def grid(values):

            return xr.DataArray(np.broadcast_to(values.values[:, None], shape).copy(), coords=[index, net.time])

        shortfall = m.add_variables(lower=0, upper=grid(region_param("shortfall_capacity")), name="Region-shortfall")
        wastage = m.add_variables(lower=0, upper=grid(region_param("wastage_capacity")), name="Region-wastage")

        lhs = shortfall - wastage

        for expression in net.injections():

            lhs = lhs + expression

        m.add_constraints(lhs == net.fixed_demand(), name="Region-balance")

        # Step-down transformer limit: cap NET IMPORT over lines, not total
        # supply. Applied to net import so a region that exports is never
        # bound -- a generation switchyard has no load behind a bank -- and so
        # that a substation's own generation is not charged against the bank it
        # does not pass through.
        #
        # Without this the model moves power from transmission straight into
        # local load with no transformer in between, and can only ever find
        # congestion on lines. Measured on the CA substation layer: relaxing
        # corridors alone leaves 30.1 GWh of shortfall over four weeks while
        # relaxing corridors and transformers together leaves 4.2 GWh, so a
        # third of undelivered energy is transformer-bound and invisible
        # without it.
        # Read straight off the params rather than through region_param, which
        # falls back to a Network-level default when a value is None. The slack
        # settings have such defaults; a transformer rating is a per-region
        # physical property with no sensible network-wide value, and going
        # through the fallback raises AttributeError on Network.
        by_handle = {o.handle: o.p.transformer_capacity for o in objs}

        capacity = xr.DataArray(
            [float(by_handle[r]) if by_handle.get(r) is not None else np.inf for r in index],
            coords=[index],
        )

        if bool(np.isfinite(capacity.values).any()):

            net_import = None

            for expression in net.imports():

                net_import = expression if net_import is None else net_import + expression

            if net_import is not None:

                # Regions with no rating carry inf, so the constraint stays one
                # vectorised block over every region rather than a subset.
                m.add_constraints(net_import <= grid(capacity), name="Region-transformer")

        dt = net.time_step

        net.add_cost((shortfall * region_param("shortfall_cost")).sum() * dt)
        net.add_cost((wastage * region_param("wastage_cost")).sum() * dt)

    @classmethod
    def solution(cls, net, objs):

        v = net.model.variables
        shortfall = v["Region-shortfall"].solution
        wastage = v["Region-wastage"].solution

        dual = net.model.constraints["Region-balance"].dual
        price = dual / net.time_step if dual is not None else None

        out = {}

        for o in objs:

            result = {
                "shortfall": shortfall.sel(region=o.handle).values.tolist(),
                "wastage": wastage.sel(region=o.handle).values.tolist(),
            }

            if price is not None:

                result["clearing_price"] = price.sel(region=o.handle).values.tolist()

            out[o.handle] = result

        return out
