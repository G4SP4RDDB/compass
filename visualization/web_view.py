from __future__ import annotations

import heapq
import json
from pathlib import Path
from typing import Any, cast

from graph.edge import Edge
from graph.graph import Graph
from graph.node import BridgeNode, DepositNode, Node, NodeType, SourceNode, SwapNode, WithdrawNode
from graph.structures.DEXes import DEX

TEMPLATE_PATH = Path(__file__).parent / "web" / "graph_template.html"


def _describe(node: Node) -> str:
    """Description lisible d'un node du graphe bas niveau, utilisée
    uniquement pour l'affichage des hops d'un chemin — ces nodes (Deposit,
    Bridge, Swap, ...) ne sont plus dessinés eux-mêmes (visualization/web_view.py
    ne montre qu'un node par DEX)."""
    if node.type == NodeType.SourceNode:
        return cast(SourceNode, node).dex.name
    if node.type == NodeType.Withdraw:
        n = cast(WithdrawNode, node)
        return f"{n.dex.name} withdraw ({n.stable.name})"
    if node.type == NodeType.Deposit:
        n = cast(DepositNode, node)
        return f"{n.dex.name} deposit {n.chain.name}/{n.stable.name}"
    if node.type == NodeType.Bridge:
        n = cast(BridgeNode, node)
        return f"Bridge {n.chain.name}/{n.stable.name}"
    if node.type == NodeType.Swap:
        n = cast(SwapNode, node)
        return f"Swap {n.stableIn.name}->{n.stableOut.name} ({n.chain.name})"
    return node.type.name


def _dijkstraFromSources(graph: Graph, sourceIds: set[int]) -> tuple[dict[int, float], dict[int, Edge]]:
    adjacency: dict[int, list[Edge]] = {}
    for edge in graph.edgeList:
        adjacency.setdefault(edge.u.nodeIndex, []).append(edge)

    dist: dict[int, float] = {sourceId: 0.0 for sourceId in sourceIds}
    prevEdge: dict[int, Edge] = {}
    visited: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, sourceId) for sourceId in sourceIds]
    heapq.heapify(heap)

    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        for edge in adjacency.get(u, []):
            weight = edge.cost or 0.0
            newDist = d + weight
            v = edge.v.nodeIndex
            if newDist < dist.get(v, float("inf")) - 1e-12:
                dist[v] = newDist
                prevEdge[v] = edge
                heapq.heappush(heap, (newDist, v))

    return dist, prevEdge


def _reconstructPath(prevEdge: dict[int, Edge], target: int, sourceIds: set[int]) -> list[Edge] | None:
    edges: list[Edge] = []
    current = target
    while current not in sourceIds:
        edge = prevEdge.get(current)
        if edge is None:
            return None
        edges.append(edge)
        current = edge.u.nodeIndex
    edges.reverse()
    return edges


def _dexNodeDict(dex: DEX, sourceNodeId: int) -> dict[str, Any]:
    return {
        "id": sourceNodeId,
        "name": dex.name,
        "inbalance": dex.inbalance,
        "stables": [s.name for s in dex.stables],
        "chains": [c.name for c in dex.chains],
        "withdrawBalances": {s.name: bal for s, bal in dex.withdrawBalances.items()},
    }


def _computeDexPaths(graph: Graph) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    dexByName: dict[str, DEX] = {}
    sourceNodeIdByDex: dict[str, int] = {}
    withdrawNodeIdsByDex: dict[str, set[int]] = {}

    for node in graph.nodeList:
        if node.type == NodeType.SourceNode:
            n = cast(SourceNode, node)
            dexByName[n.dex.name] = n.dex
            sourceNodeIdByDex[n.dex.name] = n.nodeIndex
        elif node.type == NodeType.Withdraw:
            n = cast(WithdrawNode, node)
            withdrawNodeIdsByDex.setdefault(n.dex.name, set()).add(n.nodeIndex)

    dexNodes = [_dexNodeDict(dexByName[name], sourceNodeIdByDex[name]) for name in dexByName]

    paths: dict[str, dict[str, Any]] = {}
    for name in dexByName:
        sourceIds = withdrawNodeIdsByDex.get(name, set())
        perDex: dict[str, Any] = {}
        if sourceIds:
            dist, prevEdge = _dijkstraFromSources(graph, sourceIds)
            for otherName in dexByName:
                if otherName == name:
                    continue
                targetId = sourceNodeIdByDex[otherName]
                if targetId not in dist:
                    continue
                pathEdges = _reconstructPath(prevEdge, targetId, sourceIds)
                if pathEdges is None:
                    continue
                perDex[otherName] = {
                    "totalCost": dist[targetId],
                    "hops": [
                        {"from": _describe(edge.u), "to": _describe(edge.v), "cost": edge.cost}
                        for edge in pathEdges
                    ],
                }
        paths[name] = perDex

    return dexNodes, paths


def graphToDict(graph: Graph) -> dict[str, Any]:
    dexNodes, paths = _computeDexPaths(graph)
    return {"dexNodes": dexNodes, "paths": paths}


def renderGraphHtml(graph: Graph, outputPath: str = "graph.html") -> None:
    data = graphToDict(graph)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__GRAPH_DATA_JSON__", json.dumps(data))
    Path(outputPath).write_text(html, encoding="utf-8")
