"""Validates the solver's route choice on genuinely CONTESTED multi-commodity
scenarios: two deficit DEXes that can both draw on the SAME limited surplus.

test_solver_vs_shortest_path.py's technique (an independent single-commodity
Dijkstra per deficit) does NOT generalize here: run a Dijkstra for deficit A
alone and one for deficit B alone, and both would happily claim the WHOLE
shared resource for themselves, since neither's own shortest-path search
knows the other commodity exists. Jointly allocating a scarce shared resource
across competing commodities is exactly the problem CP-SAT (solver.buildModel)
exists to solve instead of N independent shortest-path searches.

Scenarios here are kept small enough (integer dollar amounts, one shared
bottleneck, two commodities) that the TRUE joint optimum can be found by
EXHAUSTIVE brute-force enumeration (_bruteForceOptimalAssignment below) —
mechanically trying every way to split the shared capacity and pricing each
split with the exact fixed-charge formula solver.buildModel uses, rather than
reasoning about which split "should" be optimal (a first hand-derivation of
these scenarios' expected numbers got the optimal split wrong by assuming the
leftover shared capacity should always go to whoever didn't get priority —
it shouldn't, see _bruteForceOptimalAssignment's docstring — which is
precisely why brute force beats hand-wavy intuition here).

Deliberately never asserts on visualization.journeys.decomposeJourneys:
these scenarios engineer a node where flow from multiple commodities merges
then re-splits (see journeys.py::_ambiguousNodeIndices), so an individual
Journey object is "one plausible explanation" of the aggregate edge.flow,
not ground truth. Every assertion here is on edge.flow / totalCostUsd
directly, which IS ground truth regardless of ambiguity.
"""

from __future__ import annotations

import math

import pytest

from graph import costing
from graph.graph import Graph
from graph.solver import graphSolve, totalCostUsd
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.positions import Position
from graph.urgency import TimeWeightParams, computeDexUrgencySigma

from graph_test_helpers import depositEdge, withdrawEdge, withdrawnAmount, zeroGasFeeService


def _params() -> TimeWeightParams:
    return TimeWeightParams(lambda_min=0.01, lambda_max=5.0, k=2.0, epsilon=0.1)


def _bruteForceOptimalAssignment(
    sharedCapacity: float,
    sharedFee: float,
    commodities: list[tuple[float, float, float]],
) -> float:
    """The true joint optimum for exactly two commodities competing for one
    shared-capacity edge, found by exhaustively enumerating EVERY way to
    split `sharedCapacity` between them.

    Each candidate split (sharedToA, sharedToB) is chosen INDEPENDENTLY for
    each commodity (both loop over their own full range), not derived one
    from the other ("give the loser whatever capacity is left") — that
    simpler-looking approach silently assumes the loser should always grab
    any leftover shared capacity, which is WRONG whenever the loser still
    needs its own fallback for the remainder anyway: since every cost here
    is a FLAT per-edge-per-commodity charge (paid once if >0, never
    per-dollar — see solver.py's `usedTime_{i}_{d}` boolean), touching the
    shared edge for a partial amount that doesn't avoid opening the fallback
    edge anyway just adds an extra flat charge for nothing. Only a full,
    independent 2D enumeration is guaranteed to find splits like "commodity
    A takes $0 of the shared capacity and leaves it idle, relying entirely
    on its own fallback" when that turns out cheaper.

    `sharedFee` (solver's Fee(e) for the shared edge) is paid ONCE if EITHER
    commodity draws anything from it (the shared, not-per-commodity `used_i`
    boolean). `commodities`: exactly two (demand, sharedTimeWeight,
    fallbackFlatCost) tuples, where sharedTimeWeight is already
    costing.computeTimeWeightedCost(0.0, sharedEdge.time, sigma, params)
    for that commodity's own sigma (fee=0 — the shared edge's Fee is
    `sharedFee` above, charged once, not per commodity) and fallbackFlatCost
    is costing.computeTimeWeightedCost(fallbackEdge.cost, fallbackEdge.time, sigma, params)
    (its own private edge, never shared, so Fee+time are bundled as usual)."""
    (demandA, sharedTimeWeightA, fallbackFlatA), (demandB, sharedTimeWeightB, fallbackFlatB) = commodities
    best = math.inf
    for sharedToA in range(0, int(min(sharedCapacity, demandA)) + 1):
        for sharedToB in range(0, int(min(sharedCapacity, demandB)) + 1):
            if sharedToA + sharedToB > sharedCapacity:
                continue
            fallbackNeededA = demandA - sharedToA
            fallbackNeededB = demandB - sharedToB
            cost = sharedFee if (sharedToA > 0 or sharedToB > 0) else 0.0
            cost += sharedTimeWeightA if sharedToA > 0 else 0.0
            cost += sharedTimeWeightB if sharedToB > 0 else 0.0
            cost += fallbackFlatA if fallbackNeededA > 0 else 0.0
            cost += fallbackFlatB if fallbackNeededB > 0 else 0.0
            best = min(best, cost)
    return best


