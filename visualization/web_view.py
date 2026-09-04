from __future__ import annotations

import heapq
import json
from pathlib import Path
from typing import Any, Callable, cast

from graph.edge import Edge, EdgeType
from graph.graph import Graph
from graph.node import DepositNode, Node, NodeType, SourceNode, WalletNode, WithdrawNode
from graph.structures.bridges import BridgeProtocol
from graph.structures.DEXes import DEX
from graph.urgency import TimeWeightParams
from visualization.dex_branding import DEX_BRANDING
from visualization.journeys import Journey, decomposeJourneys

TEMPLATE_PATH = Path(__file__).parent / "web" / "graph_template.html"

# Ce module construit les données exportées vers graph_template.html (une
# page HTML statique + JS, voir renderGraphHtml) et vers operations.txt (voir
# writeOperationsText). Il expose deux vues du graphe qu'il ne faut pas
# confondre :
#   - "paths" (_computeDexPaths) : une estimation Dijkstra tous-DEX-vers-
#     tous-DEX, sur edge.cost seul, indépendante du flot réellement choisi —
#     purement informatif pour le panel de la UI.
#   - "operations"/"journeys" (computeChosenOperations / decomposeJourneys) :
#     la reconstruction du plan RÉELLEMENT choisi par le solveur multi-
#     commodité (graph.solver.graphSolve), lu depuis edge.flow.


def _describe(node: Node) -> str:
    """Description lisible d'un node du graphe bas niveau, utilisée
    uniquement pour l'affichage des hops d'un chemin — ces nodes (Wallet,
    Deposit, ...) ne sont plus dessinés eux-mêmes (visualization/web_view.py
    ne montre qu'un node par DEX). Bridge et Swap n'ont plus de node dédié
    (voir graph.node.WalletNode) : les deux extrémités d'un hop Bridge/Swap
    sont simplement deux WalletNode, voir _hopKind pour la distinction."""
    if node.type == NodeType.SourceNode:
        return cast(SourceNode, node).dex.name
    if node.type == NodeType.Withdraw:
        n = cast(WithdrawNode, node)
        return f"{n.dex.name} withdraw ({n.stable.name})"
    if node.type == NodeType.Wallet:
        n = cast(WalletNode, node)
        return f"Wallet {n.chain.name}/{n.stable.name}"
    if node.type == NodeType.Deposit:
        n = cast(DepositNode, node)
        return f"{n.dex.name} deposit address {n.chain.name}/{n.stable.name}"
    return node.type.name


def _hopKind(edge: Edge) -> str:
    """Catégorie lisible d'un hop : _describe donne le "quoi" (les deux
    nodes) mais pas le "comment". Withdraw/Deposit ne bougent jamais de
    fonds entre deux parties tierces (comptabilité DEX<->WalletNode
    partagé) ; tout le reste est un vrai mouvement on-chain (dépôt direct
    via DEX.requiresDepositAddress=False, virement vers une adresse de
    dépôt CEX-style, bridge, swap). Bridge et Swap sont chacun UNE SEULE
    edge WalletNode -> WalletNode (voir Graph._linkBridges/_linkSwaps) :
    edge.type les distingue, plus besoin de regarder les nodes."""
    if edge.u.type == NodeType.Withdraw:
        # Le retrait devient disponible sur le WalletNode partagé de la
        # chain autorisée (voir DEX.withdrawChainByStable) : rien n'a encore
        # été envoyé nulle part de spécifique à un autre DEX.
        return "Withdraw"
    if edge.v.type == NodeType.SourceNode:
        # Crédite le solde interne du DEX destination — dépôt direct
        # (WalletNode -> SourceNode, la majorité des DEX) ou dernier hop
        # d'un dépôt CEX-style (DepositNode -> SourceNode, voir
        # DEX.requiresDepositAddress).
        return "Deposit"
    if edge.v.type == NodeType.Deposit:
        # WalletNode -> DepositNode : virement on-chain vers l'adresse de
        # dépôt propre à CE DEX (DEX.requiresDepositAddress=True, aucun DEX
        # du registre aujourd'hui), distinct du hop "Deposit" (crédit CEX)
        # qui suit.
        return "On-chain transfer"
    if edge.type == EdgeType.Swap:
        return "Swap"
    assert edge.type == EdgeType.Bridge
    return "Bridge"


