from typing import cast

from ortools.sat.python import cp_model

from connectors.exceptions import ConnectorError
from graph import costing
from graph.edge import Edge
from graph.graph import Graph
from graph.node import NodeType, SourceNode, WithdrawNode
from graph.structures.DEXes import DEX
from graph.urgency import TimeWeightParams, computeDexUrgencySigma, computeTimeWeight

# CP-SAT veut des coefficients entiers ; costing.py et Graph._edgeCapacity
# travaillent en dollars flottants, donc la conversion en entiers ne se fait
# qu'ici, à la frontière avec le solveur.
SCALE = 1_000_000


def _scaledInt(value: float | None) -> int:
    if value is None:
        raise ValueError("cost/capacity doit être calculé avant de construire le modèle CP-SAT")
    return round(value * SCALE)


def buildModel(
    graph: Graph,
    timeWeightParams: TimeWeightParams,
) -> tuple[cp_model.CpModel, list[cp_model.IntVar], list[cp_model.IntVar | None], list[DEX]]:
    """Construit le modèle CP-SAT de flot multi-commodité : une commodité par
    DEX destination déficitaire (voir Graph.deficitDexes). Le coût d'une arête
    e pour la commodité destinée au DEX d est
        w(e, d) = Fee(e) + λ(σ_d) · Time(e)
    Fee(e) est un frais fixe payé une seule fois par arête si elle est
    utilisée par au moins une commodité (transaction on-chain partagée).
    λ(σ_d)·Time(e) est facturé par commodité utilisant l'arête (la latence
    ajoutée retarde chaque destination qui emprunte cette arête, même si la
    transaction elle-même est mutualisée).
    """
    graph.computeAllCapacities()

    commodities = graph.deficitDexes()
    sigmaByDex = {dex: computeDexUrgencySigma(dex) for dex in commodities}
    timeWeightByDex = {dex: computeTimeWeight(sigmaByDex[dex], timeWeightParams) for dex in commodities}

    model = cp_model.CpModel()

    # flow[i][d] : flot sur l'arête i attribuable à la commodité d.
    flowVars: list[list[cp_model.IntVar]] = []
    totalFlowVars: list[cp_model.IntVar] = []
    usedVars: list[cp_model.IntVar | None] = []
    swapCostVars: list[cp_model.IntVar] = []
    objectiveTerms: list[cp_model.LinearExprT] = []

    for i, edge in enumerate(graph.edgeList):
        capacityScaled = _scaledInt(edge.capacity)

        perCommodityFlows = [
            model.NewIntVar(0, capacityScaled, f"flow_{i}_{d}") for d in range(len(commodities))
        ]
        flowVars.append(perCommodityFlows)

        totalFlow = model.NewIntVar(0, capacityScaled, f"flowTotal_{i}")
        model.Add(totalFlow == sum(perCommodityFlows))
        totalFlowVars.append(totalFlow)

        if edge.v.type == NodeType.Swap:
            # coût variable (slippage, fonction du montant total transporté),
            # en plus du gas fee fixe géré ci-dessous comme toute autre arête.
            swapCostVars.append(_addSwapCost(model, edge, totalFlow, i))

        feeScaled = _scaledInt(edge.cost)
        edgeTime = edge.time or 0.0

        if feeScaled == 0:
            usedVars.append(None)  # arête virtuelle (SourceNode), pas de frais à gater
        else:
            # frais fixe : payé une seule fois si l'arête est utilisée, quel
            # que soit le montant transporté (voir la discussion fixed-charge
            # flow), partagé entre toutes les commodités qui l'empruntent.
            used = model.NewBoolVar(f"used_{i}")
            model.Add(totalFlow <= capacityScaled * used)
            usedVars.append(used)
            objectiveTerms.append(feeScaled * used)

        if edgeTime > 0:
            for d, dex in enumerate(commodities):
                timeCostScaled = _scaledInt(timeWeightByDex[dex] * edgeTime)
                if timeCostScaled == 0:
                    continue
                commodityUsed = model.NewBoolVar(f"usedTime_{i}_{d}")
                model.Add(perCommodityFlows[d] <= capacityScaled * commodityUsed)
                objectiveTerms.append(timeCostScaled * commodityUsed)

    _addFlowConservation(model, graph, flowVars, commodities)

    model.Minimize(sum(objectiveTerms) + sum(swapCostVars))

    return model, totalFlowVars, usedVars, commodities