def test_shared_cheap_source_insufficient_for_both_deficits_splits_by_hand_computed_optimum():
    """One surplus source ($60) cheaper than either deficit's own fallback;
    two deficits each need $50 (combined demand $100 > $60 shared supply),
    each with its own pricier fallback (different prices, so the split is
    genuinely non-symmetric)."""
    shared = DEX([Chain.ETHEREUM], [Stable.USDC], name="shared")
    shared.withdrawBalances = {Stable.USDC: 60.0}

    dexA = DEX([Chain.ETHEREUM], [Stable.USDC], name="dexA")
    dexA.inbalance = -50.0
    fallbackA = DEX([Chain.ETHEREUM], [Stable.USDC], name="fallbackA")
    fallbackA.withdrawBalances = {Stable.USDC: 100.0}

    dexB = DEX([Chain.ETHEREUM], [Stable.USDC], name="dexB")
    dexB.inbalance = -50.0
    fallbackB = DEX([Chain.ETHEREUM], [Stable.USDC], name="fallbackB")
    fallbackB.withdrawBalances = {Stable.USDC: 100.0}

    graph = Graph([shared, dexA, fallbackA, dexB, fallbackB], swapList=[], gasFeeService=zeroGasFeeService())

    sharedWithdraw = withdrawEdge(graph, shared, Stable.USDC, Chain.ETHEREUM)
    sharedWithdraw.cost, sharedWithdraw.time = 1.0, 60.0
    fallbackAWithdraw = withdrawEdge(graph, fallbackA, Stable.USDC, Chain.ETHEREUM)
    fallbackAWithdraw.cost, fallbackAWithdraw.time = 5.0, 200.0
    fallbackBWithdraw = withdrawEdge(graph, fallbackB, Stable.USDC, Chain.ETHEREUM)
    fallbackBWithdraw.cost, fallbackBWithdraw.time = 8.0, 300.0

    depositA = depositEdge(graph, dexA, Chain.ETHEREUM)
    depositA.cost, depositA.time = 0.0, 0.0
    depositB = depositEdge(graph, dexB, Chain.ETHEREUM)
    depositB.cost, depositB.time = 0.0, 0.0

    dexA.positions = [Position(sigma=1000.0)]
    dexB.positions = [Position(sigma=1000.0)]
    params = _params()

    solver = graphSolve(graph, params)

    sigmaA = computeDexUrgencySigma(dexA)
    sigmaB = computeDexUrgencySigma(dexB)
    expected = _bruteForceOptimalAssignment(
        sharedCapacity=60.0,
        sharedFee=sharedWithdraw.cost,
        commodities=[
            (
                50.0,
                costing.computeTimeWeightedCost(0.0, sharedWithdraw.time, sigmaA, params),
                costing.computeTimeWeightedCost(fallbackAWithdraw.cost, fallbackAWithdraw.time, sigmaA, params),
            ),
            (
                50.0,
                costing.computeTimeWeightedCost(0.0, sharedWithdraw.time, sigmaB, params),
                costing.computeTimeWeightedCost(fallbackBWithdraw.cost, fallbackBWithdraw.time, sigmaB, params),
            ),
        ],
    )

    assert totalCostUsd(solver) == pytest.approx(expected, abs=1e-3)
    assert depositA.flow == pytest.approx(50.0)
    assert depositB.flow == pytest.approx(50.0)


