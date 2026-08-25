from enum import Enum
from src.DEXes import DEX
from typing import Sequence
from DEXes import Stable
from enum import Enum,auto
from abc import ABC, abstractmethod


class BaseNode(ABC):
    def __init__(self,type:NodeType,neighbours: list[Node]):
        self.type = type
        self.neighbours = neighbours
    def getNeighbours(self) -> list[Node]:
            return self.neighbours
    def setNeighbours(self,n) -> None:
        self.neighbours = n

class DepositNode(BaseNode):
    def __init__(self,dex: DEX,neighbours: list[Node],chain: Chain ):
        super.__init__(self,type,NodeType.Deposit,neighbours)
        self.dex = dex
        self.chain = chain



class SwapNode(BaseNode):
    def __init__(self, type, neighbours,stableIn: Stable, stableOut: Stable):
        super().__init__(type, neighbours)
        self.stableIn = stableIn
        self.stableOut = stableOut


class BridgeNode(BaseNode):
    def __init__(self, type, neighbours,chainIn:Chain, chainIn: ch):
        super().__init__(type, neighbours)
        se
    
    
class NodeType(Enum):
    Deposit = auto()
    Swap = auto()
    Bridge = auto()