def _addSwapCost(model: cp_model.CpModel, edge: Edge, flow: cp_model.IntVar, index: int) -> cp_model.IntVar:
    try:
        breakpoints = costing.computeSwapCostBreakpoints(edge)
    except ConnectorError:
        # Pas encore de données de pricing pour cette chain (aujourd'hui,
        # seul Ethereum a un subgraph Uniswap vérifié — voir
        # UNISWAP_V2_SUBGRAPH_ID_BY_CHAIN) : arête désactivée plutôt que de
        # planter la construction du modèle pour tout le reste du graphe.
        model.Add(flow == 0)
        return model.NewIntVar(0, 0, f"swapCost_{index}")

    points = [(_scaledInt(x), _scaledInt(y)) for x, y in breakpoints]

    swapCost = model.NewIntVar(0, points[-1][1], f"swapCost_{index}")

    # épigraphe d'une fonction convexe approximée par segments : pas besoin de
    # booléen par segment, minimiser l'objectif pousse swapCost vers la borne
    # la plus serrée. Écrit en produits croisés (pas de division) pour garder
    # des coefficients CP-SAT entiers exacts.
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dx = x1 - x0
        dy = y1 - y0
        model.Add(swapCost * dx - flow * dy >= y0 * dx - x0 * dy)

    return swapCost


def _addFlowConservation(
    model: cp_model.CpModel,
    graph: Graph,
    flowVarsByEdge: list[list[cp_model.IntVar]],
    commodities: list[DEX],
) -> None:
    numCommodities = len(commodities)
    outflowByNode: dict[int, list[list[cp_model.IntVar]]] = {
        node.nodeIndex: [[] for _ in range(numCommodities)] for node in graph.nodeList
    }
    inflowByNode: dict[int, list[list[cp_model.IntVar]]] = {
        node.nodeIndex: [[] for _ in range(numCommodities)] for node in graph.nodeList
    }
    for edge, perCommodityFlows in zip(graph.edgeList, flowVarsByEdge):
        for d, flow in enumerate(perCommodityFlows):
            outflowByNode[edge.u.nodeIndex][d].append(flow)
            inflowByNode[edge.v.nodeIndex][d].append(flow)

    for node in graph.nodeList:
        if node.type == NodeType.Withdraw:
            # Source pure et fongible entre commodités : n'importe quel DEX
            # déficitaire peut consommer ce surplus, seule la somme totale
            # évacuée est contrainte (pas de répartition imposée par nœud).
            totalOut = sum(sum(outflowByNode[node.nodeIndex][d]) for d in range(numCommodities))
            model.Add(totalOut == _scaledInt(cast(WithdrawNode, node).balance))
            continue

        # SourceNode (déficit <= 0) et WithdrawNode (traité ci-dessus) sont
        # les deux seuls types de nodes avec un supply non nul ; tout le
        # reste (Deposit, Bridge, Swap) est un pur nœud de transit pour
        # chaque commodité. Le supply d'un SourceNode n'est non nul que pour
        # la commodité de son propre DEX (une commodité par DEX déficitaire).
        for d, dex in enumerate(commodities):
            supply = 0
            if node.type == NodeType.SourceNode and cast(SourceNode, node).dex is dex:
                supply = _scaledInt(cast(SourceNode, node).balance)
            model.Add(
                sum(outflowByNode[node.nodeIndex][d]) - sum(inflowByNode[node.nodeIndex][d]) == supply
            )


def graphSolve(graph: Graph, timeWeightParams: TimeWeightParams) -> cp_model.CpSolver:
    model, totalFlowVars, _, _ = buildModel(graph, timeWeightParams)

    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"Le solveur n'a pas trouvé de solution (status={solver.StatusName(status)})"
        )

    for edge, flow in zip(graph.edgeList, totalFlowVars):
        edge.flow = solver.Value(flow) / SCALE

    _applyRealizedSwapSlippage(graph)

    return solver


def _applyRealizedSwapSlippage(graph: Graph) -> None:
    """edge.cost d'une edge Swap (voir costing.computeCost) ne porte que le
    gas fixe de la tx ; le slippage dépend du montant réellement échangé,
    connu seulement APRÈS résolution (edge.flow). buildModel/_addSwapCost
    l'a déjà approximé par segments pour piloter le CHOIX du solveur (voir
    costing.computeSwapCostBreakpoints), mais cette approximation reste
    interne au modèle CP-SAT. On cote ici, une seule fois par edge Swap
    utilisée, le montant EXACT retenu (pas les points d'échantillonnage),
    et on l'écrase dans edge.realizedSlippageUsd — jamais accumulé, ce
    Graph pouvant être re-résolu plusieurs fois sur un nouveau k (voir
    visualization/server.py POST /api/solve)."""
    for edge in graph.edgeList:
        if edge.v.type != NodeType.Swap:
            continue
        edge.realizedSlippageUsd = (
            costing.computeRealizedSwapSlippageUsd(edge) if edge.flow and edge.flow > 1e-9 else 0.0
        )


def totalCostUsd(solver: cp_model.CpSolver) -> float:
    return solver.ObjectiveValue() / SCALE
