"""Property/invariant tests on randomly-generated, larger graphs — a
regression net that scales beyond the hand/brute-force-verifiable scenarios
in test_solver_vs_shortest_path.py / test_solver_multicommodity_contested_resources.py,
at the cost of not proving exact optimality: these check that no STRUCTURAL
guarantee is ever violated (flow conservation, capacity, deficit
satisfaction) and that the solver is never worse than a naive-but-feasible
heuristic, without needing to hand-compute what the true optimal cost is for
a graph too large to brute force.

Plain seeded randomness (random.Random(seed) + pytest.mark.parametrize), not
the `hypothesis` library — that's not currently a dependency and adding one
just for this is unnecessary; if shrinking/more sophisticated generation is
ever needed, that's a follow-up.
"""

from __future__ import annotations

import random
from typing import cast

import pytest

from graph import costing
from graph.edge import EdgeType
from graph.graph import Graph
from graph.node import NodeType, SourceNode, WalletNode, WithdrawNode
from graph.solver import graphSolve, totalCostUsd
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.positions import Position
from graph.urgency import TimeWeightParams, computeDexUrgencySigma

from graph_test_helpers import (
    shortestWeightedCost,
    shortestWeightedPath,
    timeWeightedEdgeWeight,
    toWeightedMultiDiGraph,
    zeroGasFeeService,
)

# Only BSC/ARBITRUM are bridged to each other (graph.structures.bridges —
# the sole modeled route, matching every real DEX in dex_registry.py today).
# A THIRD chain (e.g. ETHEREUM) would make "total surplus >= total deficit"
# an unsound feasibility check: a deficit stranded on an unbridged chain can
# stay infeasible no matter how much surplus sits elsewhere (found by an
# earlier version of this file failing test_deficit_exactly_met_when_generously_feasible
# for exactly this reason) — restricting to the one bridged pair keeps every
# dollar fungible regardless of which chain it starts on.
_CHAINS = [Chain.ARBITRUM, Chain.BSC]


def _params() -> TimeWeightParams:
    return TimeWeightParams(lambda_min=0.01, lambda_max=5.0, k=2.0, epsilon=0.1)


@pytest.fixture(autouse=True)
def _syntheticSwapCosts(monkeypatch):
    """Small deterministic linear slippage (same trick as
    test_solver_vs_shortest_path.py's swap scenario) so swap edges stay
    exercisable everywhere in these larger random graphs, instead of the
    ConnectorError-disable monkeypatch other existing tests use (which would
    force every swap edge's flow to 0 and silently narrow what's being
    tested)."""

    def _synthetic(edge, alchemyConnector=None):
        capacity = edge.capacity or 0.0
        return [(0.0, 0.0), (capacity, capacity * 0.001)]

    monkeypatch.setattr(costing, "computeSwapCostBreakpoints", _synthetic)


def _randomGraph(
    rng: random.Random,
    numSurplus: int,
    numDeficit: int,
    surplusRange: tuple[float, float],
    deficitRange: tuple[float, float],
) -> tuple[Graph, list[DEX]]:
    dexes: list[DEX] = []
    for i in range(numSurplus):
        chain = rng.choice(_CHAINS)
        stable = rng.choice(list(Stable))
        dex = DEX([chain], [stable], name=f"surplus{i}")
        dex.withdrawBalances = {stable: float(rng.randint(int(surplusRange[0]), int(surplusRange[1])))}
        dex.withdrawFeeUsdByChain[chain] = float(rng.randint(0, 5))
        dex.withdrawDelaySecondsByChain[chain] = float(rng.randint(0, 300))
        dexes.append(dex)
    for i in range(numDeficit):
        chain = rng.choice(_CHAINS)
        stable = rng.choice(list(Stable))
        dex = DEX([chain], [stable], name=f"deficit{i}")
        dex.inbalance = -float(rng.randint(int(deficitRange[0]), int(deficitRange[1])))
        dex.depositFeeUsdByChain[chain] = float(rng.randint(0, 5))
        dex.depositDelaySecondsByChain[chain] = float(rng.randint(0, 300))
        if rng.random() < 0.5:
            dex.positions = [Position(sigma=rng.uniform(0.01, 100.0))]
        dexes.append(dex)

    graph = Graph(dexes, swapList=[], gasFeeService=zeroGasFeeService())
    return graph, dexes


