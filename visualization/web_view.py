from __future__ import annotations

import heapq
import json
from pathlib import Path
from typing import Any, cast

from graph.edge import Edge
from graph.graph import Graph
from graph.node import BridgeNode, DepositNode, Node, NodeType, SourceNode, SwapNode, WithdrawNode
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


def _hopKind(edge: Edge) -> str:
    """Catégorie lisible d'un hop : _describe donne le "quoi" (les deux nodes)
    mais pas le "comment", ce qui rend indistinguables un simple traitement
    retrait/dépôt CÔTÉ D'UN SEUL DEX (Withdraw/Deposit, jamais de mouvement de
    fonds entre deux parties) d'un vrai mouvement ON-CHAIN entre deux DEXes
    (transfert direct, bridge, swap) — voir le "Gate deposit -> Aden deposit"
    signalé comme ambigu dans le plan choisi."""
    if edge.u.type == NodeType.Withdraw:
        # Toujours même DEX des deux côtés (voir Graph._addSourceAndDepositNodes) :
        # le retrait devient disponible sur une des chains de CE DEX, rien n'a
        # encore quitté le DEX.
        return "Withdraw"
    if edge.v.type == NodeType.SourceNode:
        # Le dépôt reçu est crédité au solde interne du DEX, toujours même DEX
        # des deux côtés.
        return "Deposit"
    if edge.u.type == NodeType.Deposit and edge.v.type == NodeType.Deposit:
        # Même chain/stable, deux DEX différents, sans bridge : c'est le cas
        # qui prêtait à confusion, un vrai virement on-chain entre deux
        # adresses de dépôt.
        return "On-chain transfer"
    if edge.u.type == NodeType.Swap or edge.v.type == NodeType.Swap:
        return "Swap"
    return "Bridge"  # Deposit<->Bridge (entrée/sortie) ou Bridge<->Bridge (cross-chain)


_BRIDGE_PROTOCOL_LABEL: dict[BridgeProtocol, str] = {
    BridgeProtocol.GENERIC: "Generic",
    BridgeProtocol.CCTP_V1: "CCTP V1",
    BridgeProtocol.CCTP_V2: "CCTP V2 (Fast Transfer)",
}


def _isBridgeCrossing(edge: Edge) -> bool:
    """La SEULE edge d'un hop "Bridge" qui traverse réellement une chain
    (Bridge<->Bridge, voir Graph._linkBridges) — Deposit<->Bridge n'est
    qu'une entrée/sortie sur la MÊME chain. C'est aussi la seule à porter
    edge.bridgeProtocol non-None : un hop fusionné ne peut donc jamais
    contenir plus d'une traversée sans perdre le protocole de l'une
    d'elles (voir _buildHopList)."""
    return edge.u.type == NodeType.Bridge and edge.v.type == NodeType.Bridge


