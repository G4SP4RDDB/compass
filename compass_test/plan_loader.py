"""Builds the SAME solved graph the frontend renders (main.buildAndSolveGraph
— same solver, same hand-set imbalances from connectors/dex_imbalances.json)
and reduces its solver-
chosen journeys (visualization/journeys.decomposeJourneys — the exact
decomposition operations.txt and the 'Chosen Operations' tab already use)
down to the Withdraw/Deposit hops compass_test can actually execute.

A Bridge hop (cross-chain same-stable move) IS instrumented for both
protocols graph.structures.bridges models: ADEN_INTERNAL, through Aden's own
deposit/withdraw ledger (runners/aden.py), for USDT on BSC<->Arbitrum only —
Aden's supported_chains/supported_stables; and CCTP, through Circle's own
burn/attestation/mint pipeline (connectors/cctp.py, cctp_runner.py), for
USDC on ARBITRUM<->SOLANA only — the one route this project's withdraw
pipeline actually uses (see graph.structures.bridges._CCTP_CHAINS). A
journey whose Bridge leg falls outside either of those (a different stable
on a route ADEN_INTERNAL nominally covers, or any CCTP pair beyond
Arbitrum<->Solana) is marked out of scope with a reason, same pattern as an
unsupported Swap pair below — never silently dropping just that leg while
still running the rest.
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
from graph.structures.bridges import BridgeProtocol, availableBridgeProtocols
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.dex_registry import buildDexRegistry
from main import buildAndSolveGraph
from visualization.journeys import decomposeJourneys

from .models import HopType, PlannedHop
from .runners.aden import AdenConnector
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


# graph.structures.bridges.BridgeProtocol -> the venue whose ledger actually
# implements it. CCTP has no real DEX behind it (see compass_test/cctp_runner.py)
# — "CCTP" here is a protocol sentinel, same role COWSWAP_VENUE_NAME plays
# for a Swap hop's `dex`, not a runners/registry.py entry.
_BRIDGE_DEX_NAME = {BridgeProtocol.ADEN_INTERNAL: "Aden", BridgeProtocol.CCTP: "CCTP"}


def _hop_type(edge: Edge) -> HopType | None:
    if edge.u.type == NodeType.Withdraw:
        return HopType.WITHDRAW
    if edge.u.type == NodeType.Wallet and edge.v.type == NodeType.SourceNode:
        return HopType.DEPOSIT
    if edge.type == EdgeType.Swap:
        return HopType.SWAP
    if edge.type == EdgeType.Bridge:
        return HopType.BRIDGE
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
    if hopType == HopType.BRIDGE:
        walletIn, walletOut = edge.u, edge.v
        # edge.cost/edge.time are already the WHOLE bridge's combined
        # gas+fee / delay (costing.computeCost's EdgeType.Bridge branch,
        # computeBridgeDelay) — a single number, not split per leg, mirroring
        # how the Swap branch above reads edge.cost/edge.time directly rather
        # than re-deriving them.
        return PlannedHop(
            hopType=hopType,
            dex=_BRIDGE_DEX_NAME[edge.bridgeProtocol],
            chain=walletIn.chain.name,
            toChain=walletOut.chain.name,
            stable=walletIn.stable.name,
            estimatedCostUsd=edge.cost or 0.0,
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
        hops = [_planned_hop_from_edge(e) for e in journey.hops if _hop_type(e) is not None]
        if not hops:
            continue  # every edge on this journey is an internal zero-cost/zero-time hop, nothing to test

        reasons: list[str] = []
        unsupportedDexes = sorted(
            {h.dex for h in hops if h.hopType not in (HopType.SWAP, HopType.BRIDGE) and not is_supported(h.dex)}
        )
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
        # availableBridgeProtocols models each protocol as open to any stable
        # it covers (see graph.structures.bridges), but only what each
        # protocol's own connector actually instruments is in scope: Aden's
        # own USDT ledger (AdenConnector.supported_*) for ADEN_INTERNAL, and
        # ARBITRUM<->SOLANA USDC (the only route compass_test/cctp_runner.py
        # implements) for CCTP — a Bridge hop outside either is out of
        # scope, same as an unsupported swap pair above, not a reason to
        # drop the whole journey.
        def _bridge_hop_supported(h: PlannedHop) -> bool:
            if h.dex == "CCTP":
                return Stable[h.stable] == Stable.USDC and Chain[h.chain] == Chain.ARBITRUM and Chain[h.toChain] == Chain.SOLANA
            return (
                Stable[h.stable] in AdenConnector.supported_stables
                and Chain[h.chain] in AdenConnector.supported_chains
                and Chain[h.toChain] in AdenConnector.supported_chains
            )

        unsupportedBridges = sorted(
            {f"{h.stable} {h.chain}->{h.toChain}" for h in hops if h.hopType == HopType.BRIDGE and not _bridge_hop_supported(h)}
        )
        if unsupportedBridges:
            reasons.append(
                f"bridge not instrumented (Aden USDT BSC<->Arbitrum, or CCTP USDC Arbitrum<->Solana, only): "
                f"{', '.join(unsupportedBridges)}"
            )
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


def build_bridge_hop_estimate(
    fromChain: Chain, toChain: Chain, stable: Stable, gasFeeService: GasFeeService | None = None
) -> PlannedHop:
    """Bridge counterpart of build_hop_estimate/build_swap_hop_estimate: the
    solver's own estimate for a WalletNode(fromChain, stable) ->
    WalletNode(toChain, stable) EdgeType.Bridge edge (Graph._linkBridges
    shape) — Fee(e)/Time(e) via costing.computeCost/computeDelay, exactly
    as the solved graph itself would compute them for this edge. Protocol
    picked the same way Graph._linkBridges itself picks it: whatever
    availableBridgeProtocols returns for this (fromChain, toChain, stable) —
    ADEN_INTERNAL for BSC<->ARBITRUM, CCTP for ARBITRUM<->SOLANA USDC (see
    graph.structures.bridges) — never guessed or hardcoded here. Raises if
    neither protocol covers the route (mirrors availableBridgeProtocols
    returning an empty list, i.e. "no bridge edge at all between these two
    chains for this stable")."""
    gasFeeService = gasFeeService or GasFeeService()
    protocols = availableBridgeProtocols(fromChain, toChain, stable)
    if not protocols:
        raise ValueError(f"no bridge protocol covers {stable.name} {fromChain.name}->{toChain.name}")
    # Only one protocol ever covers a given (fromChain, toChain) pair today
    # (ADEN_INTERNAL and CCTP's chain sets don't overlap) — first is fine,
    # not an arbitrary tiebreak.
    protocol = protocols[0]
    edge = Edge(
        WalletNode(fromChain, stable, nodeIndex=0),
        WalletNode(toChain, stable, nodeIndex=1),
        type=EdgeType.Bridge,
        bridgeProtocol=protocol,
    )
    return PlannedHop(
        hopType=HopType.BRIDGE,
        dex=_BRIDGE_DEX_NAME[protocol],
        chain=fromChain.name,
        toChain=toChain.name,
        stable=stable.name,
        estimatedCostUsd=costing.computeCost(edge, gasFeeService),
        estimatedTimeSeconds=costing.computeDelay(edge),
        configuredTimeSeconds=costing.computeConfiguredDelay(edge),
        timeSource=costing.delaySource(edge),
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
