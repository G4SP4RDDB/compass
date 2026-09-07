"""Builds the SAME solved graph the frontend renders (main.buildAndSolveGraph
— same solver, same demo-imbalance seed by default) and reduces its solver-
chosen journeys (visualization/journeys.decomposeJourneys — the exact
decomposition operations.txt and the 'Chosen Operations' tab already use)
down to the Withdraw/Deposit hops compass_test can actually execute.

A journey that touches a Swap or Bridge hop is marked out of scope WHOLESALE
(never partially executed) — v1 only instruments Withdraw/Deposit (see
README.md), and silently dropping the swap/bridge leg of a journey while
still running its withdraw+deposit would test something the solver never
actually chose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# `src` is put on sys.path by compass_test/__init__.py (runs before this
# module, as for any package submodule) — these imports rely on that.
from connectors.dex_operational_params import apply_dex_operational_params, load_dex_operational_params
from connectors.gas import GasFeeService
from graph import costing
from graph.edge import Edge, EdgeType
from graph.graph import Graph
from graph.node import NodeType, SourceNode, WalletNode, WithdrawNode
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.dex_registry import buildDexRegistry
from main import buildAndSolveGraph
from visualization.journeys import decomposeJourneys

from .models import HopType, PlannedHop
from .runners.registry import is_supported


@dataclass
class PlannedJourney:
    fromDex: str
    toDex: str
    stable: str
    hops: list[PlannedHop] = field(default_factory=list)
    inScope: bool = True
    outOfScopeReason: str = ""


def build_solved_graph() -> Graph:
    graph, _dexRegistry, _timeWeightParams = buildAndSolveGraph()
    return graph


def _hop_type(edge: Edge) -> HopType | None:
    if edge.u.type == NodeType.Withdraw:
        return HopType.WITHDRAW
    if edge.u.type == NodeType.Wallet and edge.v.type == NodeType.SourceNode:
        return HopType.DEPOSIT
    return None


def _planned_hop_from_edge(edge: Edge) -> PlannedHop:
    hopType = _hop_type(edge)
    assert hopType is not None
    dex = edge.u.dex if hopType == HopType.WITHDRAW else edge.v.dex
    # Withdraw: edge.u (WithdrawNode) has no .chain (its scope is dex+stable
    # only, see graph.node.WithdrawNode) — the chain actually used is the
    # destination WalletNode's, mirroring costing.computeCost's own
    # convention for this exact edge shape.
    chain = edge.v.chain if hopType == HopType.WITHDRAW else edge.u.chain
    return PlannedHop(
        hopType=hopType,
        dex=dex.name,
        chain=chain.name,
        stable=edge.u.stable.name,
        estimatedCostUsd=edge.cost or 0.0,
        estimatedTimeSeconds=edge.time or 0.0,
        solvedFlowUsd=edge.flow or 0.0,
        minWithdrawUsd=dex.minWithdrawUsdByChain[chain] if hopType == HopType.WITHDRAW else 0.0,
        minDepositUsd=dex.minDepositUsdByChain[chain] if hopType == HopType.DEPOSIT else 0.0,
    )


def list_planned_journeys(graph: Graph) -> list[PlannedJourney]:
    planned: list[PlannedJourney] = []
    for journey in decomposeJourneys(graph):
        outOfScopeEdges = [e for e in journey.hops if e.type in (EdgeType.Swap, EdgeType.Bridge)]
        if outOfScopeEdges:
            kinds = sorted({e.type.name for e in outOfScopeEdges})
            planned.append(
                PlannedJourney(
                    fromDex=journey.fromDex,
                    toDex=journey.toDex,
                    stable=journey.stable,
                    inScope=False,
                    outOfScopeReason=f"journey includes a {'/'.join(kinds)} hop — v1 only instruments Withdraw/Deposit",
                )
            )
            continue

        hops = [_planned_hop_from_edge(e) for e in journey.hops if _hop_type(e) is not None]
        if not hops:
            continue  # every edge on this journey is an internal zero-cost/zero-time hop, nothing to test

        unsupportedDexes = sorted({h.dex for h in hops if not is_supported(h.dex)})
        planned.append(
            PlannedJourney(
                fromDex=journey.fromDex,
                toDex=journey.toDex,
                stable=journey.stable,
                hops=hops,
                inScope=not unsupportedDexes,
                outOfScopeReason=(
                    f"no connector yet for: {', '.join(unsupportedDexes)}" if unsupportedDexes else ""
                ),
            )
        )
    return planned


def load_configured_dex_registry() -> dict[str, DEX]:
    """The real DEX registry (src/graph/structures/dex_registry.py) with real
    operational params applied (Config tab / dex_operational_params.json) —
    NO demo imbalances, no graph build, no solve. Used by build_hop_estimate
    below to test one DEX/chain/stable directly, independent of whatever
    journey the solver's demo-imbalance seed happens to produce (today, only
    Aster -> MEXC — see list_planned_journeys)."""
    registry = buildDexRegistry()
    apply_dex_operational_params(list(registry.values()), load_dex_operational_params())
    return registry


def build_hop_estimate(
    dex: DEX, hopType: HopType, chain: Chain, stable: Stable, gasFeeService: GasFeeService | None = None
) -> PlannedHop:
    """Estimate for one hop OUTSIDE any journey — reuses costing.computeCost/
    computeDelay directly (via a throwaway one-edge graph, never
    reimplementing the formula) so this estimate is guaranteed consistent
    with what the same DEX/chain/stable would show inside a real solved
    graph. Withdraw: WithdrawNode -> WalletNode, mirrors
    Graph._linkWithdrawalsAndDeposits exactly (balance=0.0 — capacity/flow
    are irrelevant here, only cost/time are read). Deposit: WalletNode ->
    SourceNode, the direct-deposit shape (DEX.requiresDepositAddress=False,
    every DEX in the registry today)."""
    gasFeeService = gasFeeService or GasFeeService()
    if hopType == HopType.WITHDRAW:
        withdrawNode = WithdrawNode(stable, nodeIndex=0, dex=dex, balance=0.0)
        walletNode = WalletNode(chain, stable, nodeIndex=1)
        edge = Edge(withdrawNode, walletNode)
        return PlannedHop(
            hopType=hopType,
            dex=dex.name,
            chain=chain.name,
            stable=stable.name,
            estimatedCostUsd=costing.computeCost(edge, gasFeeService),
            estimatedTimeSeconds=costing.computeDelay(edge),
            minWithdrawUsd=dex.minWithdrawUsdByChain[chain],
        )

    walletNode = WalletNode(chain, stable, nodeIndex=0)
    sourceNode = SourceNode(balance=0.0, nodeIndex=1, dex=dex)
    edge = Edge(walletNode, sourceNode)
    return PlannedHop(
        hopType=hopType,
        dex=dex.name,
        chain=chain.name,
        stable=stable.name,
        estimatedCostUsd=costing.computeCost(edge, gasFeeService),
        estimatedTimeSeconds=costing.computeDelay(edge),
        minDepositUsd=dex.minDepositUsdByChain[chain],
    )