_BRIDGE_PROTOCOL_LABEL: dict[BridgeProtocol, str] = {
    BridgeProtocol.ADEN_INTERNAL: "Aden internal bridge",
}


def _buildHopList(edges: list[Edge]) -> list[dict[str, Any]]:
    """Hops visibles d'un chemin/trajet (paths.hops et journeys.hops, voir
    hopHtml côté frontend). Une traversée cross-chain est UNE SEULE edge
    WalletNode -> WalletNode (voir Graph._linkBridges) — plus de fusion à
    faire ici : contrairement à l'ancien modèle à 3 edges (entrée/traversée/
    sortie), chaque edge du graphe bas niveau EST déjà exactement un hop
    utilisateur."""
    return [
        {
            "from": _describe(edge.u),
            "to": _describe(edge.v),
            "cost": _edgeCost(edge),
            "time": edge.time or 0.0,
            "type": _hopKind(edge),
            "protocol": _BRIDGE_PROTOCOL_LABEL.get(edge.bridgeProtocol) if edge.bridgeProtocol else None,
        }
        for edge in edges
        if not _isInternalHop(edge)
    ]


def _edgeCost(edge: Edge) -> float:
    """Coût affiché d'une edge : Fee(e) (edge.cost, le gas fixe de la tx —
    voir costing.computeCost) + le slippage RÉEL d'un swap au montant
    effectivement retenu par le solveur (edge.realizedSlippageUsd, voir
    graph.solver.graphSolve -> costing.computeRealizedSwapSlippageUsd), 0.0
    pour toute edge non-Swap. Jamais omis même quand infime : un vrai
    slippage nul n'est pas la même information qu'un slippage jamais coté."""
    return (edge.cost or 0.0) + (edge.realizedSlippageUsd or 0.0)


def _isInternalHop(edge: Edge) -> bool:
    """Jamais une vraie transaction on-chain distincte, mais peut porter un
    coût/délai réel (voir DEX.withdrawFeeUsd/depositDelaySeconds et
    costing.computeCost/computeDelay) : seulement caché du détail des hops
    quand ce coût/délai est nul, sinon l'utilisateur perdrait la seule trace
    visible du frais de retrait/dépôt CEX facturé sur cette DEX."""
    touchesSourceOrWithdraw = edge.u.type in (NodeType.SourceNode, NodeType.Withdraw) or edge.v.type in (
        NodeType.SourceNode,
        NodeType.Withdraw,
    )
    if not touchesSourceOrWithdraw:
        return False
    return not (edge.cost or edge.time)


def _dijkstraFromSources(
    graph: Graph, sourceIds: set[int], weight: Callable[[Edge], float]
) -> tuple[dict[int, float], dict[int, Edge]]:
    """Dijkstra multi-source classique sur `weight(edge)` — appelé une fois
    avec edge.cost (route "cheapest") et une fois avec edge.time (route
    "fastest"), voir _computeDexPaths ; ce n'est jamais qu'une estimation
    pour le panel "paths" de la UI, pas le plan du solveur (qui optimise les
    deux à la fois via λ(σ_d), voir graph.solver). Tous les `sourceIds`
    démarrent à distance 0, comme un super-source virtuel relié à chacun
    d'eux — permet de calculer en un seul passage la distance depuis
    N'IMPORTE LEQUEL des WithdrawNode d'un DEX (fongibles entre eux) vers
    tous les autres nodes.

    Retourne (dist, prevEdge) : `dist[nodeIndex]` = poids du plus court
    chemin trouvé selon `weight`, `prevEdge[nodeIndex]` = dernière arête
    empruntée pour l'atteindre (permet de reconstruire le chemin en
    remontant, voir _reconstructPath). Un node absent de `dist`/`prevEdge`
    est simplement inatteignable depuis ces sources."""
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
            newDist = d + weight(edge)
            v = edge.v.nodeIndex
            # -1e-12 : évite de repousser un node déjà settled à cause d'un
            # bruit flottant sur un coût égal (sinon un aller-retour à coût ~0
            # peut boucler indéfiniment dans le heap).
            if newDist < dist.get(v, float("inf")) - 1e-12:
                dist[v] = newDist
                prevEdge[v] = edge
                heapq.heappush(heap, (newDist, v))

    return dist, prevEdge


