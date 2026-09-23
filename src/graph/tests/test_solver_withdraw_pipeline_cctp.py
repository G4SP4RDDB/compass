"""Behavioral tests for the withdraw pipeline settling into the Solana vault
via CCTP (graph.node.WalletDeficitNode's single incoming edge, see
Graph._linkWalletPayouts): proves the four routes described for this feature
emerge purely from the solver's own cost optimization over two composable
bridge primitives (Aden internal bridge, CCTP) plus the pre-existing Swap
edges -- no per-route special-casing exists anywhere in the graph-building
code.

Supersedes the older test_solver_wallet_deficit.py (removed): that file
proved WalletDeficitNode's chain/stable fungibility via a flat, no-bridge,
no-swap settlement edge from ANY wallet -- a placeholder Graph._linkWalletPayouts
deliberately replaced with the real CCTP/Aden/Swap routing tested here."""

from typing import cast

import pytest

from connectors.gas import GasFeeService
from graph.edge import Edge, EdgeType
from graph.graph import Graph
from graph.node import NodeType, WalletNode
from graph.solver import graphSolve
from graph.structures.bridges import BridgeProtocol, availableBridgeProtocols
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
    """Lets a test make one chain's SWAP gas strictly cheaper than another's,
    to force the solver into one ordering of swap-vs-Aden-bridge (bridge gas
    stays flat across chains here, isolating the swap-placement decision)."""

    def __init__(self, swapGasByChain: dict[Chain, float]) -> None:
        self._swapGasByChain = swapGasByChain

    def get_gas_cost_usd(self, chain, operation) -> float:
        return self._swapGasByChain.get(chain, 0.01)

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.5


@pytest.fixture(autouse=True)
def _syntheticSwapCosts(monkeypatch):
    """Several scenarios here need a Swap edge to actually be usable (USDT->USDC),
    so unlike test_solver_wallet_deficit.py's no-swap fixture, this installs a
    small deterministic linear slippage model (same trick as
    test_solver_vs_shortest_path.py's test_swap_route_matches_shortest_path)
    instead of disabling swaps outright -- no network call, no real pricing
    data needed, just a stable, cheap cost so the solver's ordering choice
    stays governed by the gas stubs in each test, not by slippage noise."""
    from graph import costing

    SLIPPAGE_RATE = 0.0001

    def _syntheticBreakpoints(edge, alchemyConnector=None):
        capacity = edge.capacity or 0.0
        return [(0.0, 0.0), (capacity, capacity * SLIPPAGE_RATE)]

    monkeypatch.setattr(costing, "computeSwapCostBreakpoints", _syntheticBreakpoints)


def _params() -> TimeWeightParams:
    return TimeWeightParams(lambda_min=0.0, lambda_max=0.0, k=1.0)


def _flowingBridgeEdges(graph: Graph, protocol: BridgeProtocol | None = None) -> list[Edge]:
    return [
        e
        for e in graph.edgeList
        if e.type == EdgeType.Bridge
        and (protocol is None or e.bridgeProtocol == protocol)
        and e.flow
        and e.flow > 1e-9
    ]


def _flowingSwapEdges(graph: Graph) -> list[Edge]:
    return [e for e in graph.edgeList if e.type == EdgeType.Swap and e.flow and e.flow > 1e-9]


def _totalPayout(graph: Graph) -> float:
    return sum(e.flow or 0.0 for e in graph.edgeList if e.v.type == NodeType.WalletDeficit)