@pytest.mark.parametrize("seed", range(20))
def test_flow_conservation_holds_at_every_node(seed):
    """Independently re-derives, from the SOLVED graph's edge.flow alone,
    the same aggregate rule solver._addFlowConservation encodes per
    commodity — summed over all commodities, that per-commodity rule
    implies exactly this aggregate one. A genuine re-derivation from the
    result, not trust in the solver's own OPTIMAL/FEASIBLE status."""
    rng = random.Random(seed)
    graph, _dexes = _randomGraph(rng, numSurplus=4, numDeficit=3, surplusRange=(50, 150), deficitRange=(5, 40))
    params = _params()
    try:
        graphSolve(graph, params)
    except RuntimeError:
        pytest.skip(f"infeasible graph for seed={seed} — feasibility itself is covered elsewhere")

    inflow: dict[int, float] = {n.nodeIndex: 0.0 for n in graph.nodeList}
    outflow: dict[int, float] = {n.nodeIndex: 0.0 for n in graph.nodeList}
    for edge in graph.edgeList:
        flow = edge.flow or 0.0
        outflow[edge.u.nodeIndex] += flow
        inflow[edge.v.nodeIndex] += flow

    for node in graph.nodeList:
        i = node.nodeIndex
        if node.type == NodeType.SourceNode:
            assert outflow[i] == pytest.approx(0.0, abs=1e-3)  # pure sink, never an outgoing edge
            assert inflow[i] == pytest.approx(abs(cast(SourceNode, node).balance), abs=1e-3)
        elif node.type == NodeType.Withdraw:
            assert inflow[i] == pytest.approx(0.0, abs=1e-3)  # pure source, never an incoming edge
            assert outflow[i] <= cast(WithdrawNode, node).balance + 1e-3
        elif node.type == NodeType.Wallet and cast(WalletNode, node).balance > 0:
            assert outflow[i] - inflow[i] <= cast(WalletNode, node).balance + 1e-3
            assert outflow[i] - inflow[i] >= -1e-3  # can't absorb flow, only pass it through or contribute its own
        else:
            assert outflow[i] == pytest.approx(inflow[i], abs=1e-3)  # pure transit (Deposit, empty Wallet)


@pytest.mark.parametrize("seed", range(20))
def test_capacity_never_violated(seed):
    rng = random.Random(seed)
    graph, _dexes = _randomGraph(rng, numSurplus=4, numDeficit=3, surplusRange=(50, 150), deficitRange=(5, 40))
    params = _params()
    try:
        graphSolve(graph, params)
    except RuntimeError:
        pytest.skip(f"infeasible graph for seed={seed} — feasibility itself is covered elsewhere")

    for edge in graph.edgeList:
        assert (edge.flow or 0.0) <= (edge.capacity or 0.0) + 1e-3


@pytest.mark.parametrize("seed", range(15))
def test_deficit_exactly_met_when_generously_feasible(seed):
    """Surplus (4 dexes, $50-150 each) is deliberately far larger than
    deficit (3 dexes, $5-40 each) — comfortably feasible by construction, so
    a RuntimeError here is a real regression, not an expected outcome to
    branch on."""
    rng = random.Random(seed)
    graph, dexes = _randomGraph(rng, numSurplus=4, numDeficit=3, surplusRange=(50, 150), deficitRange=(5, 40))
    params = _params()
    graphSolve(graph, params)

    for dex in dexes:
        if dex.inbalance >= 0:
            continue
        met = sum(
            e.flow or 0.0
            for e in graph.edgeList
            if e.v.type == NodeType.SourceNode and cast(SourceNode, e.v).dex is dex
        )
        assert met == pytest.approx(abs(dex.inbalance), abs=1e-3)


@pytest.mark.parametrize("seed", range(10))
def test_infeasible_graph_raises_and_really_is_infeasible(seed):
    """The reverse construction — one tiny surplus dex against three huge
    deficits — deliberately far short: guards against a silent partial fill
    being reported as success instead of the solver correctly refusing."""
    rng = random.Random(seed)
    graph, dexes = _randomGraph(rng, numSurplus=1, numDeficit=3, surplusRange=(1, 5), deficitRange=(50, 100))
    params = _params()

    totalSurplus = sum(sum(d.withdrawBalances.values()) for d in dexes)
    totalDeficit = sum(abs(min(d.inbalance, 0.0)) for d in dexes)
    assert totalSurplus < totalDeficit  # sanity: this construction really is infeasible

    with pytest.raises(RuntimeError):
        graphSolve(graph, params)


