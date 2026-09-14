from typing import cast

import pytest

from connectors.gas import GasFeeService
from graph.graph import Graph
from graph.node import NodeType, WithdrawNode
from graph.solver import graphSolve
from graph.structures.DEXes import DEX, Chain, Stable
from graph.urgency import TimeWeightParams


class _ZeroGasFeeService(GasFeeService):
    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.0

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.0


@pytest.fixture(autouse=True)
def _noNetworkSwapQuotes(monkeypatch):
    """Swap edges (WalletNode -> WalletNode, other stable) get priced through
    a live Uniswap QuoterV2 eth_call in solver._addSwapCost — slow and
    network-dependent. Every scenario here is same-stable, so swaps are
    never needed: make the quote raise ConnectorError, which _addSwapCost
    already handles by disabling the edge."""
    from connectors.exceptions import ConnectorError
    from graph import costing

    def _unavailable(edge, alchemyConnector=None):
        raise ConnectorError("swap quotes disabled in tests")

    monkeypatch.setattr(costing, "computeSwapCostBreakpoints", _unavailable)


def _params() -> TimeWeightParams:
    return TimeWeightParams(lambda_min=0.0, lambda_max=0.0, k=1.0)


def _withdrawn(graph: Graph, dex: DEX) -> float:
    return sum(
        e.flow or 0.0
        for e in graph.edgeList
        if e.u.type == NodeType.Withdraw and cast(WithdrawNode, e.u).dex is dex
    )


class TestSurplusIsAnUpperBound:
    def test_only_what_the_deficit_needs_is_withdrawn(self):
        """Surplus $100 against a $30 deficit: the old equality constraint
        made this INFEASIBLE (the two totals had to match); now $30 moves
        and $70 stays put."""
        surplus = DEX([Chain.ARBITRUM], [Stable.USDC], name="S")
        surplus.withdrawBalances = {Stable.USDC: 100.0}
        deficit = DEX([Chain.ARBITRUM], [Stable.USDC], name="D")
        deficit.inbalance = -30.0
        for d in (surplus, deficit):
            d.withdrawFeeUsdByChain[Chain.ARBITRUM] = 1.0
            d.withdrawDelaySecondsByChain[Chain.ARBITRUM] = 0.0
            d.depositDelaySecondsByChain[Chain.ARBITRUM] = 0.0

        graph = Graph([surplus, deficit], swapList=[], gasFeeService=_ZeroGasFeeService())
        graphSolve(graph, _params())

        assert _withdrawn(graph, surplus) == pytest.approx(30.0)

    def test_cheapest_source_is_drained_first(self):
        cheap = DEX([Chain.ARBITRUM], [Stable.USDC], name="Cheap")
        cheap.withdrawBalances = {Stable.USDC: 50.0}
        cheap.withdrawFeeUsdByChain[Chain.ARBITRUM] = 0.1
        pricey = DEX([Chain.ARBITRUM], [Stable.USDC], name="Pricey")
        pricey.withdrawBalances = {Stable.USDC: 50.0}
        pricey.withdrawFeeUsdByChain[Chain.ARBITRUM] = 5.0
        deficit = DEX([Chain.ARBITRUM], [Stable.USDC], name="D")
        deficit.inbalance = -40.0
        for d in (cheap, pricey, deficit):
            d.withdrawDelaySecondsByChain[Chain.ARBITRUM] = 0.0
            d.depositDelaySecondsByChain[Chain.ARBITRUM] = 0.0

        graph = Graph([cheap, pricey, deficit], swapList=[], gasFeeService=_ZeroGasFeeService())
        graphSolve(graph, _params())

        assert _withdrawn(graph, cheap) == pytest.approx(40.0)
        assert _withdrawn(graph, pricey) == pytest.approx(0.0)

    def test_no_deficit_means_empty_plan(self):
        surplus = DEX([Chain.ARBITRUM], [Stable.USDC], name="S")
        surplus.withdrawBalances = {Stable.USDC: 100.0}
        graph = Graph([surplus], swapList=[], gasFeeService=_ZeroGasFeeService())
        graphSolve(graph, _params())
        assert all((e.flow or 0.0) == 0.0 for e in graph.edgeList)
