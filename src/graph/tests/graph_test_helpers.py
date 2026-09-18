"""Shared helpers for the solver-validation test suite
(test_solver_vs_shortest_path.py, test_solver_multicommodity_contested_resources.py,
test_solver_invariants_random_graphs.py) plus a couple of the most-duplicated
accessors from the existing solver tests (test_solver_surplus_upper_bound.py,
test_solver_time_weight.py). Plain importable module, not an autouse
conftest.py — usage stays explicit per test file. The three existing test
files are deliberately left untouched (this is a pure addition); migrating
them to use these helpers instead of their own local copies is optional
future cleanup, not part of this change.
"""

from __future__ import annotations

import math
from typing import Callable, cast

import networkx as nx

from connectors.gas import GasFeeService
from graph import costing
from graph.edge import Edge
from graph.graph import Graph
from graph.node import NodeType, SourceNode, WalletNode, WithdrawNode
from graph.structures.DEXes import DEX, Chain, Stable
from graph.urgency import TimeWeightParams


class ZeroGasFeeService(GasFeeService):
    """No network calls: every test in this suite poses cost/time on edges
    by hand (or via zeroed DEX operational params) to isolate the behavior
    under test, exactly like the existing _ZeroGasFeeService/_FlatGas stubs
    in test_solver_surplus_upper_bound.py / test_solver_time_weight.py."""

    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.0

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.0


def zeroGasFeeService() -> GasFeeService:
    return ZeroGasFeeService()


def depositEdge(graph: Graph, toDex: DEX, chain) -> Edge:
    """The DIRECT deposit edge (WalletNode -> SourceNode, DEX.requiresDepositAddress=False,
    the default for every DEX in the registry) into `toDex` on `chain` —
    WalletNode is shared across DEXes, so there's no "fromDex" to filter on.
    Same lookup as test_solver_time_weight.py's local _depositEdge."""
    for edge in graph.edgeList:
        if edge.u.type != NodeType.Wallet or edge.v.type != NodeType.SourceNode:
            continue
        wallet = cast(WalletNode, edge.u)
        if wallet.chain == chain and cast(SourceNode, edge.v).dex is toDex:
            return edge
    raise AssertionError(f"no wallet->source edge into {toDex.name} on {chain}")


def withdrawEdge(graph: Graph, dex: DEX, stable: Stable, chain: Chain) -> Edge:
    """The WithdrawNode -> WalletNode(chain) edge for `dex`/`stable` — one
    per chain `dex` supports (the WithdrawNode itself is shared/fungible
    across them, see graph.node.WithdrawNode, but Graph._linkWithdrawalsAndDeposits
    creates a SEPARATE Edge per chain), hence `chain` is required even for a
    single-chain dex. Symmetric to depositEdge above."""
    for edge in graph.edgeList:
        if edge.u.type != NodeType.Withdraw:
            continue
        withdrawNode = cast(WithdrawNode, edge.u)
        if withdrawNode.dex is dex and withdrawNode.stable is stable and cast(WalletNode, edge.v).chain == chain:
            return edge
    raise AssertionError(f"no withdraw->wallet edge for {dex.name}/{stable.name} on {chain.name}")


def withdrawnAmount(graph: Graph, dex: DEX) -> float:
    """Total flow withdrawn from `dex`, summed across every chain/stable
    WithdrawNode it has. Same lookup as the existing tests' local _withdrawn."""
    return sum(
        e.flow or 0.0 for e in graph.edgeList if e.u.type == NodeType.Withdraw and cast(WithdrawNode, e.u).dex is dex
    )


def withdrawNodeIndex(graph: Graph, dex: DEX, stable: Stable) -> int:
    for node in graph.nodeList:
        if node.type == NodeType.Withdraw and cast(WithdrawNode, node).dex is dex and cast(WithdrawNode, node).stable is stable:
            return node.nodeIndex
    raise AssertionError(f"no WithdrawNode for {dex.name}/{stable.name}")


def sourceNodeIndex(graph: Graph, dex: DEX) -> int:
    for node in graph.nodeList:
        if node.type == NodeType.SourceNode and cast(SourceNode, node).dex is dex:
            return node.nodeIndex
    raise AssertionError(f"no SourceNode for {dex.name}")


def walletNodeIndex(graph: Graph, chain: Chain, stable: Stable) -> int:
    for node in graph.nodeList:
        if node.type == NodeType.Wallet and cast(WalletNode, node).chain == chain and cast(WalletNode, node).stable == stable:
            return node.nodeIndex
    raise AssertionError(f"no WalletNode for {chain.name}/{stable.name}")


# --- Independent shortest-path cross-check (networkx) -----------------------
#
# Deliberately NOT visualization.graph_view.buildNetworkxGraph: that helper
# only stores display strings (label/color) for nx.draw, not the Edge object
# itself, and uses a plain nx.DiGraph, which silently collapses parallel
# edges between the same two node indices (irrelevant today — no two Edge
# objects in this graph model currently share the exact same (u, v) node
# pair — but a MultiDiGraph is the structurally correct choice regardless,
# and costs nothing extra).
#
# Deliberately NOT visualization.web_view._dijkstraFromSources either: it's
# a hand-rolled, untested-itself Dijkstra sharing no code with the solver,
# which is fine for the UI's informational hover info, but reusing it here
# would just be testing one home-grown algorithm against another. networkx
# is a separate, well-tested, already-installed dependency — a genuinely
# independent implementation.

