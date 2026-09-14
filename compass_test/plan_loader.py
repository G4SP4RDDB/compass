"""Builds the SAME solved graph the frontend renders (main.buildAndSolveGraph
— same solver, same hand-set imbalances from connectors/dex_imbalances.json)
and reduces its solver-
chosen journeys (visualization/journeys.decomposeJourneys — the exact
decomposition operations.txt and the 'Chosen Operations' tab already use)
down to the Withdraw/Deposit hops compass_test can actually execute.

A journey that touches a Bridge hop is marked out of scope WHOLESALE (never
partially executed) — Bridge isn't instrumented (see README.md), and
silently dropping the bridge leg of a journey while still running its
withdraw+deposit would test something the solver never actually chose.
Swap hops (same-chain USDC <-> USDT) ARE instrumented, through CoW Swap
(runners/cowswap.py), on BSC and Arbitrum only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# `src` is put on sys.path by compass_test/__init__.py (runs before this
# module, as for any package submodule) — these imports rely on that.
from connectors.cowswap import COWSWAP_VENUE_NAME
from connectors.dex_operational_params import apply_all_dex_params
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
from .runners.cowswap import CowSwapRunner
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
    if edge.type == EdgeType.Swap:
        return HopType.SWAP
    return None


def _planned_hop_from_edge(edge: Edge) -> PlannedHop:
    hopType = _hop_type(edge)
    assert hopType is not None
    if hopType == HopType.SWAP:
        walletIn, walletOut = edge.u, edge.v
        # The solver's own all-in estimate for this exact edge: the fixed
        # gas Fee(e) (edge.cost) PLUS the slippage it quoted at the chosen
        # amount (edge.realizedSlippageUsd, see solver.graphSolve) — the
        # same sum the UI shows (web_view._edgeCost).
        return PlannedHop(
            hopType=hopType,
            dex=COWSWAP_VENUE_NAME,
            chain=walletIn.chain.name,
            stable=walletIn.stable.name,
            toStable=walletOut.stable.name,
            estimatedCostUsd=(edge.cost or 0.0) + (edge.realizedSlippageUsd or 0.0),
            estimatedTimeSeconds=edge.time or 0.0,
            configuredTimeSeconds=costing.computeConfiguredDelay(edge),
            timeSource=costing.delaySource(edge),
            solvedFlowUsd=edge.flow or 0.0,
        )
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
        configuredTimeSeconds=costing.computeConfiguredDelay(edge),
        timeSource=costing.delaySource(edge),
        solvedFlowUsd=edge.flow or 0.0,
        minWithdrawUsd=dex.minWithdrawUsdByChain[chain] if hopType == HopType.WITHDRAW else 0.0,
        minDepositUsd=dex.minDepositUsdByChain[chain] if hopType == HopType.DEPOSIT else 0.0,
    )


def list_planned_journeys(graph: Graph) -> list[PlannedJourney]:
    planned: list[PlannedJourney] = []
    for journey in decomposeJourneys(graph):
        if any(e.type == EdgeType.Bridge for e in journey.hops):
            planned.append(
                PlannedJourney(
                    fromDex=journey.fromDex,
                    toDex=journey.toDex,
                    stable=journey.stable,
                    inScope=False,
                    outOfScopeReason="journey includes a Bridge hop — only Withdraw/Swap/Deposit are instrumented",
                )
            )
            continue

        hops = [_planned_hop_from_edge(e) for e in journey.hops if _hop_type(e) is not None]
        if not hops:
            continue  # every edge on this journey is an internal zero-cost/zero-time hop, nothing to test

        reasons: list[str] = []
        unsupportedDexes = sorted({h.dex for h in hops if h.hopType != HopType.SWAP and not is_supported(h.dex)})
        if unsupportedDexes:
            reasons.append(f"no connector yet for: {', '.join(unsupportedDexes)}")
        unsupportedSwaps = sorted(
            {
                f"{h.stable}->{h.toStable} on {h.chain}"
                for h in hops
                if h.hopType == HopType.SWAP
                and not CowSwapRunner.supports(Chain[h.chain], Stable[h.stable], Stable[h.toStable])
            }
        )
        if unsupportedSwaps:
            reasons.append(f"swap not instrumented ({COWSWAP_VENUE_NAME} is wired for BSC/Arbitrum only): {', '.join(unsupportedSwaps)}")
        planned.append(
            PlannedJourney(
                fromDex=journey.fromDex,
                toDex=journey.toDex,
                stable=journey.stable,
                hops=hops,
                inScope=not reasons,
                outOfScopeReason="; ".join(reasons),
            )
        )
    return planned


def load_configured_dex_registry() -> dict[str, DEX]:
    """The real DEX registry (src/graph/structures/dex_registry.py) with real
    operational params applied (Config tab / dex_operational_params.json) —
    NO imbalances, no graph build, no solve. Used by build_hop_estimate
    below to test one DEX/chain/stable directly, independent of whatever
    journey the hand-set imbalances happen to produce (today, only
    Aster -> MEXC — see list_planned_journeys)."""
    registry = buildDexRegistry()
    apply_all_dex_params(list(registry.values()))
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
            configuredTimeSeconds=costing.computeConfiguredDelay(edge),
            timeSource=costing.delaySource(edge),
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
        configuredTimeSeconds=costing.computeConfiguredDelay(edge),
        timeSource=costing.delaySource(edge),
        minDepositUsd=dex.minDepositUsdByChain[chain],
    )


def build_swap_hop_estimate(
    chain: Chain, stableIn: Stable, stableOut: Stable, amount_usd: float, gasFeeService: GasFeeService | None = None
) -> PlannedHop:
    """Swap counterpart of build_hop_estimate: the solver's own estimate for
    a WalletNode(chain, stableIn) -> WalletNode(chain, stableOut) edge
    (Graph._linkSwaps shape) at exactly `amount_usd` — Fee(e) from
    costing.computeCost (the fixed swap gas) plus the slippage
    costing.computeRealizedSwapSlippageUsd quotes (Uniswap QuoterV2) at
    that amount, i.e. what the solver would have charged had it picked this
    edge for that flow. Compared against what CoW actually charges."""
    gasFeeService = gasFeeService or GasFeeService()
    edge = Edge(WalletNode(chain, stableIn, nodeIndex=0), WalletNode(chain, stableOut, nodeIndex=1), type=EdgeType.Swap)
    edge.flow = amount_usd
    edge.realizedSlippageUsd = costing.computeRealizedSwapSlippageUsd(edge)
    return PlannedHop(
        hopType=HopType.SWAP,
        dex=COWSWAP_VENUE_NAME,
        chain=chain.name,
        stable=stableIn.name,
        toStable=stableOut.name,
        estimatedCostUsd=costing.computeCost(edge, gasFeeService) + edge.realizedSlippageUsd,
        estimatedTimeSeconds=costing.computeDelay(edge),
        configuredTimeSeconds=costing.computeConfiguredDelay(edge),
        timeSource=costing.delaySource(edge),
        solvedFlowUsd=amount_usd,
    )