def _reconstructPath(prevEdge: dict[int, Edge], target: int, sourceIds: set[int]) -> list[Edge] | None:
    """Remonte `prevEdge` (produit par _dijkstraFromSources) depuis `target`
    jusqu'à atteindre un des `sourceIds`, puis inverse pour obtenir le
    chemin dans l'ordre source -> target. None si `target` n'a jamais été
    settled par Dijkstra (prevEdge n'a pas d'entrée pour lui) : inatteignable
    depuis ces sources."""
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
    """Résumé JSON-sérialisable d'un DEX, un par node dessiné côté frontend
    (le frontend n'affiche qu'un node par DEX, jamais les nodes bas niveau
    Wallet/Deposit/... — voir _describe). `sourceNodeId` sert d'id
    stable pour le frontend : c'est l'index du SourceNode de ce DEX dans
    graph.nodeList, aussi utilisé comme cible dans `paths` (_computeDexPaths)."""
    # Absent de DEX_BRANDING (nouveau DEX pas encore brandé) : le frontend
    # retombe sur son rendu par défaut (pas de logo, cercle neutre) plutôt
    # que planter — voir renderNodeVisual dans graph_template.html.
    branding = DEX_BRANDING.get(dex.name, {})
    return {
        "id": sourceNodeId,
        "name": dex.name,
        "inbalance": dex.inbalance,
        "stables": [s.name for s in dex.stables],
        "chains": [c.name for c in dex.chains],
        "withdrawBalances": {s.name: bal for s, bal in dex.withdrawBalances.items()},
        # Voir DEX.requiresSameChainWithdraw/withdrawChainByStable (ex: Aster) :
        # un solde crédité sur cette chain ne peut être retiré que vers elle,
        # affiché dans le panel "Details" (voir renderDexDetails côté frontend)
        # pour expliquer pourquoi certaines routes de retrait sont absentes.
        "withdrawChainByStable": {s.name: c.name for s, c in dex.withdrawChainByStable.items()},
        "logo": branding.get("logo"),
        "brandColor": branding.get("color"),
        # Pré-remplit le panel "Config" du frontend avec les valeurs déjà
        # connues côté Python (voir connectors.dex_operational_params) ;
        # l'utilisateur peut les affiner à la main dans ce panel.
        "operationalParams": {
            "withdrawFeeUsd": dex.withdrawFeeUsd,
            "withdrawDelaySeconds": dex.withdrawDelaySeconds,
            "depositFeeUsd": dex.depositFeeUsd,
            "depositDelaySeconds": dex.depositDelaySeconds,
        },
    }


def _routesFromSource(
    graph: Graph,
    sourceIds: set[int],
    targetIdByName: dict[str, int],
    weight: Callable[[Edge], float],
) -> dict[str, dict[str, Any]]:
    """Un Dijkstra (sur `weight`) depuis `sourceIds` vers chaque SourceNode de
    `targetIdByName`, au format {totalCost, totalTime, hops} attendu par
    `paths` — quel que soit `weight`, totalCost/totalTime sont TOUJOURS
    recalculés en sommant edge.cost/edge.time le long du chemin obtenu (pas
    lus depuis `dist`, qui n'est que le poids MINIMISÉ selon `weight`) : ça
    donne le coût réel et le temps réel de CE chemin précis, que ce soit
    celui qui minimise l'un ou l'autre (voir _computeDexPaths)."""
    dist, prevEdge = _dijkstraFromSources(graph, sourceIds, weight)
    routes: dict[str, dict[str, Any]] = {}
    for otherName, targetId in targetIdByName.items():
        if targetId not in dist:
            continue  # otherName inatteignable depuis ces sources dans ce graphe
        pathEdges = _reconstructPath(prevEdge, targetId, sourceIds)
        if pathEdges is None:
            continue
        # Les hops purement comptables (accounting withdraw/deposit CEX sans
        # coût/délai réel) sont masqués — voir _buildHopList.
        visibleHops = [edge for edge in pathEdges if not _isInternalHop(edge)]
        routes[otherName] = {
            "totalCost": sum(edge.cost or 0.0 for edge in visibleHops),
            "totalTime": sum(edge.time or 0.0 for edge in visibleHops),
            "hops": _buildHopList(pathEdges),
        }
    return routes