_SUPER_SOURCE = "__super_source__"


def toWeightedMultiDiGraph(graph: Graph) -> nx.MultiDiGraph:
    """Converts a Graph (after computeAllCosts/computeAllDelays — i.e. any
    Graph, solved or not, since edge.cost/edge.time are set at construction
    time) into an nx.MultiDiGraph keyed by node.nodeIndex, storing the
    original Edge object on each nx edge (data={"edge": edge}) so a weight
    function can read .cost/.time directly instead of duplicating them."""
    nxGraph = nx.MultiDiGraph()
    for node in graph.nodeList:
        nxGraph.add_node(node.nodeIndex)
    for i, edge in enumerate(graph.edgeList):
        nxGraph.add_edge(edge.u.nodeIndex, edge.v.nodeIndex, key=i, edge=edge)
    return nxGraph


def timeWeightedEdgeWeight(
    sigmaD: float,
    params: TimeWeightParams,
    extraCostByEdge: dict[Edge, float] | None = None,
) -> Callable[[object, object, dict], float]:
    """Returns a networkx multigraph-compatible weight function computing
    the SAME per-edge-per-commodity weight solver.buildModel actually
    minimizes: Fee(e) + λ(σ_d)·Time(e) (costing.computeTimeWeightedCost,
    costing.py:168) — NOT edge.cost or edge.time alone, which is what
    visualization/web_view.py's "cheapest"/"fastest" UI paths use and would
    NOT agree with the solver whenever fee and time trade off.

    `extraCostByEdge` adds a fixed extra cost for specific edges — used for
    swap slippage, which computeTimeWeightedCost deliberately does not cover
    (see test_solver_vs_shortest_path.py's swap scenario).

    For an nx.MultiDiGraph with a callable `weight`, networkx calls this
    function as weight(u, v, keydict) where keydict is {key: data} for every
    parallel edge between u and v (see
    networkx.algorithms.shortest_paths.weighted._weight_function) — NOT a
    single edge's data dict. This returns the cheapest parallel edge, as a
    plain string-keyed weight would for a multigraph.
    """
    extra = extraCostByEdge or {}

    def _weight(_u: object, _v: object, keydict: dict) -> float:
        best = math.inf
        for data in keydict.values():
            edge = data.get("edge")
            if edge is None:
                # Synthetic zero-weight super-source edge, see
                # shortestWeightedCost/shortestWeightedPath below.
                best = min(best, 0.0)
                continue
            cost = costing.computeTimeWeightedCost(edge.cost or 0.0, edge.time or 0.0, sigmaD, params)
            best = min(best, cost + extra.get(edge, 0.0))
        return best

    return _weight


def _withSuperSource(nxGraph: nx.MultiDiGraph, sourceIndices: list[int]) -> nx.MultiDiGraph:
    """A copy of `nxGraph` plus a virtual node connected to every node in
    `sourceIndices` by a zero-weight edge (no "edge" data key — the weight
    function above treats that as 0.0) — the same multi-source trick
    visualization.web_view._dijkstraFromSources uses, reimplemented
    independently here via a virtual node instead of seeding Dijkstra's heap
    with multiple zero-distance starts."""
    g = nxGraph.copy()
    g.add_node(_SUPER_SOURCE)
    for idx in sourceIndices:
        g.add_edge(_SUPER_SOURCE, idx, key="super")
    return g


def shortestWeightedCost(
    nxGraph: nx.MultiDiGraph,
    sourceIndices: list[int],
    targetIndex: int,
    weight: Callable[[object, object, dict], float],
) -> float:
    """Cheapest of the per-source shortest-path costs to `targetIndex`, under
    `weight` (typically timeWeightedEdgeWeight(...)) — the independent
    cross-check for what the solver's own commodity for `targetIndex`
    (a deficit SourceNode) should cost, when exactly one commodity is
    competing for the graph's capacity (see test_solver_vs_shortest_path.py;
    NOT valid ground truth once multiple commodities share a bottleneck, see
    test_solver_multicommodity_contested_resources.py)."""
    g = _withSuperSource(nxGraph, sourceIndices)
    return nx.dijkstra_path_length(g, _SUPER_SOURCE, targetIndex, weight=weight)


def shortestWeightedPath(
    nxGraph: nx.MultiDiGraph,
    sourceIndices: list[int],
    targetIndex: int,
    weight: Callable[[object, object, dict], float],
) -> list[int]:
    """Same as shortestWeightedCost but returns the node-index path (super
    source stripped), for asserting which edges the solver should have used."""
    g = _withSuperSource(nxGraph, sourceIndices)
    path = nx.dijkstra_path(g, _SUPER_SOURCE, targetIndex, weight=weight)
    return path[1:]  # drop the synthetic super source
