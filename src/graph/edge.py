from graph.node import Node


class Edge:
    def __init__(self, u: Node, v: Node):
        self.u = u
        self.v = v
        self.cost: float | None = None
        self.capacity: float | None = None
        self.flow: float | None = None