def test_urgent_deficit_wins_contested_source_over_normal_deficit():
    """Shared source exactly $50, two deficits each needing $50, each with
    its OWN fallback at the SAME fee but a much SLOWER time — the only
    difference between the two deficits is urgency (sigma). This is the
    scenario that most directly shows why an independent per-commodity
    Dijkstra would be wrong: run one for the urgent deficit alone and one
    for the normal deficit alone, and both would "want" the exact same $50."""
    shared = DEX([Chain.ETHEREUM], [Stable.USDC], name="shared")
    shared.withdrawBalances = {Stable.USDC: 50.0}

    dexUrgent = DEX([Chain.ETHEREUM], [Stable.USDC], name="urgent")
    dexUrgent.inbalance = -50.0
    fallbackUrgent = DEX([Chain.ETHEREUM], [Stable.USDC], name="fallbackUrgent")
    fallbackUrgent.withdrawBalances = {Stable.USDC: 100.0}

    dexNormal = DEX([Chain.ETHEREUM], [Stable.USDC], name="normal")
    dexNormal.inbalance = -50.0
    fallbackNormal = DEX([Chain.ETHEREUM], [Stable.USDC], name="fallbackNormal")
    fallbackNormal.withdrawBalances = {Stable.USDC: 100.0}

    graph = Graph(
        [shared, dexUrgent, fallbackUrgent, dexNormal, fallbackNormal], swapList=[], gasFeeService=zeroGasFeeService()
    )

    sharedWithdraw = withdrawEdge(graph, shared, Stable.USDC, Chain.ETHEREUM)
    sharedWithdraw.cost, sharedWithdraw.time = 1.0, 10.0
    fallbackUrgentWithdraw = withdrawEdge(graph, fallbackUrgent, Stable.USDC, Chain.ETHEREUM)
    fallbackUrgentWithdraw.cost, fallbackUrgentWithdraw.time = 1.0, 500.0
    fallbackNormalWithdraw = withdrawEdge(graph, fallbackNormal, Stable.USDC, Chain.ETHEREUM)
    fallbackNormalWithdraw.cost, fallbackNormalWithdraw.time = 1.0, 500.0

    depositUrgent = depositEdge(graph, dexUrgent, Chain.ETHEREUM)
    depositUrgent.cost, depositUrgent.time = 0.0, 0.0
    depositNormal = depositEdge(graph, dexNormal, Chain.ETHEREUM)
    depositNormal.cost, depositNormal.time = 0.0, 0.0

    dexUrgent.positions = [Position(sigma=0.001)]
    dexNormal.positions = [Position(sigma=1000.0)]
    params = _params()

    solver = graphSolve(graph, params)

    sigmaUrgent = computeDexUrgencySigma(dexUrgent)
    sigmaNormal = computeDexUrgencySigma(dexNormal)
    expected = _bruteForceOptimalAssignment(
        sharedCapacity=50.0,
        sharedFee=sharedWithdraw.cost,
        commodities=[
            (
                50.0,
                costing.computeTimeWeightedCost(0.0, sharedWithdraw.time, sigmaUrgent, params),
                costing.computeTimeWeightedCost(fallbackUrgentWithdraw.cost, fallbackUrgentWithdraw.time, sigmaUrgent, params),
            ),
            (
                50.0,
                costing.computeTimeWeightedCost(0.0, sharedWithdraw.time, sigmaNormal, params),
                costing.computeTimeWeightedCost(fallbackNormalWithdraw.cost, fallbackNormalWithdraw.time, sigmaNormal, params),
            ),
        ],
    )

    assert totalCostUsd(solver) == pytest.approx(expected, abs=1e-3)
    assert depositUrgent.flow == pytest.approx(50.0)
    assert depositNormal.flow == pytest.approx(50.0)
    # The whole point of this scenario: the URGENT deficit gets the fast
    # shared source, the normal one is pushed to its own (equally-priced but
    # much slower) fallback — the exact opposite of what happens if urgency
    # is ignored (test_shared_cheap_source_insufficient_for_both_deficits_...
    # above splits purely on fee/time tradeoffs with equal urgency instead).
    assert withdrawnAmount(graph, shared) == pytest.approx(50.0)
    assert withdrawnAmount(graph, fallbackUrgent) == pytest.approx(0.0)
    assert withdrawnAmount(graph, fallbackNormal) == pytest.approx(50.0)