def _buildHopList(edges: list[Edge]) -> list[dict[str, Any]]:
    """Hops visibles d'un chemin/trajet (paths.hops et journeys.hops, voir
    hopHtml côté frontend). Une traversée cross-chain est 3 edges
    consécutives dans le graphe bas niveau (Deposit->Bridge pour entrer,
    Bridge->Bridge pour traverser, Bridge->Deposit pour sortir — voir
    Graph._linkBridges) mais UN SEUL évènement du point de vue utilisateur :
    on les fusionne ici en un seul hop "Bridge" (coût/délai sommés), sinon la
    même traversée de chain apparaît comme 3 lignes distinctes dans l'UI pour
    ce qui n'est en réalité qu'un seul changement de blockchain.

    Une route peut cependant enchaîner PLUSIEURS traversées distinctes,
    relayées via une chain intermédiaire sans jamais repasser par un Deposit
    (Bridge->Bridge->Bridge, chaque traversée pouvant avoir SON PROPRE
    protocole — ex: Solana->Avalanche en GENERIC faute de support CCTP sur
    Solana, puis Avalanche->Ethereum en CCTP_V1). Fusionner tout le run en un
    seul hop écraserait silencieusement le protocole/coût/délai de la
    première traversée avec ceux de la dernière : un groupe fusionné ne
    contient donc jamais plus d'une edge _isBridgeCrossing, une deuxième
    traversée démarre toujours un nouveau hop."""
    visibleEdges = [edge for edge in edges if not _isInternalHop(edge)]

    # Le node cible d'un Withdraw (Withdraw->Deposit, même DEX, voir
    # Graph._addSourceAndDepositNodes) n'est PAS une adresse appartenant à ce
    # DEX : ce sont les fonds de l'UTILISATEUR, tout juste rendus liquides
    # sur cette chain, pas encore envoyés nulle part. _describe() le nomme
    # pourtant "<DEX> deposit <chain>/<stable>" comme n'importe quelle autre
    # DepositNode ; dès que ce même node réapparaît comme point de départ du
    # hop suivant (ex: fusionné en "Deposit" par le bloc ci-dessous), ça lit
    # comme si CE DEX déposait lui-même chez le DEX destination. On le
    # désigne donc "Your wallet" plutôt que par le nom du DEX, PARTOUT où ce
    # node précis apparaît (identité d'objet, pas juste même chain/stable).
    landingNode: Node | None = None
    if visibleEdges and visibleEdges[0].u.type == NodeType.Withdraw:
        landingNode = visibleEdges[0].v

    def describe(node: Node) -> str:
        if landingNode is not None and node is landingNode:
            n = cast(DepositNode, node)
            return f"Your wallet ({n.chain.name}/{n.stable.name})"
        return _describe(node)

    hops: list[dict[str, Any]] = []
    i = 0
    while i < len(visibleEdges):
        kind = _hopKind(visibleEdges[i])
        if kind != "Bridge":
            edge = visibleEdges[i]
            hops.append(
                {
                    "from": describe(edge.u),
                    "to": describe(edge.v),
                    "cost": edge.cost,
                    "time": edge.time or 0.0,
                    "type": kind,
                    "protocol": None,
                }
            )
            i += 1
            continue

        j = i
        cost = 0.0
        time = 0.0
        protocol: BridgeProtocol | None = None
        seenCrossing = False
        while j < len(visibleEdges) and _hopKind(visibleEdges[j]) == "Bridge":
            hopEdge = visibleEdges[j]
            if _isBridgeCrossing(hopEdge):
                if seenCrossing:
                    break  # deuxième traversée : hop suivant, pas celui-ci
                seenCrossing = True
                protocol = hopEdge.bridgeProtocol
            cost += hopEdge.cost or 0.0
            time += hopEdge.time or 0.0
            j += 1
        hops.append(
            {
                "from": describe(visibleEdges[i].u),
                "to": describe(visibleEdges[j - 1].v),
                "cost": cost,
                "time": time,
                "type": "Bridge",
                "protocol": _BRIDGE_PROTOCOL_LABEL.get(protocol) if protocol else None,
            }
        )
        i = j

    # Le dernier hop d'un trajet est TOUJOURS Deposit->SourceNode (voir
    # Graph._addSourceAndDepositNodes) : le DEX destination reconnaît/crédite
    # les fonds arrivés. Il n'existe pas d'adresse "appartenant" à ce DEX
    # distincte de l'adresse de l'utilisateur — déposer chez lui, c'est
    # précisément lui envoyer les fonds ET les voir crédités, UNE seule
    # action de son point de vue, pas deux. On fusionne donc ce hop avec le
    # mouvement qui l'a amené (On-chain transfer ou Bridge, coût/délai
    # sommés, protocole conservé), sauf s'il suit directement un Withdraw :
    # un DEX qui se recrédite lui-même (retrait puis dépôt du MÊME DEX)
    # n'a aucun mouvement entre les deux à fusionner, "Withdraw" et
    # "Deposit" sont déjà les 2 seules actions réelles.
    if len(hops) >= 2 and hops[-1]["type"] == "Deposit" and hops[-2]["type"] != "Withdraw":
        finalDeposit = hops.pop()
        merged = hops[-1]
        merged["cost"] += finalDeposit["cost"]
        merged["time"] += finalDeposit["time"]
        merged["type"] = "Deposit"

    return hops


def _isInternalHop(edge: Edge) -> bool:
    """Jamais une vraie transaction on-chain distincte, mais peut porter un
    coût/délai réel (voir DEX.withdrawFeeUsd/depositDelaySeconds et
    costing.computeCost/computeDelay) : seulement caché du détail des hops
    quand ce coût/délai est nul, sinon l'utilisateur perdrait la seule trace
    visible du frais de retrait/dépôt CEX facturé sur cette DEX."""
    if edge.u.type == NodeType.Swap:
        # sortie du swap : toujours pure comptabilité, déjà facturée à l'entrée.
        return True
    touchesSourceOrWithdraw = edge.u.type in (NodeType.SourceNode, NodeType.Withdraw) or edge.v.type in (
        NodeType.SourceNode,
        NodeType.Withdraw,
    )
    if not touchesSourceOrWithdraw:
        return False
    return not (edge.cost or edge.time)


