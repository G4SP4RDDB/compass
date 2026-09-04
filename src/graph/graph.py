import itertools
from typing import cast

from connectors.gas import GasFeeService
from graph import costing
from graph.edge import Edge, EdgeType
from graph.node import DepositNode, Node, NodeType, SourceNode, WalletNode, WithdrawNode
from graph.structures.bridges import availableBridgeProtocols
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.swap import SwapVenue

# Toutes les DEX du registre ne vivent plus que sur BSC et/ou Arbitrum (voir
# graph.structures.dex_registry._DEX_SPECS) : la seule route de bridging qui
# reste utile est BSC<->Arbitrum, servie par le bridge interne d'Aden (voir
# graph.structures.bridges.availableBridgeProtocols, qui ne renvoie plus que
# ça). Réactivé maintenant que cette route a un vrai usage ; remettre à False
# si le registre finit par tenir entièrement sur une seule chain.
BRIDGES_ENABLED = True


class Graph:
    def __init__(
        self,
        dexList: list[DEX],
        swapList: list[SwapVenue],
        gasFeeService: GasFeeService | None = None,
    ):
        self.nodeList: list[Node] = []
        self.edgeList: list[Edge] = []
        self.nodeIndex = 0
        self._gasFeeService = gasFeeService or GasFeeService()
        self._addSourceAndWithdrawNodes(dexList)
        self._addWalletNodes()
        self._linkWithdrawalsAndDeposits(dexList)
        self._linkBridges()
        self.computeAllCosts()
        self.computeAllDelays()

    def _addSourceAndWithdrawNodes(self, dexList: list[DEX]) -> None:
        for dex in dexList:
            # SourceNode ne représente que le déficit (<= 0) : comblable en
            # dépôt depuis n'importe quelle chain du DEX (fongible). Jamais
            # d'arête sortante -> pur puits.
            sourceNode = SourceNode(min(dex.inbalance, 0), self.nodeIndex, dex)
            self.nodeList.append(sourceNode)
            self.nodeIndex += 1

            # WithdrawNode : un par stable (pas par chain) ne représente que le
            # surplus (>= 0) de CETTE stable précise, jamais vers une autre
            # stable. Câblé vers le WalletNode partagé de la chain qui va bien
            # dans _linkWithdrawalsAndDeposits (fongible par défaut, restreint
            # à une seule chain si dex.withdrawChainByStable le dit, voir
            # DEX.requiresSameChainWithdraw). Jamais d'arête entrante -> pure
            # source, comme SourceNode est un pur puits (même garantie
            # structurelle : pas de pont gratuit entre deux chains d'un même
            # DEX).
            for stable in dex.stables:
                balance = dex.withdrawBalances.get(stable, 0.0)
                withdrawNode = WithdrawNode(stable, self.nodeIndex, dex, balance=balance)
                self.nodeList.append(withdrawNode)
                self.nodeIndex += 1

    def _addWalletNodes(self) -> None:
        """Un WalletNode par (chain, stable) — PARTAGÉ entre tous les DEX,
        aucun propriétaire (voir node.WalletNode) : un seul point de vérité
        pour "l'argent en transit sur cette chain, dans cette stable", que ce
        soit un retrait tout juste rendu liquide, l'arrivée d'un bridge, ou
        la sortie d'un swap (les deux étant directement des edges
        WalletNode -> WalletNode, voir Graph._linkBridges/_linkSwaps) — plus
        besoin d'une adresse de dépôt par DEX pour simplement faire transiter
        des fonds d'un DEX à un autre sur la même chain (voir
        Graph._linkWithdrawalsAndDeposits)."""
        for chain in Chain:
            for stable in Stable:
                self.nodeList.append(WalletNode(chain, stable, self.nodeIndex))
                self.nodeIndex += 1

    def _linkWithdrawalsAndDeposits(self, dexList: list[DEX]) -> None:
        """Câble WithdrawNode -> WalletNode (retrait) et WalletNode ->
        SourceNode (dépôt), pour chaque DEX et chaque chain qu'il supporte.

        Le dépôt prend deux formes selon dex.requiresDepositAddress :
          - False (tous les DEX du registre aujourd'hui, y compris MEXC qui
            crédite automatiquement dès réception) : WalletNode ->
            SourceNode DIRECTEMENT, un seul hop — un seul appel de contrat
            fait à la fois le virement et le crédit, pas d'adresse de dépôt
            distincte (voir costing.computeCost/computeDelay, qui portent
            alors le gas ET le frais/délai de crédit sur ce même edge).
          - True (un vrai CEX dont le dépôt serait réellement reconnu/crédité
            en deux étapes séparées, voir graph.structures.dex_registry._DEPOSIT_ADDRESS_DEXES) :
            WalletNode -> DepositNode(dex) -> SourceNode, deux hops distincts
            (virement puis reconnaissance/crédit CEX), chacun avec son propre
            coût/délai.
        """
        walletByChainStable: dict[tuple[Chain, Stable], WalletNode] = {
            (cast(WalletNode, n).chain, cast(WalletNode, n).stable): cast(WalletNode, n)
            for n in self.nodeList
            if n.type == NodeType.Wallet
        }
        sourceNodeByDex: dict[DEX, SourceNode] = {
            cast(SourceNode, n).dex: cast(SourceNode, n) for n in self.nodeList if n.type == NodeType.SourceNode
        }
        withdrawNodesByDex: dict[DEX, dict[Stable, WithdrawNode]] = {}
        for n in self.nodeList:
            if n.type == NodeType.Withdraw:
                withdrawNode = cast(WithdrawNode, n)
                withdrawNodesByDex.setdefault(withdrawNode.dex, {})[withdrawNode.stable] = withdrawNode

        for dex in dexList:
            sourceNode = sourceNodeByDex[dex]
            for chain in dex.chains:
                for stable in dex.stables:
                    wallet = walletByChainStable[(chain, stable)]

                    # DEX.withdrawChainByStable restreint l'évacuation du
                    # surplus à UNE SEULE chain (voir DEX.requiresSameChainWithdraw,
                    # ex: Aster) : pas d'edge vers les autres chains dans ce
                    # cas, contrairement au défaut fongible (aucune entrée ->
                    # toutes les chains valent).
                    withdrawNode = withdrawNodesByDex.get(dex, {}).get(stable)
                    if withdrawNode is not None:
                        restrictedChain = dex.withdrawChainByStable.get(stable)
                        if restrictedChain is None or restrictedChain == chain:
                            self.edgeList.append(Edge(withdrawNode, wallet))

                    if dex.requiresDepositAddress:
                        depositNode = DepositNode(chain, self.nodeIndex, dex, stable)
                        self.nodeList.append(depositNode)
                        self.nodeIndex += 1
                        self.edgeList.append(Edge(wallet, depositNode))
                        self.edgeList.append(Edge(depositNode, sourceNode))
                    else:
                        self.edgeList.append(Edge(wallet, sourceNode))

    def _linkBridges(self) -> None:
        walletNodes = [cast(WalletNode, n) for n in self.nodeList if n.type == NodeType.Wallet]

        # Un bridge transporte le même actif d'une chain à l'autre, il ne le
        # convertit jamais : arête directe WalletNode -> WalletNode entre la
        # MÊME stable, sur des chains différentes (graphe complet par stable,
        # une arête dirigée par sens : A->B et B->A sont deux edges
        # distinctes). Une edge PAR PROTOCOLE disponible sur cette route (un
        # seul aujourd'hui : ADEN_INTERNAL, sur BSC<->ARBITRUM uniquement,
        # voir availableBridgeProtocols) : autant d'options parallèles que le
        # solveur peut arbitrer par coût, voir costing.py. Pas de node Bridge
        # intermédiaire : l'opération se fait en un seul appel de contrat, du
        # wallet source au wallet destination — un node entrée/sortie séparé
        # ne représenterait aucune transaction réelle (voir graph.node.WalletNode).
        #
        # Désactivé via BRIDGES_ENABLED (pas supprimé) : toute cette logique
        # reste correcte et prête à être réactivée, voir le commentaire sur
        # BRIDGES_ENABLED en tête de fichier.
        if BRIDGES_ENABLED:
            for walletA, walletB in itertools.permutations(walletNodes, 2):
                if walletA.stable != walletB.stable or walletA.chain == walletB.chain:
                    continue
                for protocol in availableBridgeProtocols(walletA.chain, walletB.chain, walletA.stable):
                    self.edgeList.append(Edge(walletA, walletB, type=EdgeType.Bridge, bridgeProtocol=protocol))

        self._linkSwaps(walletNodes)

    def _linkSwaps(self, walletNodes: list[WalletNode]) -> None:
        # Miroir de _linkBridges : un swap change la stable, jamais la chain
        # (même chain des deux côtés), arête directe WalletNode -> WalletNode
        # (edge.type == EdgeType.Swap la distingue d'un bridge). Comme pour
        # un bridge, un swap est une transaction on-chain atomique unique :
        # pas de node intermédiaire pour ce qui n'est en réalité qu'un seul
        # appel de contrat.
        walletsByChain: dict[Chain, list[WalletNode]] = {}
        for wallet in walletNodes:
            walletsByChain.setdefault(wallet.chain, []).append(wallet)

        for walletsOnChain in walletsByChain.values():
            for walletIn, walletOut in itertools.permutations(walletsOnChain, 2):
                self.edgeList.append(Edge(walletIn, walletOut, type=EdgeType.Swap))

    def computeAllCosts(self) -> None:
        for edge in self.edgeList:
            edge.cost = costing.computeCost(edge, self._gasFeeService)

    def computeAllDelays(self) -> None:
        for edge in self.edgeList:
            edge.time = costing.computeDelay(edge)

    def deficitDexes(self) -> list[DEX]:
        """Une commodité par DEX destination déficitaire (SourceNode.balance < 0),
        dans l'ordre où elles ont été ajoutées au graphe."""
        return [
            cast(SourceNode, node).dex
            for node in self.nodeList
            if node.type == NodeType.SourceNode and cast(SourceNode, node).balance < 0
        ]

    def computeAllCapacities(self) -> None:
        # Borne "infinie" pour les arêtes non contraintes par un excédent/déficit
        # (bridge, swap) : aucun flot ne peut de toute façon dépasser le total
        # des excédents du système, par conservation. Le surplus vit sur les
        # WithdrawNode (SourceNode ne peut plus être que <= 0).
        totalSurplus = sum(
            cast(WithdrawNode, node).balance
            for node in self.nodeList
            if node.type == NodeType.Withdraw
        )
        for edge in self.edgeList:
            edge.capacity = self._edgeCapacity(edge, totalSurplus)

    def _edgeCapacity(self, edge: Edge, unconstrainedCapacity: float) -> float:
        # WithdrawNode n'est jamais que edge.u, SourceNode jamais que edge.v
        # (voir _linkWithdrawalsAndDeposits) : pas de branche symétrique à gérer.
        if edge.u.type == NodeType.Withdraw:
            return cast(WithdrawNode, edge.u).balance

        if edge.v.type == NodeType.SourceNode:
            return abs(cast(SourceNode, edge.v).balance)

        if edge.v.type == NodeType.Deposit:
            # WalletNode -> DepositNode (entrée dans l'adresse de dépôt propre
            # à CE DEX, voir DEX.requiresDepositAddress) : bornée par son
            # propre déficit, comme le hop final Deposit -> SourceNode juste
            # après.
            return abs(cast(DepositNode, edge.v).dex.inbalance)

        return unconstrainedCapacity  # Bridge (WalletNode->WalletNode) et Swap (WalletNode->WalletNode)
