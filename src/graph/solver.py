from ortools.sat.python import cp_model

from connectors.exceptions import ConnectorError
from graph import costing
from graph.edge import Edge
from graph.graph import Graph
from graph.node import NodeType

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
) -> tuple[cp_model.CpModel, list[cp_model.IntVar], list[cp_model.IntVar | None]]:
    graph.computeAllCapacities()

    model = cp_model.CpModel()
    flowVars: list[cp_model.IntVar] = []
    usedVars: list[cp_model.IntVar | None] = []
    swapCostVars: list[cp_model.IntVar] = []

    for i, edge in enumerate(graph.edgeList):
        capacityScaled = _scaledInt(edge.capacity)
        flow = model.NewIntVar(0, capacityScaled, f"flow_{i}")
        flowVars.append(flow)

        if edge.v.type == NodeType.Swap:
            # coût variable (slippage, fonction du montant) en plus du gas
            # fee fixe géré ci-dessous comme toute autre arête : voir _addSwapCost
            swapCostVars.append(_addSwapCost(model, edge, flow, i))

        costScaled = _scaledInt(edge.cost)
        if costScaled == 0:
            usedVars.append(None)  # arête virtuelle (SourceNode), pas de frais à gater
            continue

        # frais fixe : payé une seule fois si l'arête est utilisée, quel que
        # soit le montant transporté (voir la discussion fixed-charge flow).
        used = model.NewBoolVar(f"used_{i}")
        model.Add(flow <= capacityScaled * used)
        usedVars.append(used)

    _addFlowConservation(model, graph, flowVars)

    model.Minimize(
        sum(
            _scaledInt(edge.cost) * used
            for edge, used in zip(graph.edgeList, usedVars)
            if used is not None
        )
        + sum(swapCostVars)
    )

    return model, flowVars, usedVars


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


def _addFlowConservation(model: cp_model.CpModel, graph: Graph, flowVars: list[cp_model.IntVar]) -> None:
    outflowByNode: dict[int, list[cp_model.IntVar]] = {node.nodeIndex: [] for node in graph.nodeList}
    inflowByNode: dict[int, list[cp_model.IntVar]] = {node.nodeIndex: [] for node in graph.nodeList}
    for edge, flow in zip(graph.edgeList, flowVars):
        outflowByNode[edge.u.nodeIndex].append(flow)
        inflowByNode[edge.v.nodeIndex].append(flow)

    for node in graph.nodeList:
        # SourceNode (déficit <= 0) et WithdrawNode (surplus >= 0) sont les
        # deux seuls types de nodes avec un supply non nul ; tout le reste
        # (Deposit, Bridge, Swap) est un pur nœud de transit.
        supply = _scaledInt(node.balance) if node.type in (NodeType.SourceNode, NodeType.Withdraw) else 0
        model.Add(sum(outflowByNode[node.nodeIndex]) - sum(inflowByNode[node.nodeIndex]) == supply)


def graphSolve(graph: Graph) -> cp_model.CpSolver:
    model, flowVars, _ = buildModel(graph)

    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"Le solveur n'a pas trouvé de solution (status={solver.StatusName(status)})"
        )

    for edge, flow in zip(graph.edgeList, flowVars):
        edge.flow = solver.Value(flow) / SCALE

    return solver


def totalCostUsd(solver: cp_model.CpSolver) -> float:
    return solver.ObjectiveValue() / SCALE
