from abc import ABC
from enum import Enum, auto

from graph.structures.DEXes import DEX, Chain, Stable


class NodeType(Enum):
    Deposit = auto()
    Swap = auto()
    Bridge = auto()
    SourceNode = auto()


class Node(ABC):
    def __init__(self, type: NodeType, nodeIndex: int):
        self.type = type
        self.nodeIndex = nodeIndex



class SourceNode (Node):
    def __init__(self,balance: int,nodeIndex,dex:DEX):
        super().__init__(NodeType.SourceNode,nodeIndex)
        self.balance = balance
        self.dex = dex
        

class DepositNode(Node):
    def __init__(self,chain: Chain,nodeIndex:int,dex:DEX):
        super().__init__(NodeType.Deposit,nodeIndex)
        self.chain = chain
        self.dex = dex


class SwapNode(Node):
    def __init__(self, chain: Chain, stableIn: Stable, stableOut: Stable,nodeIndex:int):
        super().__init__(NodeType.Swap,nodeIndex)
        self.chain = chain
        self.stableIn = stableIn
        self.stableOut = stableOut


class BridgeNode(Node):
    def __init__(self, chain: Chain, stable: Stable, nodeIndex: int):
        super().__init__(NodeType.Bridge, nodeIndex)
        self.chain = chain
        self.stable = stable

