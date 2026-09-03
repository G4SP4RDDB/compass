from graph.node import Node
from graph.structures.bridges import BridgeProtocol


class Edge:
    def __init__(self, u: Node, v: Node, bridgeProtocol: BridgeProtocol | None = None):
        self.u = u
        self.v = v
        self.cost: float | None = None
        self.capacity: float | None = None
        self.flow: float | None = None
        # Time(e) : latence absolue en secondes attribuée à cette arête, voir
        # costing.computeDelay. Utilisée dans w(e, d) = Fee(e) + λ(σ_d)·Time(e).
        self.time: float | None = None
        # Seulement pour une edge Bridge->Bridge : quel protocole dessert
        # cette route précise parmi ceux retournés par availableBridgeProtocols
        # (une edge par protocole, entre les deux mêmes BridgeNode). None pour
        # tout autre type d'edge.
        self.bridgeProtocol = bridgeProtocol
