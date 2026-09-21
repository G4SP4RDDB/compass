"""Behavioral tests for graph.node.WalletDeficitNode (money owed to a user
withdrawal, modeled directly on the operating wallet rather than on any
DEX): a deficit that is FUNGIBLE across every chain (see
Graph._linkWalletPayouts), unlike a real wallet surplus (WalletNode.balance),
which stays pinned to one chain and can only reach another via a priced
bridge edge (see graph.node.WalletDeficitNode's docstring: "shared deficit,
non-shared surplus"). Mirrors the style of test_solver_wallet_source.py."""

from typing import cast

import pytest

from connectors.gas import GasFeeService
from graph.edge import EdgeType
from graph.graph import Graph
from graph.node import NodeType, WalletDeficitNode, WalletNode
from graph.solver import graphSolve
from graph.structures.DEXes import Chain, Stable
from graph.urgency import TimeWeightParams
from visualization.journeys import decomposeJourneys


class _FlatGas(GasFeeService):
    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.01

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.5


class _PerChainGas(GasFeeService):
    """Lets a test make one chain's payout edge strictly cheaper than
    another's, to check the solver actually treats the wallet deficit as a
    single fungible commodity choosing its cheapest source chain, not just
    summing whatever is available."""

    def __init__(self, costByChain: dict[Chain, float]) -> None:
        self._costByChain = costByChain

    def get_gas_cost_usd(self, chain, operation) -> float:
        return self._costByChain.get(chain, 0.01)

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.5


@pytest.fixture(autouse=True)
def _noNetworkSwapQuotes(monkeypatch):
    """Same rationale as test_solver_wallet_source.py: no test in this file
    needs a real swap quote except the one that overrides this fixture."""
    from connectors.exceptions import ConnectorError
    from graph import costing

    def _unavailable(edge, alchemyConnector=None):
        raise ConnectorError("swap quotes disabled in tests")

    monkeypatch.setattr(costing, "computeSwapCostBreakpoints", _unavailable)


def _params() -> TimeWeightParams:
    return TimeWeightParams(lambda_min=0.0, lambda_max=0.0, k=1.0)


def _payoutFlow(graph: Graph, chain: Chain, stable: Stable) -> float:
    return sum(
        e.flow or 0.0
        for e in graph.edgeList
        if e.u.type == NodeType.Wallet
        and cast(WalletNode, e.u).chain == chain
        and cast(WalletNode, e.u).stable == stable
        and e.v.type == NodeType.WalletDeficit
    )


def _totalPayout(graph: Graph, stable: Stable) -> float:
    return sum(
        e.flow or 0.0
        for e in graph.edgeList
        if e.v.type == NodeType.WalletDeficit and cast(WalletDeficitNode, e.v).stable == stable
    )


def _bridged(graph: Graph) -> float:
    return sum(e.flow or 0.0 for e in graph.edgeList if e.type == EdgeType.Bridge)


class TestWalletDeficitFungibility:
    def test_deficit_is_split_across_two_chains_with_no_dex_and_no_bridge(self):
        """The core "shared deficit" property: a single WalletDeficitNode can
        be filled by combining balances sitting on DIFFERENT chains directly
        (parallel payout edges into the same sink), with no DEX and no
        bridge involved at all -- the exact opposite of WalletNode.balance,
        which never pools across chains without a priced bridge hop."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.BSC, Stable.USDC): 5.0, (Chain.ARBITRUM, Stable.USDC): 10.0},
            walletDeficits={Stable.USDC: -12.0},
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph, Stable.USDC) == pytest.approx(12.0)
        assert _bridged(graph) == pytest.approx(0.0)
        # Neither chain overdrawn beyond its own real balance.
        assert _payoutFlow(graph, Chain.BSC, Stable.USDC) <= 5.0 + 1e-6
        assert _payoutFlow(graph, Chain.ARBITRUM, Stable.USDC) <= 10.0 + 1e-6

    def test_deficit_prefers_the_cheaper_chain_when_either_alone_would_do(self):
        """Both chains individually hold enough to cover the deficit alone:
        the solver must pick the cheaper one entirely, proving it treats
        "which chain pays the user" as a real, chain-agnostic choice."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_PerChainGas({Chain.BSC: 0.01, Chain.ARBITRUM: 5.0}),
            walletBalances={(Chain.BSC, Stable.USDC): 50.0, (Chain.ARBITRUM, Stable.USDC): 50.0},
            walletDeficits={Stable.USDC: -10.0},
        )
        graphSolve(graph, _params())

        assert _payoutFlow(graph, Chain.BSC, Stable.USDC) == pytest.approx(10.0)
        assert _payoutFlow(graph, Chain.ARBITRUM, Stable.USDC) == pytest.approx(0.0)

    def test_no_wallet_deficit_is_a_pure_transit_wallet_deficit_node(self):
        """Absent from walletDeficits (the default): every WalletDeficitNode
        still exists (one per Stable, see Graph._addWalletDeficitNodes) but
        demands nothing, exactly like a DEX with no imbalance entry."""
        graph = Graph([], swapList=[], gasFeeService=_FlatGas(), walletBalances={(Chain.BSC, Stable.USDC): 5.0})
        graphSolve(graph, _params())

        assert _totalPayout(graph, Stable.USDC) == pytest.approx(0.0)

    def test_journey_decomposition_lands_on_the_wallet_deficit_node(self):
        """decomposeJourneys must terminate a journey at a WalletDeficitNode
        just like it does at a DEX SourceNode (see visualization/journeys.py
        _extractOneJourney) -- before this Node type existed, a journey
        ending here would silently vanish instead of being reported."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.BSC, Stable.USDC): 10.0},
            walletDeficits={Stable.USDC: -4.0},
        )
        graphSolve(graph, _params())

        journeys = decomposeJourneys(graph)

        assert len(journeys) == 1
        j = journeys[0]
        assert j.fromWallet is True
        assert j.fromDex == "Wallet BSC/USDC"
        assert j.toDex == "User payouts (USDC)"
        assert j.amount == pytest.approx(4.0)


class TestWalletDeficitAcrossStableViaSwap:
    def test_deficit_in_usdc_can_be_funded_by_withdrawing_usdt_and_swapping(self, monkeypatch):
        """What the user actually described: payouts must be in USDC, but
        the wallet only holds USDT -- the solver should route
        Wallet(USDT) -> [Swap] -> Wallet(USDC) -> WalletDeficitNode(USDC),
        composed for free from the existing Swap edges (Graph._linkSwaps),
        with no special-casing needed in the new wallet-deficit wiring."""
        from graph import costing

        SLIPPAGE_RATE = 0.01

        def _syntheticBreakpoints(edge, alchemyConnector=None):
            capacity = edge.capacity or 0.0
            return [(0.0, 0.0), (capacity, capacity * SLIPPAGE_RATE)]

        monkeypatch.setattr(costing, "computeSwapCostBreakpoints", _syntheticBreakpoints)

        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.BSC, Stable.USDT): 20.0},
            walletDeficits={Stable.USDC: -8.0},
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph, Stable.USDC) == pytest.approx(8.0)
        swapFlow = sum(
            e.flow or 0.0
            for e in graph.edgeList
            if e.type == EdgeType.Swap
            and cast(WalletNode, e.u).stable == Stable.USDT
            and cast(WalletNode, e.v).stable == Stable.USDC
        )
        assert swapFlow == pytest.approx(8.0)
