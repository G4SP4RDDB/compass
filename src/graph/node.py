from abc import ABC
from enum import Enum, auto

from graph.structures.DEXes import DEX, Chain, Stable


class NodeType(Enum):
    Deposit = auto()
    SourceNode = auto()
    Withdraw = auto()
    Wallet = auto()
    WalletDeficit = auto()


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

    def __init__(self, chain: Chain, stable: Stable, nodeIndex: int, balance: float = 0.0):
        super().__init__(NodeType.Wallet, nodeIndex)
        self.chain = chain
        self.stable = stable
        # Solde RÉEL déjà présent sur l'operating wallet pour ce (chain,
        # stable) au moment du build (voir Graph.__init__ walletBalances,
        # rempli par main.buildAndSolveGraph depuis compass_test.balances /
        # connectors.wallet_sources). Source FONGIBLE et BORNÉE pour le
        # solveur, exactement comme WithdrawNode.balance : au plus ce montant
        # peut sortir du wallet sans y être entré dans le même plan (voir
        # solver._addFlowConservation). 0.0 = pur nœud de transit (l'ancien
        # comportement) : l'argent déjà dans le wallet n'existait pas pour le
        # solveur, qui préférait retirer+bridger depuis un DEX plutôt que de
        # déposer $1 déjà disponible à côté.
        self.balance = balance


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


class WalletDeficitNode(Node):
    """Argent dû à des retraits utilisateur (l'entreprise gère les fonds pour
    compte de tiers, voir main), dans une stable donnée — PARTAGÉ entre
    TOUTES les chains (aucune chain propriétaire, contrairement à WalletNode
    qui reste pinné à une chain) : comblable depuis n'importe quel WalletNode
    de cette stable, sur n'importe quelle chain (voir Graph._linkWalletPayouts) —
    pour l'entreprise, payer sur BSC ou Arbitrum ne fait aucune différence.
    Un seul par stable au niveau du Graph. balance toujours <= 0 (déficit),
    jamais d'arête sortante -> pur puits, comme SourceNode. C'est l'exact
    miroir du surplus (WalletNode.balance, lui jamais partagé entre chains
    sans passer par un bridge et son coût) : déficit partagé, surplus non
    partagé."""

    def __init__(self, stable: Stable, nodeIndex: int, balance: float = 0.0):
        super().__init__(NodeType.WalletDeficit, nodeIndex)
        self.stable = stable
        self.balance = balance


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