class TestCctpRoutes:
    def test_usdc_arbitrum_settles_directly_via_cctp(self):
        """USDC already on Arbitrum: one CCTP hop straight to the Solana
        vault, no swap, no Aden bridge."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.ARBITRUM, Stable.USDC): 20.0},
            walletDeficitUsd=-10.0,
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(10.0)
        assert _flowingSwapEdges(graph) == []
        assert _flowingBridgeEdges(graph, BridgeProtocol.ADEN_INTERNAL) == []
        cctpEdges = _flowingBridgeEdges(graph, BridgeProtocol.CCTP)
        assert len(cctpEdges) == 1
        edge = cctpEdges[0]
        assert (cast(WalletNode, edge.u).chain, cast(WalletNode, edge.u).stable) == (Chain.ARBITRUM, Stable.USDC)
        assert (cast(WalletNode, edge.v).chain, cast(WalletNode, edge.v).stable) == (Chain.SOLANA, Stable.USDC)
        assert edge.flow == pytest.approx(10.0)

    def test_usdt_arbitrum_swaps_then_cctp(self):
        """USDT on Arbitrum: swap to USDC (same chain) then CCTP -- CCTP
        never carries USDT (availableBridgeProtocols is USDC-only)."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.ARBITRUM, Stable.USDT): 20.0},
            walletDeficitUsd=-10.0,
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(10.0)
        swaps = _flowingSwapEdges(graph)
        assert len(swaps) == 1
        assert (cast(WalletNode, swaps[0].u).chain, cast(WalletNode, swaps[0].u).stable) == (Chain.ARBITRUM, Stable.USDT)
        assert (cast(WalletNode, swaps[0].v).chain, cast(WalletNode, swaps[0].v).stable) == (Chain.ARBITRUM, Stable.USDC)
        cctpEdges = _flowingBridgeEdges(graph, BridgeProtocol.CCTP)
        assert len(cctpEdges) == 1
        assert cctpEdges[0].flow == pytest.approx(10.0)

    def test_usdc_bsc_bridges_then_cctp(self):
        """USDC on BSC: Aden bridge to Arbitrum (CCTP has no BSC domain),
        then CCTP to the vault."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.BSC, Stable.USDC): 20.0},
            walletDeficitUsd=-10.0,
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(10.0)
        assert _flowingSwapEdges(graph) == []
        adenEdges = _flowingBridgeEdges(graph, BridgeProtocol.ADEN_INTERNAL)
        assert len(adenEdges) == 1
        assert cast(WalletNode, adenEdges[0].u).chain == Chain.BSC
        assert cast(WalletNode, adenEdges[0].v).chain == Chain.ARBITRUM
        assert cast(WalletNode, adenEdges[0].u).stable == Stable.USDC
        cctpEdges = _flowingBridgeEdges(graph, BridgeProtocol.CCTP)
        assert len(cctpEdges) == 1
        assert cctpEdges[0].flow == pytest.approx(10.0)

    def test_usdt_bsc_swaps_on_bsc_first_when_that_is_cheaper(self):
        """USDT on BSC, cheapest ordering depends on where the swap is
        cheapest to execute -- here BSC's swap gas is artificially cheap, so
        the solver should swap BEFORE bridging (bridges as USDC)."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_PerChainGas({Chain.BSC: 0.001, Chain.ARBITRUM: 5.0}),
            walletBalances={(Chain.BSC, Stable.USDT): 20.0},
            walletDeficitUsd=-10.0,
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(10.0)
        swaps = _flowingSwapEdges(graph)
        assert len(swaps) == 1
        assert cast(WalletNode, swaps[0].u).chain == Chain.BSC
        adenEdges = _flowingBridgeEdges(graph, BridgeProtocol.ADEN_INTERNAL)
        assert len(adenEdges) == 1
        assert cast(WalletNode, adenEdges[0].u).stable == Stable.USDC  # already swapped before bridging

    def test_usdt_bsc_swaps_on_arbitrum_first_when_that_is_cheaper(self):
        """Same scenario, opposite cost skew: Arbitrum's swap gas is cheap
        instead, so the solver should bridge first (as USDT) and swap after
        landing on Arbitrum -- proving this is a real solver choice, not a
        hardcoded ordering."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_PerChainGas({Chain.BSC: 5.0, Chain.ARBITRUM: 0.001}),
            walletBalances={(Chain.BSC, Stable.USDT): 20.0},
            walletDeficitUsd=-10.0,
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(10.0)
        swaps = _flowingSwapEdges(graph)
        assert len(swaps) == 1
        assert cast(WalletNode, swaps[0].u).chain == Chain.ARBITRUM
        adenEdges = _flowingBridgeEdges(graph, BridgeProtocol.ADEN_INTERNAL)
        assert len(adenEdges) == 1
        assert cast(WalletNode, adenEdges[0].u).stable == Stable.USDT  # bridged before swapping

    def test_no_wallet_deficit_is_a_pure_transit_wallet_deficit_node(self):
        """Default (walletDeficitUsd=0.0): the single WalletDeficitNode still
        exists (see Graph._addWalletDeficitNodes) but demands nothing,
        exactly like a DEX with no imbalance entry -- no route is forced."""
        graph = Graph([], swapList=[], gasFeeService=_FlatGas(), walletBalances={(Chain.ARBITRUM, Stable.USDC): 5.0})
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(0.0)

    def test_journey_decomposition_follows_the_full_bridge_chain_to_the_vault(self):
        """decomposeJourneys must terminate a journey at the WalletDeficitNode
        through the REAL multi-hop route (Aden bridge then CCTP), not just a
        single flat edge -- see visualization/journeys.py _extractOneJourney."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.BSC, Stable.USDC): 10.0},
            walletDeficitUsd=-4.0,
        )
        graphSolve(graph, _params())

        journeys = decomposeJourneys(graph)

        assert len(journeys) == 1
        j = journeys[0]
        assert j.fromWallet is True
        assert j.fromDex == "Wallet BSC/USDC"
        assert j.toDex == "User payouts"
        assert j.amount == pytest.approx(4.0)
        # Aden bridge (BSC->Arbitrum), then CCTP (Arbitrum->Solana), then the
        # final settlement hop into WalletDeficitNode (see _linkWalletPayouts).
        assert len(j.hops) == 3
        assert (j.hops[0].bridgeProtocol, cast(WalletNode, j.hops[0].u).chain, cast(WalletNode, j.hops[0].v).chain) == (
            BridgeProtocol.ADEN_INTERNAL,
            Chain.BSC,
            Chain.ARBITRUM,
        )
        assert (j.hops[1].bridgeProtocol, cast(WalletNode, j.hops[1].u).chain, cast(WalletNode, j.hops[1].v).chain) == (
            BridgeProtocol.CCTP,
            Chain.ARBITRUM,
            Chain.SOLANA,
        )
        assert j.hops[2].v.type == NodeType.WalletDeficit


class TestCctpScopeRegression:
    def test_cctp_never_offered_on_bsc_routes(self):
        assert BridgeProtocol.CCTP not in availableBridgeProtocols(Chain.BSC, Chain.ARBITRUM, Stable.USDC)
        assert BridgeProtocol.CCTP not in availableBridgeProtocols(Chain.ARBITRUM, Chain.BSC, Stable.USDC)

    def test_cctp_never_offered_for_usdt(self):
        assert availableBridgeProtocols(Chain.ARBITRUM, Chain.SOLANA, Stable.USDT) == []

    def test_aden_still_the_only_protocol_between_bsc_and_arbitrum(self):
        assert availableBridgeProtocols(Chain.BSC, Chain.ARBITRUM, Stable.USDC) == [BridgeProtocol.ADEN_INTERNAL]
