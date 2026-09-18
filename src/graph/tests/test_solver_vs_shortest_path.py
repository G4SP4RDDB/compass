"""Cross-checks the solver's route/cost choice against an INDEPENDENT
shortest-path implementation (networkx, already a dependency — see
graph_test_helpers.toWeightedMultiDiGraph/timeWeightedEdgeWeight) for the
UNCONTESTED, single-commodity case: exactly one deficit DEX, enough surplus
capacity that nothing competes for a shared resource.

Key subtlety baked into every scenario here: the solver never minimizes raw
edge.cost or edge.time alone — its actual per-edge-per-commodity objective is
Fee(e) + λ(σ_d)·Time(e) (solver.py:32, standalone as
costing.computeTimeWeightedCost). A fair independent check must use that same
weight, not two separate "cheapest"/"fastest" criteria the way
visualization/web_view.py's existing hand-rolled Dijkstra does for the UI's
hover info — that would NOT agree with the solver whenever fee and time trade
off against each other. graph_test_helpers.timeWeightedEdgeWeight is that
weight, reused by nx.dijkstra_path_length/nx.dijkstra_path here.

Also note: Fee(e)/λ(σ_d)·Time(e) are FIXED per-edge(-commodity) charges, paid
once if the edge carries any positive flow for that commodity — NOT
per-dollar (see solver.buildModel's `used`/`usedTime_{i}_{d}` boolean gates).
So a path's total cost is simply the SUM of computeTimeWeightedCost(...) over
its edges, exactly what nx.dijkstra_path_length already computes — no
amount-scaling needed, except for swap slippage (see the last scenario),
which genuinely IS amount-dependent and handled via `extraCostByEdge`.

Contention between multiple deficit commodities sharing one resource is
explicitly OUT OF SCOPE here — see test_solver_multicommodity_contested_resources.py.
"""

from __future__ import annotations

import pytest

from graph import costing
from graph.edge import Edge, EdgeType
from graph.graph import Graph
from graph.solver import graphSolve, totalCostUsd
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.positions import Position
from graph.urgency import TimeWeightParams, computeDexUrgencySigma

from graph_test_helpers import (
    depositEdge,
    shortestWeightedCost,
    shortestWeightedPath,
    sourceNodeIndex,
    timeWeightedEdgeWeight,
    toWeightedMultiDiGraph,
    withdrawEdge,
    withdrawNodeIndex,
    withdrawnAmount,
    zeroGasFeeService,
)


def _params() -> TimeWeightParams:
    return TimeWeightParams(lambda_min=0.01, lambda_max=5.0, k=2.0, epsilon=0.1)


def test_direct_two_dex_rebalance_matches_shortest_path():
    """The simplest case, the user's own starting proposal: one surplus DEX,
    one deficit DEX, one possible path exists. Mainly a sanity anchor for
    the harness itself (nx and the solver trivially agree here) before the
    harder scenarios below."""
    surplus = DEX([Chain.ETHEREUM], [Stable.USDC], name="surplus")
    surplus.withdrawBalances = {Stable.USDC: 100.0}

    deficit = DEX([Chain.ETHEREUM], [Stable.USDC], name="deficit")
    deficit.inbalance = -30.0

    graph = Graph([surplus, deficit], swapList=[], gasFeeService=zeroGasFeeService())
    withdraw = withdrawEdge(graph, surplus, Stable.USDC, Chain.ETHEREUM)
    withdraw.cost, withdraw.time = 0.5, 120.0
    deposit = depositEdge(graph, deficit, Chain.ETHEREUM)
    deposit.cost, deposit.time = 0.1, 30.0

    params = _params()
    solver = graphSolve(graph, params)

    nxGraph = toWeightedMultiDiGraph(graph)
    weight = timeWeightedEdgeWeight(computeDexUrgencySigma(deficit), params)
    expected = shortestWeightedCost(
        nxGraph, [withdrawNodeIndex(graph, surplus, Stable.USDC)], sourceNodeIndex(graph, deficit), weight
    )

    assert totalCostUsd(solver) == pytest.approx(expected, rel=1e-6)


