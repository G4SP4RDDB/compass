from enum import Enum
from src.DEXes import DEX, Chain
from typing import Sequence
from DEXes import Stable
from enum import Enum,auto
from abc import ABC, abstractmethod



class Node(ABC):
    def __init__(self,type:NodeType,neighbours: list[Node]):
        self.type = type
        self.neighbours = neighbours
    def getNeighbours(self) -> list[Node]:
            return self.neighbours
    def setNeighbours(self,n) -> None:
        self.neighbours = n

class DepositNode(Base):
    def __init__(self,dex: DEX,neighbours: list[Node],chain: Chain ):
        super.__init__(self,type,NodeType.Deposit,neighbours)
        self.dex = dex
        self.chain = chain



class SwapNode(Base):
    def __init__(self,  neighbours,stableIn: Stable, stableOut: Stable):
        super().__init__(NodeType.Swap, neighbours)
        self.stableIn = stableIn
        self.stableOut = stableOut


class BridgeNode(Base):
    def __init__(self, type: NodeType, neighbours: list[BaseNode],chainIn:Chain,chainOut: Chain, supportedStable: Stable):
        super().__init__(NodeType.Bridge, neighbours)
        self.chainIn = chainIn
        self.chainOut = chainOut
        self.supportedStable = supportedStable

class NodeType(Enum):
    Deposit = auto()
    Swap = auto()
    Bridge = auto()

