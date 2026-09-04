from abc import ABC
from enum import Enum, auto

from graph.structures.DEXes import DEX, Chain, Stable


class NodeType(Enum):
    Deposit = auto()
    SourceNode = auto()
    Withdraw = auto()
    Wallet = auto()


class Node(ABC):
    def __init__(self, type: NodeType, nodeIndex: int):
        self.type = type
        self.nodeIndex = nodeIndex



class SourceNode (Node):
    def __init__(self,balance: int,nodeIndex,dex:DEX):
        super().__init__(NodeType.SourceNode,nodeIndex)
        self.balance = balance
        self.dex = dex


class WalletNode(Node):
    """Argent en transit sur une chain, dans une stable donnée — PARTAGÉ
    entre tous les DEX (pas de propriétaire), un seul par (chain, stable) au
    niveau du Graph (voir Graph._addWalletNodes). C'est là qu'atterrit un
    retrait tout juste rendu liquide (WithdrawNode -> WalletNode), qu'arrive
    un bridge (WalletNode -> WalletNode, edge.type == EdgeType.Bridge, voir
    Graph._linkBridges), ou que sort un swap (WalletNode -> WalletNode,
    edge.type == EdgeType.Swap, voir Graph._linkSwaps) — et c'est de là que
    part un dépôt, direct (WalletNode -> SourceNode, voir
    DEX.requiresDepositAddress=False, la majorité des DEX) ou vers l'adresse
    de dépôt propre à un DEX CEX-style (WalletNode -> DepositNode). Un
    bridge ou un swap est une opération atomique unique (un seul appel de
    contrat déplace les fonds d'un wallet à l'autre) : jamais de node
    intermédiaire dédié, l'edge à lui seul EST l'opération (voir graph.edge.EdgeType)."""

    def __init__(self, chain: Chain, stable: Stable, nodeIndex: int):
        super().__init__(NodeType.Wallet, nodeIndex)
        self.chain = chain
        self.stable = stable


class DepositNode(Node):
    """Adresse de dépôt propre à un DEX précis, sur une chain précise — n'est
    créée QUE pour les DEX dont DEX.requiresDepositAddress est True : un vrai
    CEX où la plateforme reconnaît/crédite le dépôt séparément du virement,
    avec son propre coût/délai (aucun DEX du registre aujourd'hui, voir
    graph.structures.dex_registry._DEPOSIT_ADDRESS_DEXES, vide -- MEXC crédite
    automatiquement dès réception, donc pas de second hop pour lui). Tous les
    autres DEX déposent directement depuis le WalletNode partagé vers leur
    SourceNode (voir Graph._linkWithdrawalsAndDeposits) — un seul appel de
    contrat fait à la fois le virement et le crédit, pas besoin d'une adresse
    intermédiaire distincte."""

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

