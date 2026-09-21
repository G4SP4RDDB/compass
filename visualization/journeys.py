from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from graph.edge import Edge
from graph.graph import Graph
from graph.node import Node, NodeType, SourceNode, WalletDeficitNode, WalletNode, WithdrawNode

_TERMINAL_NODE_TYPES = (NodeType.SourceNode, NodeType.WalletDeficit)

_FLOW_EPS = 1e-9


@dataclass
class Journey:
    """Un trajet DEX -> DEX reconstruit à partir du flot agrégé choisi par le
    solveur (edge.flow), en suivant les arêtes à flot positif depuis un
    WithdrawNode jusqu'au SourceNode d'arrivée. Voir decomposeJourneys."""

    # DEX de départ, OU "Wallet <CHAIN>/<STABLE>" quand le trajet part de
    # l'argent déjà présent sur l'operating wallet (voir WalletNode.balance,
    # fromWallet=True) : pas de retrait DEX en tête, le premier hop est
    # directement un dépôt/bridge/swap depuis ce wallet.
    fromDex: str
    toDex: str
    amount: float
    # Stable ENVOYÉE au départ (celle du WithdrawNode d'origine, voir
    # _extractOneJourney) — jamais ambiguë : chaque trajet démarre sur UN
    # retrait d'UNE stable précise (voir WithdrawNode.stable). Peut différer
    # de la stable reçue à l'arrivée si le trajet traverse un swap (voir
    # EdgeType.Swap) ; ce champ ne suit que le "quoi j'envoie", pas les
    # conversions en route.
    stable: str
    hops: list[Edge] = field(default_factory=list)
    # True si ce trajet passe par un nœud où plusieurs flots se mélangent
    # PUIS se re-séparent : dans ce cas l'appariement source/destination
    # n'est plus déterminé par edge.flow seul (voir decomposeJourneys), ce
    # trajet n'est qu'UNE explication plausible parmi toutes celles qui
    # reproduiraient le même flot agrégé.
    plausible: bool = False
    fromWallet: bool = False


def decomposeJourneys(graph: Graph) -> list[Journey]:
    """Décompose edge.flow (déjà résolu par graph.solver.graphSolve) en
    trajets DEX -> DEX, par extraction répétée de chemin — l'algorithme
    standard de décomposition d'un flot en chemins simples. N'ajoute aucun
    concept au modèle : ne lit que edge.flow, déjà présent sur chaque Edge.

    Limite assumée : le solveur résout un flot MULTI-COMMODITÉ (une
    commodité par DEX destination déficitaire, voir graph/solver.py), mais
    graphSolve() n'enregistre que la somme par arête (edge.flow), pas la
    répartition par commodité. Dès qu'une arête est empruntée par plusieurs
    commodités à la fois (ex: un bridge partagé par un flot vers
    Hyperliquid et un flot vers Aster), le total agrégé ne dit plus quelle
    part vient de quelle source — la décomposition choisit alors UNE
    répartition valide parmi toutes celles qui somment au bon total,
    marquée `plausible=True` sur les trajets concernés (voir
    _ambiguousNodeIndices), plutôt que de complexifier le solveur pour
    tracker la vraie provenance par commodité source."""
    ambiguousNodes = _ambiguousNodeIndices(graph)

    remaining: dict[int, float] = {
        i: cast(float, edge.flow) for i, edge in enumerate(graph.edgeList) if edge.flow and edge.flow > _FLOW_EPS
    }
    outEdgesByNode: dict[int, list[int]] = {}
    for i in remaining:
        outEdgesByNode.setdefault(graph.edgeList[i].u.nodeIndex, []).append(i)

    journeys: list[Journey] = []
    for node in graph.nodeList:
        if node.type != NodeType.Withdraw:
            continue
        while any(remaining.get(i, 0.0) > _FLOW_EPS for i in outEdgesByNode.get(node.nodeIndex, [])):
            journey = _extractOneJourney(graph, node, remaining, outEdgesByNode, ambiguousNodes)
            if journey is None:
                break
            journeys.append(journey)

    # Ce qui reste après avoir épuisé les retraits DEX ne peut venir que du
    # solde propre des wallets (voir WalletNode.balance et
    # solver._addFlowConservation) : un trajet par tranche, qui démarre
    # directement au wallet. Un wallet à solde 0 n'a par construction plus
    # de flot sortant non apparié ici (transit pur : tout ce qui sort y est
    # entré via un trajet déjà extrait).
    for node in graph.nodeList:
        if node.type != NodeType.Wallet:
            continue
        while any(remaining.get(i, 0.0) > _FLOW_EPS for i in outEdgesByNode.get(node.nodeIndex, [])):
            journey = _extractOneJourney(graph, node, remaining, outEdgesByNode, ambiguousNodes)
            if journey is None:
                break
            journeys.append(journey)

    journeys.sort(key=lambda j: j.amount, reverse=True)
    return journeys


