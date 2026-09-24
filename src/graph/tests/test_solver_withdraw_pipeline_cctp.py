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

    def test_usdc_bsc_swaps_bridges_then_swaps_back_before_cctp(self):
        """USDC on BSC: Aden's internal ledger only carries USDT (see
        availableBridgeProtocols), so the solver must swap to USDT on BSC
        first, bridge to Arbitrum, swap back to USDC there, then CCTP to
        the vault -- a Swap+Bridge+Swap sandwich, not a direct USDC bridge."""
        graph = Graph(
            [],
            swapList=[],
            gasFeeService=_FlatGas(),
            walletBalances={(Chain.BSC, Stable.USDC): 20.0},
            walletDeficitUsd=-10.0,
        )
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(10.0)
        swaps = _flowingSwapEdges(graph)
        assert len(swaps) == 2
        bscSwap = next(s for s in swaps if cast(WalletNode, s.u).chain == Chain.BSC)
        assert cast(WalletNode, bscSwap.u).stable == Stable.USDC
        assert cast(WalletNode, bscSwap.v).stable == Stable.USDT
        arbSwap = next(s for s in swaps if cast(WalletNode, s.u).chain == Chain.ARBITRUM)
        assert cast(WalletNode, arbSwap.u).stable == Stable.USDT
        assert cast(WalletNode, arbSwap.v).stable == Stable.USDC
        adenEdges = _flowingBridgeEdges(graph, BridgeProtocol.ADEN_INTERNAL)
        assert len(adenEdges) == 1
        assert cast(WalletNode, adenEdges[0].u).chain == Chain.BSC
        assert cast(WalletNode, adenEdges[0].v).chain == Chain.ARBITRUM
        assert cast(WalletNode, adenEdges[0].u).stable == Stable.USDT
        cctpEdges = _flowingBridgeEdges(graph, BridgeProtocol.CCTP)
        assert len(cctpEdges) == 1
        assert cctpEdges[0].flow == pytest.approx(10.0)

    def test_usdt_bsc_always_bridges_as_usdt_and_swaps_on_arbitrum(self):
        """USDT on BSC: Aden being USDT-only leaves exactly one viable
        topology -- bridge as USDT, THEN swap to USDC on Arbitrum for CCTP
        -- unlike before this restriction, gas skew between chains can no
        longer move the swap to the BSC side (there is no route left where
        Aden carries USDC), so both cost skews below must produce the same
        route."""
        for gasByChain in [{Chain.BSC: 0.001, Chain.ARBITRUM: 5.0}, {Chain.BSC: 5.0, Chain.ARBITRUM: 0.001}]:
            graph = Graph(
                [],
                swapList=[],
                gasFeeService=_PerChainGas(gasByChain),
                walletBalances={(Chain.BSC, Stable.USDT): 20.0},
                walletDeficitUsd=-10.0,
            )
            graphSolve(graph, _params())

            assert _totalPayout(graph) == pytest.approx(10.0)
            adenEdges = _flowingBridgeEdges(graph, BridgeProtocol.ADEN_INTERNAL)
            assert len(adenEdges) == 1
            assert cast(WalletNode, adenEdges[0].u).stable == Stable.USDT
            swaps = _flowingSwapEdges(graph)
            assert len(swaps) == 1
            assert cast(WalletNode, swaps[0].u).chain == Chain.ARBITRUM
            assert cast(WalletNode, swaps[0].u).stable == Stable.USDT
            assert cast(WalletNode, swaps[0].v).stable == Stable.USDC

    def test_no_wallet_deficit_is_a_pure_transit_wallet_deficit_node(self):
        """Default (walletDeficitUsd=0.0): the single WalletDeficitNode still
        exists (see Graph._addWalletDeficitNodes) but demands nothing,
        exactly like a DEX with no imbalance entry -- no route is forced."""
        graph = Graph([], swapList=[], gasFeeService=_FlatGas(), walletBalances={(Chain.ARBITRUM, Stable.USDC): 5.0})
        graphSolve(graph, _params())

        assert _totalPayout(graph) == pytest.approx(0.0)

    def test_journey_decomposition_follows_the_full_bridge_chain_to_the_vault(self):
        """decomposeJourneys must terminate a journey at the WalletDeficitNode
        through the REAL multi-hop route (Swap, Aden bridge, Swap, CCTP), not
        just a single flat edge -- see visualization/journeys.py
        _extractOneJourney. Also proves _extractOneJourney's maxAmount cap
        (see decomposeJourneys) doesn't fragment this into multiple partial
        journeys: BSC's own balance is the only source here, so one journey
        of the full amount should come out, not several smaller ones."""
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
        # Swap (BSC USDC->USDT), Aden bridge (BSC->Arbitrum), Swap (Arbitrum
        # USDT->USDC), CCTP (Arbitrum->Solana), then the final settlement
        # hop into WalletDeficitNode (see _linkWalletPayouts).
        assert len(j.hops) == 5
        assert (j.hops[0].type, cast(WalletNode, j.hops[0].u).stable, cast(WalletNode, j.hops[0].v).stable) == (
            EdgeType.Swap,
            Stable.USDC,
            Stable.USDT,
        )
        assert (j.hops[1].bridgeProtocol, cast(WalletNode, j.hops[1].u).chain, cast(WalletNode, j.hops[1].v).chain) == (
            BridgeProtocol.ADEN_INTERNAL,
            Chain.BSC,
            Chain.ARBITRUM,
        )
        assert (j.hops[2].type, cast(WalletNode, j.hops[2].u).stable, cast(WalletNode, j.hops[2].v).stable) == (
            EdgeType.Swap,
            Stable.USDT,
            Stable.USDC,
        )
        assert (j.hops[3].bridgeProtocol, cast(WalletNode, j.hops[3].u).chain, cast(WalletNode, j.hops[3].v).chain) == (
            BridgeProtocol.CCTP,
            Chain.ARBITRUM,
            Chain.SOLANA,
        )
        assert j.hops[4].v.type == NodeType.WalletDeficit


class TestCctpScopeRegression:
    def test_cctp_never_offered_on_bsc_routes(self):
        assert BridgeProtocol.CCTP not in availableBridgeProtocols(Chain.BSC, Chain.ARBITRUM, Stable.USDC)
        assert BridgeProtocol.CCTP not in availableBridgeProtocols(Chain.ARBITRUM, Chain.BSC, Stable.USDC)

    def test_cctp_never_offered_for_usdt(self):
        assert availableBridgeProtocols(Chain.ARBITRUM, Chain.SOLANA, Stable.USDT) == []

    def test_aden_still_the_only_protocol_between_bsc_and_arbitrum(self):
        # Aden's internal ledger only carries USDT (see availableBridgeProtocols
        # and compass_test/runners/aden.py::AdenConnector.supported_stables) --
        # a USDC imbalance between these two chains has NO direct bridge edge
        # at all, only Swap+Aden+Swap (see test_usdc_bsc_swaps_bridges_then_
        # swaps_back_before_cctp above).
        assert availableBridgeProtocols(Chain.BSC, Chain.ARBITRUM, Stable.USDT) == [BridgeProtocol.ADEN_INTERNAL]
        assert availableBridgeProtocols(Chain.BSC, Chain.ARBITRUM, Stable.USDC) == []
