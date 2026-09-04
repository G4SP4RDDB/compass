from __future__ import annotations

import matplotlib

# Backend non interactif : renderGraph() est appelé depuis un thread de
# requête Flask par visualization/server.py (POST /api/recompute), et le
# backend GUI par défaut de matplotlib refuse de créer une figure hors du
# thread principal ("Cannot create a GUI FigureManager outside the main
# thread"). Sans effet sur l'usage CLI (show=False par défaut, voir plus bas).
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import networkx as nx  # noqa: E402

from graph.edge import Edge, EdgeType
from graph.graph import Graph
from graph.node import Node, NodeType

NODE_COLOR_BY_TYPE = {
    NodeType.SourceNode: "gold",
    NodeType.Withdraw: "khaki",
    NodeType.Wallet: "lightsteelblue",
    NodeType.Deposit: "skyblue",
}

# Position en colonnes : Source/Withdraw -> Wallet (partagé) -> Deposit
# (adresse CEX-style), pour retrouver visuellement le sens du flot plutôt
# qu'un nuage de points désordonné. Bridge/Swap n'ont plus de node dédié
# (voir graph.node.WalletNode) : ce sont des edges Wallet->Wallet, voir
# EDGE_COLOR_BY_TYPE pour les distinguer visuellement.
NODE_LAYER_BY_TYPE = {
    NodeType.SourceNode: 0,
    NodeType.Withdraw: 0,
    NodeType.Wallet: 1,
    NodeType.Deposit: 2,
}

EDGE_COLOR_BY_TYPE = {
    EdgeType.Bridge: "lightgreen",
    EdgeType.Swap: "orange",
}


def _nodeLabel(node: Node) -> str:
    if node.type == NodeType.SourceNode:
        return f"{node.dex.name}\nbal={node.balance:g}"
    if node.type == NodeType.Withdraw:
        return f"{node.dex.name}\n{node.stable.name}\nbal={node.balance:g}"
    if node.type == NodeType.Wallet:
        return f"Wallet\n{node.chain.name}/{node.stable.name}"
    if node.type == NodeType.Deposit:
        return f"{node.dex.name}\n{node.chain.name}/{node.stable.name}"
    return node.type.name


def _edgeLabel(edge: Edge) -> str | None:
    parts = []
    if edge.type is not None:
        parts.append(edge.type.name)
    if edge.cost is not None:
        parts.append(f"cost={edge.cost:.2f}")
    if edge.capacity is not None:
        parts.append(f"cap={edge.capacity:g}")
    return "\n".join(parts) if parts else None


def buildNetworkxGraph(graph: Graph) -> nx.DiGraph:
    nxGraph = nx.DiGraph()
    nodeToId = {node: i for i, node in enumerate(graph.nodeList)}

    for node, nodeId in nodeToId.items():
        nxGraph.add_node(
            nodeId,
            label=_nodeLabel(node),
            color=NODE_COLOR_BY_TYPE.get(node.type, "lightgray"),
            layer=NODE_LAYER_BY_TYPE.get(node.type, 0),
        )

    for edge in graph.edgeList:
        nxGraph.add_edge(
            nodeToId[edge.u],
            nodeToId[edge.v],
            label=_edgeLabel(edge),
            color=EDGE_COLOR_BY_TYPE.get(edge.type, "gray"),
        )

    return nxGraph


def renderGraph(graph: Graph, outputPath: str = "graph.png", show: bool = False) -> None:
    nxGraph = buildNetworkxGraph(graph)

    layers = nx.get_node_attributes(nxGraph, "layer")
    positions = (
        nx.multipartite_layout(nxGraph, subset_key="layer")
        if len(set(layers.values())) > 1
        else nx.spring_layout(nxGraph, seed=0)
    )

    colors = [data["color"] for _, data in nxGraph.nodes(data=True)]
    labels = {nodeId: data["label"] for nodeId, data in nxGraph.nodes(data=True)}
    edgeColors = [data["color"] for _, _, data in nxGraph.edges(data=True)]
    edgeLabels = {
        (u, v): data["label"] for u, v, data in nxGraph.edges(data=True) if data.get("label")
    }

    # La colonne la plus chargée (souvent Deposit) fixe la hauteur nécessaire :
    # sans ça, les labels se chevauchent dès qu'un graphe dépasse une dizaine
    # de nodes par couche (ex: le registre complet des DEX).
    maxNodesPerLayer = max(list(layers.values()).count(layer) for layer in set(layers.values())) if layers else 1
    figHeight = max(10, maxNodesPerLayer * 0.55)

    plt.figure(figsize=(16, figHeight))
    nx.draw(
        nxGraph,
        positions,
        labels=labels,
        node_color=colors,
        edge_color=edgeColors,
        node_size=900,
        font_size=6,
        arrows=True,
        arrowsize=12,
        connectionstyle="arc3,rad=0.08",
    )
    if edgeLabels:
        nx.draw_networkx_edge_labels(nxGraph, positions, edge_labels=edgeLabels, font_size=5)

    plt.tight_layout()
    plt.savefig(outputPath, dpi=150)
    if show:
        plt.show()
    plt.close()


if __name__ == "__main__":
    from connectors.gas import GasFeeService
    from graph.structures.DEXes import DEX, Chain, Stable

    class _FakeGasFeeService(GasFeeService):
        def __init__(self) -> None:
            pass

        def get_gas_cost_usd(self, chain, operation) -> float:
            return 1.0

        def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
            return 1.0

    dexA = DEX([Chain.ETHEREUM, Chain.BASE], [Stable.USDC])
    dexA.inbalance = 100

    dexB = DEX([Chain.ETHEREUM, Chain.ARBITRUM], [Stable.USDC])
    dexB.inbalance = -60

    demoGraph = Graph([dexA, dexB], [], gasFeeService=_FakeGasFeeService())
    demoGraph.computeAllCosts()
    demoGraph.computeAllCapacities()

    renderGraph(demoGraph, outputPath="graph.png")
    print("Graphe écrit dans graph.png")