def _buildTwoRouteGraph() -> tuple[Graph, DEX, DEX]:
    deficit = DEX([Chain.ETHEREUM, Chain.ARBITRUM], [Stable.USDC], name="deficit")
    deficit.inbalance = -50.0
    surplus = DEX([Chain.ETHEREUM, Chain.ARBITRUM], [Stable.USDC], name="surplus")
    surplus.withdrawBalances = {Stable.USDC: 50.0}

    graph = Graph([deficit, surplus], swapList=[], gasFeeService=zeroGasFeeService())

    slowCheap = depositEdge(graph, deficit, Chain.ETHEREUM)
    fastExpensive = depositEdge(graph, deficit, Chain.ARBITRUM)
    slowCheap.cost, slowCheap.time = 1.0, 100.0
    fastExpensive.cost, fastExpensive.time = 20.0, 1.0

    for chain in surplus.chains:
        edge = withdrawEdge(graph, surplus, Stable.USDC, chain)
        edge.cost, edge.time = 0.0, 0.0

    return graph, deficit, surplus


@pytest.mark.parametrize("sigma", [1000.0, 0.001])
def test_urgency_flips_choice_between_cheap_slow_and_fast_expensive_route(sigma):
    """Two parallel routes to the same deficit DEX, one cheap+slow, one
    expensive+fast (same shape as test_solver_time_weight.py's
    TestUrgencyFlipsRoutingChoice) — verified here against the independent
    nx path instead of a hardcoded formula, for both a normal and an urgent
    sigma. This is the direct answer to "run a Dijkstra on the side and
    compare"."""
    graph, deficit, surplus = _buildTwoRouteGraph()
    deficit.positions = [Position(sigma=sigma)]
    params = _params()

    solver = graphSolve(graph, params)

    nxGraph = toWeightedMultiDiGraph(graph)
    weight = timeWeightedEdgeWeight(computeDexUrgencySigma(deficit), params)
    sourceIndices = [withdrawNodeIndex(graph, surplus, Stable.USDC)]
    targetIndex = sourceNodeIndex(graph, deficit)

    expectedCost = shortestWeightedCost(nxGraph, sourceIndices, targetIndex, weight)
    assert totalCostUsd(solver) == pytest.approx(expectedCost, rel=1e-6)

    expectedPath = shortestWeightedPath(nxGraph, sourceIndices, targetIndex, weight)
    slowCheap = depositEdge(graph, deficit, Chain.ETHEREUM)
    fastExpensive = depositEdge(graph, deficit, Chain.ARBITRUM)
    usedEdge = slowCheap if slowCheap.u.nodeIndex in expectedPath else fastExpensive
    unusedEdge = fastExpensive if usedEdge is slowCheap else slowCheap
    assert usedEdge.flow == pytest.approx(50.0)
    assert unusedEdge.flow == pytest.approx(0.0)


def _buildThreeSurplusGraph() -> tuple[Graph, DEX, DEX, DEX, DEX]:
    deficit = DEX([Chain.ETHEREUM], [Stable.USDC], name="deficit")
    deficit.inbalance = -20.0

    cheapSlow = DEX([Chain.ETHEREUM], [Stable.USDC], name="cheapSlow")
    cheapSlow.withdrawBalances = {Stable.USDC: 100.0}
    midMid = DEX([Chain.ETHEREUM], [Stable.USDC], name="midMid")
    midMid.withdrawBalances = {Stable.USDC: 100.0}
    fastExpensive = DEX([Chain.ETHEREUM], [Stable.USDC], name="fastExpensive")
    fastExpensive.withdrawBalances = {Stable.USDC: 100.0}

    graph = Graph([deficit, cheapSlow, midMid, fastExpensive], swapList=[], gasFeeService=zeroGasFeeService())

    withdrawEdge(graph, cheapSlow, Stable.USDC, Chain.ETHEREUM).cost = 0.5
    withdrawEdge(graph, cheapSlow, Stable.USDC, Chain.ETHEREUM).time = 500.0
    withdrawEdge(graph, midMid, Stable.USDC, Chain.ETHEREUM).cost = 2.0
    withdrawEdge(graph, midMid, Stable.USDC, Chain.ETHEREUM).time = 100.0
    withdrawEdge(graph, fastExpensive, Stable.USDC, Chain.ETHEREUM).cost = 10.0
    withdrawEdge(graph, fastExpensive, Stable.USDC, Chain.ETHEREUM).time = 5.0

    deposit = depositEdge(graph, deficit, Chain.ETHEREUM)
    deposit.cost, deposit.time = 0.0, 0.0

    return graph, deficit, cheapSlow, midMid, fastExpensive