def _evaluateBreakpoints(breakpoints: list[tuple[float, float]], amount: float) -> float:
    """Linear interpolation of a computeSwapCostBreakpoints-shaped
    piecewise-linear curve at an exact amount — mirrors what
    solver._addSwapCost's epigraph forces swapCost to equal at the
    solver's own chosen flow, so the greedy baseline below prices a swap
    edge the same way the real solver does, not by ignoring slippage."""
    if amount <= breakpoints[0][0]:
        return breakpoints[0][1]
    for (x0, y0), (x1, y1) in zip(breakpoints, breakpoints[1:]):
        if amount <= x1:
            if x1 == x0:
                return y1
            return y0 + (amount - x0) / (x1 - x0) * (y1 - y0)
    return breakpoints[-1][1]


def _greedySequentialBaselineCost(graph: Graph, params: TimeWeightParams) -> float:
    """A FEASIBLE (never overdraws shared capacity) baseline: route each
    deficit commodity ONE AT A TIME, in graph.deficitDexes()'s fixed order,
    via the independent nx shortest path against whichever WithdrawNode/
    positive-balance-WalletNode sources still have enough REMAINING balance
    to cover that commodity's FULL demand in one path — a naive "first
    come, first served, no partial multi-path routing" heuristic. Because it
    never lets two commodities overdraw the same shared source, this is a
    genuinely valid feasible solution to the SAME joint problem the solver
    solves, so the solver must never cost MORE than this (though it may
    cost less, by allocating scarce resources more cleverly than a fixed
    serving order — see test_solver_multicommodity_contested_resources.py
    for exactly that kind of smarter allocation).

    This is NOT the same as running a Dijkstra independently and
    SIMULTANEOUSLY per commodity (ignoring that they share capacity) — that
    version can double-claim scarce capacity and is not a valid feasible
    baseline; the sequential decrementing here is what makes it feasible.

    Because every commodity here routes its FULL demand through exactly one
    path (no partial multi-path routing, see above), a swap edge on that
    path always carries exactly `demand` — so its slippage contribution is
    computeSwapCostBreakpoints(edge) evaluated at `demand` (_evaluateBreakpoints),
    fed in as `extraCostByEdge` per commodity, same as
    test_solver_vs_shortest_path.py's swap scenario. Omitting this (an
    earlier version of this baseline did) silently under-priced any path
    through a swap edge and made the solver look worse than this baseline
    even though the solver was correctly including slippage all along.

    Relies on _randomGraph's surplus/deficit ranges keeping any single
    demand smaller than at least one surplus dex's balance, so a single
    fully-covering path always exists — raises AssertionError (a test-design
    problem, not a solver bug) if that assumption ever breaks."""
    remaining: dict[int, float] = {}
    for node in graph.nodeList:
        if node.type == NodeType.Withdraw:
            remaining[node.nodeIndex] = cast(WithdrawNode, node).balance
        elif node.type == NodeType.Wallet and cast(WalletNode, node).balance > 0:
            remaining[node.nodeIndex] = cast(WalletNode, node).balance

    nxGraph = toWeightedMultiDiGraph(graph)
    total = 0.0
    for dex in graph.deficitDexes():
        sourceNode = next(n for n in graph.nodeList if n.type == NodeType.SourceNode and cast(SourceNode, n).dex is dex)
        demand = abs(cast(SourceNode, sourceNode).balance)
        extraCostByEdge = {
            e: _evaluateBreakpoints(costing.computeSwapCostBreakpoints(e), demand)
            for e in graph.edgeList
            if e.type == EdgeType.Swap
        }
        weight = timeWeightedEdgeWeight(computeDexUrgencySigma(dex), params, extraCostByEdge=extraCostByEdge)

        eligibleSources = [idx for idx, cap in remaining.items() if cap >= demand - 1e-6]
        if not eligibleSources:
            raise AssertionError(
                f"no single remaining source can fully cover {dex.name}'s ${demand} demand — "
                "adjust _randomGraph's surplus/deficit ranges"
            )

        total += shortestWeightedCost(nxGraph, eligibleSources, sourceNode.nodeIndex, weight)
        chosenSourceIdx = shortestWeightedPath(nxGraph, eligibleSources, sourceNode.nodeIndex, weight)[0]
        remaining[chosenSourceIdx] -= demand

    return total


@pytest.mark.parametrize("seed", range(15))
def test_solved_cost_never_exceeds_feasible_greedy_sequential_baseline(seed):
    rng = random.Random(seed)
    graph, _dexes = _randomGraph(rng, numSurplus=4, numDeficit=3, surplusRange=(50, 150), deficitRange=(5, 40))
    params = _params()
    solver = graphSolve(graph, params)

    baseline = _greedySequentialBaselineCost(graph, params)

    assert totalCostUsd(solver) <= baseline + 1e-3