def _dijkstraFromSources(graph: Graph, sourceIds: set[int]) -> tuple[dict[int, float], dict[int, Edge]]:
    """Dijkstra multi-source classique sur edge.cost (edge.time ignoré ici :
    ce n'est qu'une estimation de coût pour le panel "paths" de la UI, pas le
    plan du solveur). Tous les `sourceIds` démarrent à distance 0, comme un
    super-source virtuel relié à chacun d'eux — permet de calculer en un seul
    passage la distance depuis N'IMPORTE LEQUEL des WithdrawNode d'un DEX
    (fongibles entre eux) vers tous les autres nodes.

    Retourne (dist, prevEdge) : `dist[nodeIndex]` = coût du plus court chemin
    trouvé, `prevEdge[nodeIndex]` = dernière arête empruntée pour l'atteindre
    (permet de reconstruire le chemin en remontant, voir _reconstructPath).
    Un node absent de `dist`/`prevEdge` est simplement inatteignable depuis
    ces sources."""
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
    Deposit/Bridge/Swap/... — voir _describe). `sourceNodeId` sert d'id
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


def _computeDexPaths(graph: Graph) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Construit les deux morceaux "statiques" de graphToDict :
      - dexNodes : un résumé par DEX (voir _dexNodeDict) ;
      - paths : pour chaque DEX `name` qui a du surplus (des WithdrawNode),
        le moins cher chemin ESTIMÉ (Dijkstra sur edge.cost, voir
        _dijkstraFromSources) vers chacun des autres DEX — indépendamment de
        ce que le solveur choisit réellement. C'est le all-pairs "et si
        j'envoyais de X vers Y" que la UI affiche au survol/sélection d'un
        node, PAS le plan retenu (voir computeChosenOperations pour ça)."""
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
            # Un seul Dijkstra pour TOUTES les destinations à la fois (depuis
            # tous les WithdrawNode de `name`), plutôt qu'un par paire.
            dist, prevEdge = _dijkstraFromSources(graph, sourceIds)
            for otherName in dexByName:
                if otherName == name:
                    continue
                targetId = sourceNodeIdByDex[otherName]
                if targetId not in dist:
                    continue  # otherName inatteignable depuis name dans ce graphe
                pathEdges = _reconstructPath(prevEdge, targetId, sourceIds)
                if pathEdges is None:
                    continue
                visibleHops = [edge for edge in pathEdges if not _isInternalHop(edge)]
                perDex[otherName] = {
                    "totalCost": dist[targetId],
                    # Hops sequentiels le long d'un même chemin : le temps total
                    # est la SOMME des délais, contrairement à computeChosenOperations
                    # où plusieurs opérations tournent en parallèle sur des paires
                    # différentes (voir le max() dans formatOperationsText).
                    "totalTime": sum(edge.time or 0.0 for edge in visibleHops),
                    # Les hops purement comptables (accounting withdraw/deposit
                    # CEX sans coût/délai réel, sortie de swap) sont masqués,
                    # et une traversée cross-chain (3 edges bas niveau) est
                    # fusionnée en un seul hop "Bridge" : voir _buildHopList.
                    "hops": _buildHopList(pathEdges),
                }
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
            "cost": edge.cost,
            "time": edge.time or 0.0,
            "type": _hopKind(edge),
            # Edge-level view (unlike paths/journeys, see _buildHopList) : no
            # merging of entry/cross/exit hops, so the protocol only ever
            # shows on the actual Bridge<->Bridge edge that carries it.
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
        "totalCost": sum(edge.cost or 0.0 for edge in visibleHops),
        # Somme des délais des hops traversés séquentiellement le long de ce
        # trajet (voir la même remarque dans _computeDexPaths).
        "totalTime": sum(edge.time or 0.0 for edge in visibleHops),
        # Une traversée cross-chain (3 edges bas niveau) est fusionnée en un
        # seul hop "Bridge" ici aussi : voir _buildHopList.
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
