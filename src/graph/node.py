from enum import Enum
from graph.structures.DEXes import DEX, Chain
from typing import Sequence
from graph.structures.DEXes import Stable
from enum import Enum,auto
from abc import ABC, abstractmethod



class Node(ABC):
    def __init__(self,type:NodeType,nodeIndex: int):
        self.type = type
  


class SourceNode (Node):
    def __init__(self,balance: int,nodeIndex,dex:DEX):
        super().__init__(type,NodeType.SourceNode,nodeIndex)
        self.balance = balance
        self.dex = dex
        

class DepositNode(Node):
    def __init__(self,chain: Chain,nodeIndex:int,dex:DEX):
        super.__init__(self,type,NodeType.Deposit,nodeIndex)
        self.chain = chain
        self.dex = dex


class SwapNode(Node):
    def __init__(self, stableIn: Stable, stableOut: Stable,nodeIndex:int):
        super().__init__(NodeType.Swap,nodeIndex)
        self.stableIn = stableIn
        self.stableOut = stableOut


class BridgeNode(Node):
    def __init__(self, type: NodeType,chainIn:Chain,chainOut: Chain, supportedStable: Stable):
        super().__init__(NodeType.Bridge, )
        self.chainIn = chainIn
        self.chainOut = chainOut
        self.supportedStable = supportedStable

class NodeType(Enum):
    Deposit = auto()
    Swap = auto()
    Bridge = auto()
    SourceNode = auto()