@pytest.mark.parametrize("sigma", [1000.0, 0.001])
def test_source_choice_among_three_uncontested_surpluses(sigma):
    """One deficit DEX, three surplus DEXes each independently able to cover
    the full deficit alone (no contention between them) — validates
    urgency-driven SOURCE selection, not just route selection: which single
    WithdrawNode the solver drains should match nx's argmin over all three,
    under both a normal and an urgent sigma."""
    graph, deficit, cheapSlow, midMid, fastExpensive = _buildThreeSurplusGraph()
    deficit.positions = [Position(sigma=sigma)]
    params = _params()

    solver = graphSolve(graph, params)

    surpluses = (cheapSlow, midMid, fastExpensive)
    nxGraph = toWeightedMultiDiGraph(graph)
    weight = timeWeightedEdgeWeight(computeDexUrgencySigma(deficit), params)
    sourceIndices = [withdrawNodeIndex(graph, dex, Stable.USDC) for dex in surpluses]
    targetIndex = sourceNodeIndex(graph, deficit)

    expectedCost = shortestWeightedCost(nxGraph, sourceIndices, targetIndex, weight)
    assert totalCostUsd(solver) == pytest.approx(expectedCost, rel=1e-6)

    expectedPath = shortestWeightedPath(nxGraph, sourceIndices, targetIndex, weight)
    chosenIndex = expectedPath[0]
    chosenDex = next(dex for dex in surpluses if withdrawNodeIndex(graph, dex, Stable.USDC) == chosenIndex)

    assert withdrawnAmount(graph, chosenDex) == pytest.approx(20.0)
    for other in surpluses:
        if other is not chosenDex:
            assert withdrawnAmount(graph, other) == pytest.approx(0.0)


def _buildBridgeVsDirectGraph(bridgeShouldWin: bool) -> tuple[Graph, DEX, DEX, DEX, Edge]:
    deficit = DEX([Chain.ARBITRUM], [Stable.USDT], name="deficit")
    deficit.inbalance = -20.0

    directSurplus = DEX([Chain.ARBITRUM], [Stable.USDT], name="directSurplus")
    directSurplus.withdrawBalances = {Stable.USDT: 100.0}

    bridgedSurplus = DEX([Chain.BSC], [Stable.USDT], name="bridgedSurplus")
    bridgedSurplus.withdrawBalances = {Stable.USDT: 100.0}

    graph = Graph([deficit, directSurplus, bridgedSurplus], swapList=[], gasFeeService=zeroGasFeeService())

    directWithdraw = withdrawEdge(graph, directSurplus, Stable.USDT, Chain.ARBITRUM)
    directWithdraw.cost, directWithdraw.time = 1.0, 10.0
    bscWithdraw = withdrawEdge(graph, bridgedSurplus, Stable.USDT, Chain.BSC)
    bscWithdraw.cost, bscWithdraw.time = 0.0, 0.0

    deposit = depositEdge(graph, deficit, Chain.ARBITRUM)
    deposit.cost, deposit.time = 0.0, 0.0

    bridgeEdge = next(
        e
        for e in graph.edgeList
        if e.type == EdgeType.Bridge and e.u.chain == Chain.BSC and e.v.chain == Chain.ARBITRUM and e.u.stable == Stable.USDT
    )

    # graph.structures.bridges is the ONLY modeled bridge route (BSC<->ARBITRUM,
    # ADEN_INTERNAL) — no other protocol/pair exists to pick between here.
    if bridgeShouldWin:
        bridgeEdge.cost, bridgeEdge.time = 0.5, 10.0
    else:
        bridgeEdge.cost, bridgeEdge.time = 50.0, 10.0

    return graph, deficit, directSurplus, bridgedSurplus, bridgeEdge


