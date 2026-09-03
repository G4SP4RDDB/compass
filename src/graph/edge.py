from graph.node import Node
from graph.structures.bridges import BridgeProtocol


class Edge:
    def __init__(self, u: Node, v: Node, bridgeProtocol: BridgeProtocol | None = None):
        self.u = u
        self.v = v
        self.cost: float | None = None
        self.capacity: float | None = None
        self.flow: float | None = None
        # Slippage RÉEL d'un swap (v.type==Swap uniquement) au montant EXACT
        # retenu par le solveur (self.flow), voir graph.solver.graphSolve ->
        # costing.computeRealizedSwapSlippageUsd. Connu seulement après
        # résolution -- toujours 0.0 avant, ou pour toute edge non-Swap.
        # Distinct de self.cost (qui ne porte que le gas fixe de la tx, voir
        # costing.computeCost) pour ne jamais l'accumuler d'un re-solve à
        # l'autre (voir visualization/server.py POST /api/solve, qui résout
        # plusieurs fois le même Graph) : recalculé/écrasé à chaque résolution.
        self.realizedSlippageUsd: float = 0.0
        # Time(e) : latence absolue en secondes attribuée à cette arête, voir
        # costing.computeDelay. Utilisée dans w(e, d) = Fee(e) + λ(σ_d)·Time(e).
        self.time: float | None = None
        # Seulement pour une edge Bridge->Bridge : quel protocole dessert
        # cette route précise parmi ceux retournés par availableBridgeProtocols
        # (une edge par protocole, entre les deux mêmes BridgeNode). None pour
        # tout autre type d'edge.
        self.bridgeProtocol = bridgeProtocol