def _ambiguousNodeIndices(graph: Graph) -> set[int]:
    """Nœuds où le flot agrégé fusionne (>1 arête entrante à flot positif)
    ET se re-sépare (>1 arête sortante à flot positif) : seuls ces points
    peuvent faire inventer à la décomposition un appariement source/
    destination qui n'est pas forcément celui réellement produit par le
    solveur multi-commodité (voir decomposeJourneys)."""
    inCount: dict[int, int] = {}
    outCount: dict[int, int] = {}
    for edge in graph.edgeList:
        if not edge.flow or edge.flow <= _FLOW_EPS:
            continue
        outCount[edge.u.nodeIndex] = outCount.get(edge.u.nodeIndex, 0) + 1
        inCount[edge.v.nodeIndex] = inCount.get(edge.v.nodeIndex, 0) + 1
    return {n for n, count in inCount.items() if count > 1 and outCount.get(n, 0) > 1}


def _extractOneJourney(
    graph: Graph,
    startNode: Node,
    remaining: dict[int, float],
    outEdgesByNode: dict[int, list[int]],
    ambiguousNodes: set[int],
) -> Journey | None:
    path: list[int] = []
    visited = {startNode.nodeIndex}
    touchesAmbiguous = False
    current: Node = startNode

    while current.type not in _TERMINAL_NODE_TYPES:
        candidates = [i for i in outEdgesByNode.get(current.nodeIndex, []) if remaining.get(i, 0.0) > _FLOW_EPS]
        if not candidates:
            return None  # flot bloqué : ne devrait pas arriver sur une solution valide du solveur
        if current.nodeIndex in ambiguousNodes:
            touchesAmbiguous = True
        edgeIndex = candidates[0]
        path.append(edgeIndex)
        current = graph.edgeList[edgeIndex].v
        if current.nodeIndex in visited:
            return None  # cycle de flot : ne devrait pas arriver, on abandonne plutôt que de boucler
        visited.add(current.nodeIndex)

    amount = min(remaining[i] for i in path)
    for i in path:
        remaining[i] -= amount
        if remaining[i] <= _FLOW_EPS:
            del remaining[i]

    if startNode.type == NodeType.Wallet:
        wallet = cast(WalletNode, startNode)
        fromLabel, stable, fromWallet = f"Wallet {wallet.chain.name}/{wallet.stable.name}", wallet.stable, True
    else:
        withdraw = cast(WithdrawNode, startNode)
        fromLabel, stable, fromWallet = withdraw.dex.name, withdraw.stable, False
    if current.type == NodeType.WalletDeficit:
        # Trajet vers un déficit wallet (retrait utilisateur, voir
        # graph.node.WalletDeficitNode), pas vers un DEX -- pas de .dex à lire.
        toLabel = f"User payouts ({cast(WalletDeficitNode, current).stable.name})"
    else:
        toLabel = cast(SourceNode, current).dex.name
    return Journey(
        fromDex=fromLabel,
        toDex=toLabel,
        amount=amount,
        stable=stable.name,
        hops=[graph.edgeList[i] for i in path],
        plausible=touchesAmbiguous,
        fromWallet=fromWallet,
    )
