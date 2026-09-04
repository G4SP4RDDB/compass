from enum import Enum, auto

from graph.node import Node
from graph.structures.bridges import BridgeProtocol


class EdgeType(Enum):
    """Discrimine les edges WalletNode -> WalletNode, seules ambiguës une
    fois que Bridge et Swap n'ont plus de node dédié (voir graph.node.WalletNode) :
    même chain/stable différent -> Swap, chain différente/même stable ->
    Bridge. Toute autre edge (Withdraw, Deposit, SourceNode, ...) reste
    disambiguée par le NodeType de ses extrémités comme avant, `type` y vaut
    None."""

    Bridge = auto()
    Swap = auto()


class Edge:
    def __init__(self, u: Node, v: Node, type: EdgeType | None = None, bridgeProtocol: BridgeProtocol | None = None):
        self.u = u
        self.v = v
        self.type = type
        self.cost: float | None = None
        self.capacity: float | None = None
        self.flow: float | None = None
        # Slippage RÉEL d'un swap (type==EdgeType.Swap uniquement) au montant EXACT
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
        # Seulement pour une edge type==EdgeType.Bridge : quel protocole
        # dessert cette route précise parmi ceux retournés par
        # availableBridgeProtocols (une edge par protocole, entre les deux
        # mêmes WalletNode). None pour tout autre type d'edge.
        self.bridgeProtocol = bridgeProtocol
