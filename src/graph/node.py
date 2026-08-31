from abc import ABC
from enum import Enum, auto

from graph.structures.DEXes import DEX, Chain, Stable


class NodeType(Enum):
    Deposit = auto()
    Swap = auto()
    Bridge = auto()
    SourceNode = auto()
    Withdraw = auto()


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
    def __init__(self,chain: Chain,nodeIndex:int,dex:DEX,stable:Stable):
        super().__init__(NodeType.Deposit,nodeIndex)
        self.chain = chain
        self.dex = dex
        self.stable = stable


class WithdrawNode(Node):
    def __init__(self, stable: Stable, nodeIndex: int, dex: DEX, balance: float = 0.0):
        super().__init__(NodeType.Withdraw, nodeIndex)
        self.stable = stable
        self.dex = dex
        # Cash à retirer pour CETTE stable précise, toujours >= 0. Fongible
        # entre chains (n'importe laquelle des chains du DEX peut servir de
        # sortie), jamais entre stables (voir DEX.withdrawBalances). Miroir de
        # SourceNode : jamais d'arête entrante, pour la même raison structurelle
        # (empêcher un DEX de servir de pont gratuit entre deux de ses chains).
        self.balance = balance


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