def _computeDexPaths(graph: Graph) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Construit les deux morceaux "statiques" de graphToDict :
      - dexNodes : un résumé par DEX (voir _dexNodeDict) ;
      - paths : pour chaque DEX `name` qui a du surplus (des WithdrawNode),
        DEUX chemins ESTIMÉS vers chacun des autres DEX (Dijkstra, voir
        _dijkstraFromSources) — "cheapest" (minimise edge.cost) et "fastest"
        (minimise edge.time), indépendamment de ce que le solveur choisit
        réellement (qui optimise un compromis des deux via λ(σ_d), voir
        graph.solver). C'est le all-pairs "et si j'envoyais de X vers Y" que
        la UI affiche au survol/sélection d'un node, PAS le plan retenu (voir
        computeChosenOperations pour ça)."""
    dexByName: dict[str, DEX] = {}
    sourceNodeIdByDex: dict[str, int] = {}
    withdrawNodeIdsByDex: dict[str, set[int]] = {}

    # Indexe chaque DEX par son SourceNode (puits de déficit, cible d'un
    # chemin) et la liste de ses WithdrawNode (sources de surplus, fongibles
    # entre eux — d'où le Dijkstra multi-source plus bas).
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
            targetIdByName = {other: id_ for other, id_ in sourceNodeIdByDex.items() if other != name}
            # Deux Dijkstra depuis les mêmes sources (tous les WithdrawNode de
            # `name`, fongibles entre eux) : un par critère à minimiser.
            # Reachability est identique pour les deux (mêmes edges, seuls les
            # poids changent), donc toujours les deux mêmes otherName atteints.
            cheapest = _routesFromSource(graph, sourceIds, targetIdByName, weight=lambda e: e.cost or 0.0)
            fastest = _routesFromSource(graph, sourceIds, targetIdByName, weight=lambda e: e.time or 0.0)
            for otherName in cheapest:
                perDex[otherName] = {"cheapest": cheapest[otherName], "fastest": fastest[otherName]}
        paths[name] = perDex

    return dexNodes, paths


def computeChosenOperations(graph: Graph) -> list[dict[str, Any]]:
    """Le plan réellement choisi par le solveur (edge.flow > 0, hors edges
    d'accounting) — par opposition à _computeDexPaths qui n'est qu'une
    estimation Dijkstra par paire, jamais le résultat réel du solveur."""
    operations = [
        {
            "from": _describe(edge.u),
            "to": _describe(edge.v),
            "amount": edge.flow,
            "cost": _edgeCost(edge),
            "time": edge.time or 0.0,
            "type": _hopKind(edge),
            "protocol": _BRIDGE_PROTOCOL_LABEL.get(edge.bridgeProtocol) if edge.bridgeProtocol else None,
        }
        for edge in graph.edgeList
        if edge.flow and edge.flow > 1e-9 and not _isInternalHop(edge)
    ]
    operations.sort(key=lambda op: op["amount"], reverse=True)
    return operations


def _journeyDict(journey: Journey) -> dict[str, Any]:
    """Sérialise un Journey (trajet DEX -> DEX reconstruit depuis edge.flow
    par visualization.journeys.decomposeJourneys) au même format `hops` que
    _computeDexPaths, pour que le frontend puisse réutiliser le même
    renderer. `journey.plausible` signale un trajet où l'appariement source/
    destination n'est qu'UNE explication valide parmi d'autres (flot
    multi-commodité agrégé sur une arête partagée, voir Journey docstring)."""
    visibleHops = [edge for edge in journey.hops if not _isInternalHop(edge)]
    return {
        "from": journey.fromDex,
        "to": journey.toDex,
        "amount": journey.amount,
        "plausible": journey.plausible,
        "totalCost": sum(_edgeCost(edge) for edge in visibleHops),
        # Somme des délais des hops traversés séquentiellement le long de ce
        # trajet (voir la même remarque dans _computeDexPaths).
        "totalTime": sum(edge.time or 0.0 for edge in visibleHops),
        "hops": _buildHopList(journey.hops),
    }


def graphToDict(graph: Graph, timeWeightParams: TimeWeightParams | None = None) -> dict[str, Any]:
    """Point d'entrée unique assemblant tout ce que graph_template.html
    consomme (voir renderGraphHtml) :
      - dexNodes : un node par DEX à dessiner ;
      - paths : estimation Dijkstra tous-DEX-vers-tous-DEX (informatif) ;
      - operations : le plan réellement choisi par le solveur, agrégé par
        arête (voir computeChosenOperations) ;
      - journeys : ce même plan redécomposé en trajets DEX -> DEX individuels
        (voir visualization.journeys.decomposeJourneys) ;
      - timeWeight : les paramètres λ(σ_d) réellement utilisés par le solveur
        (voir graph.urgency.TimeWeightParams), affichés tels quels dans le
        panneau "Cost model" du frontend — None si le solveur n'a pas tourné
        avec un time-weighting (pas de timeWeightParams fourni)."""
    dexNodes, paths = _computeDexPaths(graph)
    journeys = [_journeyDict(j) for j in decomposeJourneys(graph)]
    timeWeight = (
        {
            "lambdaMin": timeWeightParams.lambda_min,
            "lambdaMax": timeWeightParams.lambda_max,
            "k": timeWeightParams.k,
            "epsilon": timeWeightParams.epsilon,
        }
        if timeWeightParams is not None
        else None
    )
    return {
        "dexNodes": dexNodes,
        "paths": paths,
        "operations": computeChosenOperations(graph),
        "journeys": journeys,
        "timeWeight": timeWeight,
    }


def renderGraphHtml(graph: Graph, outputPath: str = "graph.html", timeWeightParams: TimeWeightParams | None = None) -> None:
    """Injecte graphToDict(graph) en JSON dans le template statique (simple
    remplacement de texte sur le placeholder __GRAPH_DATA_JSON__, voir
    visualization/web/graph_template.html) et écrit le résultat, un fichier
    HTML autonome sans serveur ni build step."""
    data = graphToDict(graph, timeWeightParams)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__GRAPH_DATA_JSON__", json.dumps(data))
    Path(outputPath).write_text(html, encoding="utf-8")


def _formatDuration(seconds: float) -> str:
    totalSeconds = round(seconds)
    minutes, secs = divmod(totalSeconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def formatOperationsText(graph: Graph) -> str:
    """Rendu texte brut de computeChosenOperations (même plan que le "operations"
    de graphToDict), pour operations.txt — un log lisible sans ouvrir de
    navigateur, en complément de graph.html."""
    operations = computeChosenOperations(graph)
    if not operations:
        return (
            "No rebalancing operations chosen by the solver — every DEX balance "
            "is currently zero (target/balance data isn't wired in yet).\n"
        )

    totalAmount = sum(op["amount"] for op in operations)
    totalCost = sum(op["cost"] for op in operations)
    # Approximation volontaire : le plus long délai parmi tous les hops
    # choisis, pas la somme des hops le long d'une chaîne (voir la même
    # remarque dans graph_template.html:renderOperations).
    totalTime = max(op["time"] for op in operations)
    lines = [
        f"{len(operations)} operations — ${totalAmount:.2f} moved — ${totalCost:.4f} total fees — "
        f"~{_formatDuration(totalTime)} to complete (longest single hop)",
        "",
    ]
    typeWidth = max(len(op["type"]) for op in operations)
    for op in operations:
        protocolSuffix = f"  via {op['protocol']}" if op.get("protocol") else ""
        lines.append(
            f"[{op['type']:<{typeWidth}}] {op['from']} -> {op['to']}    amount=${op['amount']:.2f}  "
            f"cost=${op['cost']:.4f}  delay={op['time']:.0f}s{protocolSuffix}"
        )
    return "\n".join(lines) + "\n"


def writeOperationsText(graph: Graph, outputPath: str = "operations.txt") -> None:
    Path(outputPath).write_text(formatOperationsText(graph), encoding="utf-8")