@pytest.mark.parametrize("bridgeShouldWin", [True, False])
def test_bridge_route_vs_direct_route_matches_shortest_path(bridgeShouldWin):
    """One surplus directly on the deficit's own chain (Arbitrum), one that
    must cross the sole modeled bridge (ADEN_INTERNAL, BSC<->Arbitrum) —
    parametrized so one sub-case prefers the direct route and one prefers
    bridging; checked against nx on the full
    Withdraw->Wallet->Bridge->Wallet->SourceNode chain."""
    graph, deficit, directSurplus, bridgedSurplus, bridgeEdge = _buildBridgeVsDirectGraph(bridgeShouldWin)
    params = _params()

    solver = graphSolve(graph, params)

    nxGraph = toWeightedMultiDiGraph(graph)
    weight = timeWeightedEdgeWeight(computeDexUrgencySigma(deficit), params)
    sourceIndices = [
        withdrawNodeIndex(graph, directSurplus, Stable.USDT),
        withdrawNodeIndex(graph, bridgedSurplus, Stable.USDT),
    ]
    targetIndex = sourceNodeIndex(graph, deficit)

    expectedCost = shortestWeightedCost(nxGraph, sourceIndices, targetIndex, weight)
    assert totalCostUsd(solver) == pytest.approx(expectedCost, rel=1e-6)

    if bridgeShouldWin:
        assert bridgeEdge.flow == pytest.approx(20.0)
        assert withdrawnAmount(graph, directSurplus) == pytest.approx(0.0)
    else:
        assert bridgeEdge.flow == pytest.approx(0.0)
        assert withdrawnAmount(graph, directSurplus) == pytest.approx(20.0)


def test_swap_route_matches_shortest_path(monkeypatch):
    """The deficit DEX only accepts USDC; the only surplus sits in USDT on
    the same chain, forcing Withdraw -> Wallet(USDT) -> [Swap] ->
    Wallet(USDC) -> SourceNode.

    Does NOT use the existing ConnectorError-disable monkeypatch (see
    test_solver_wallet_source.py's _noNetworkSwapQuotes) — that forces
    flow==0 on every swap edge via solver._addSwapCost, making this route
    infeasible. Instead installs a small, deterministic, purely synthetic
    LINEAR slippage model (cost = SLIPPAGE_RATE * amount, a straight line
    through the origin, so the exact breakpoint-sampling capacity doesn't
    matter for the expected value below) and feeds the same slippage into
    `extraCostByEdge` on the nx side, since costing.computeTimeWeightedCost
    deliberately never covers swap slippage (only Fee(e) + λ(σ_d)·Time(e))."""
    SLIPPAGE_RATE = 0.01

    def _syntheticBreakpoints(edge, alchemyConnector=None):
        capacity = edge.capacity or 0.0
        return [(0.0, 0.0), (capacity, capacity * SLIPPAGE_RATE)]

    monkeypatch.setattr(costing, "computeSwapCostBreakpoints", _syntheticBreakpoints)

    deficit = DEX([Chain.ETHEREUM], [Stable.USDC], name="deficit")
    deficit.inbalance = -20.0

    surplus = DEX([Chain.ETHEREUM], [Stable.USDT], name="surplus")
    surplus.withdrawBalances = {Stable.USDT: 100.0}

    graph = Graph([deficit, surplus], swapList=[], gasFeeService=zeroGasFeeService())

    withdraw = withdrawEdge(graph, surplus, Stable.USDT, Chain.ETHEREUM)
    withdraw.cost, withdraw.time = 0.0, 0.0

    swapEdge = next(
        e
        for e in graph.edgeList
        if e.type == EdgeType.Swap
        and e.u.chain == Chain.ETHEREUM
        and e.u.stable == Stable.USDT
        and e.v.stable == Stable.USDC
    )
    # edge.cost/edge.time (set at Graph() construction, before this test's
    # monkeypatch existed) only ever cover the swap's fixed gas — never
    # slippage, see costing.computeCost's Swap branch — override to a
    # controlled value like every other edge in this file.
    swapEdge.cost, swapEdge.time = 0.1, 5.0

    deposit = depositEdge(graph, deficit, Chain.ETHEREUM)
    deposit.cost, deposit.time = 0.0, 0.0

    params = _params()
    solver = graphSolve(graph, params)

    demandAmount = 20.0  # the only amount that ever flows on swapEdge here
    slippageAtDemand = SLIPPAGE_RATE * demandAmount  # linear through the origin: exact regardless of edge.capacity

    nxGraph = toWeightedMultiDiGraph(graph)
    weight = timeWeightedEdgeWeight(
        computeDexUrgencySigma(deficit), params, extraCostByEdge={swapEdge: slippageAtDemand}
    )
    expected = shortestWeightedCost(
        nxGraph, [withdrawNodeIndex(graph, surplus, Stable.USDT)], sourceNodeIndex(graph, deficit), weight
    )

    assert totalCostUsd(solver) == pytest.approx(expected, abs=1e-3)
    assert swapEdge.flow == pytest.approx(demandAmount)
