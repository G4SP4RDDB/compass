import itertools
from typing import cast

from connectors.gas import GasFeeService
from graph import costing
from graph.edge import Edge
from graph.node import BridgeNode, DepositNode, Node, NodeType, SourceNode, SwapNode, WithdrawNode
from graph.structures.bridges import Bridge, buildBridgeList
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.swap import SwapVenue


class Graph:
    def __init__(
        self,
        dexList: list[DEX],
        swapList: list[SwapVenue],
        gasFeeService: GasFeeService | None = None,
        bridgeList: list[Bridge] | None = None,
    ):
        self.nodeList: list[Node] = []
        self.edgeList: list[Edge] = []
        self.nodeIndex = 0
        self._gasFeeService = gasFeeService or GasFeeService()
        self._bridgeList = bridgeList if bridgeList is not None else buildBridgeList()
        self._addSourceAndDepositNodes(dexList)
        self._linkImbalancedDeposits()
        self._addSourcesBridges()
        self._linkBridges()
        self.computeAllCosts()

    def _addSourceAndDepositNodes(self, dexList: list[DEX]) -> None:
        for dex in dexList:
            # SourceNode ne représente que le déficit (<= 0) : comblable en
            # dépôt depuis n'importe quelle chain du DEX (fongible). Jamais
            # d'arête sortante -> pur puits.
            sourceNode = SourceNode(min(dex.inbalance, 0), self.nodeIndex, dex)
            self.nodeList.append(sourceNode)
            self.nodeIndex += 1

            # WithdrawNode : un par stable (pas par chain) ne représente que le
            # surplus (>= 0) de CETTE stable précise, évacuable vers n'importe
            # quelle chain du DEX qui la supporte (fongible), jamais vers une
            # autre stable. Jamais d'arête entrante -> pure source, comme
            # SourceNode est un pur puits (même garantie structurelle : pas de
            # pont gratuit entre deux chains d'un même DEX).
            withdrawNodeByStable: dict[Stable, WithdrawNode] = {}
            for stable in dex.stables:
                balance = dex.withdrawBalances.get(stable, 0.0)
                withdrawNode = WithdrawNode(stable, self.nodeIndex, dex, balance=balance)
                self.nodeList.append(withdrawNode)
                withdrawNodeByStable[stable] = withdrawNode
                self.nodeIndex += 1

            for chain in dex.chains:
                for stable in dex.stables:
                    depositNode = DepositNode(chain, self.nodeIndex, dex, stable)
                    self.nodeList.append(depositNode)
                    self.edgeList.append(Edge(depositNode, sourceNode))
                    self.edgeList.append(Edge(withdrawNodeByStable[stable], depositNode))
                    self.nodeIndex += 1

    def _linkImbalancedDeposits(self) -> None:
        #Créer une arête entre deux nodes de deposits
        depositNodes = [cast(DepositNode, node) for node in self.nodeList if node.type == NodeType.Deposit]
        # Surplus : le DEX a du cash à évacuer pour cette stable (n'importe
        # laquelle de ses chains convient, voir DEX.withdrawBalances).
        # Déficit : comblable depuis n'importe quelle chain du DEX (fongible,
        # basé sur le déficit du DEX, pas sur ce node en particulier) — mais
        # jamais depuis une AUTRE stable : ça, c'est le rôle du SwapNode.
        surplusNodes = [n for n in depositNodes if n.dex.withdrawBalances.get(n.stable, 0.0) > 0]
        deficitNodes = [n for n in depositNodes if n.dex.inbalance < 0]

        for surplusNode in surplusNodes:
            for deficitNode in deficitNodes:
                if surplusNode.dex is deficitNode.dex:
                    continue
                if surplusNode.chain != deficitNode.chain or surplusNode.stable != deficitNode.stable:
                    continue
                self.edgeList.append(Edge(surplusNode, deficitNode))

    def _addSourcesBridges(self) -> None:
        for bridge in self._bridgeList:
            self.nodeList.append(BridgeNode(bridge.chain, bridge.stable, self.nodeIndex))
            self.nodeIndex += 1

    def _linkBridges(self) -> None:
        bridgeNodes = [cast(BridgeNode, n) for n in self.nodeList if n.type == NodeType.Bridge]
        depositNodes = [cast(DepositNode, n) for n in self.nodeList if n.type == NodeType.Deposit]

        # Un bridge transporte le même actif d'une chain à l'autre, il ne le
        # convertit jamais : arête seulement entre deux bridges de la MÊME
        # stable, sur des chains différentes (graphe complet par stable, une
        # arête dirigée par sens : A->B et B->A sont deux edges distincts).
        for bridgeA, bridgeB in itertools.permutations(bridgeNodes, 2):
            if bridgeA.stable != bridgeB.stable:
                continue
            self.edgeList.append(Edge(bridgeA, bridgeB))

        # Un deposit peut envoyer vers le bridge de sa chain/stable si son DEX a
        # du surplus à évacuer pour cette stable ; il peut recevoir depuis le
        # bridge si son DEX est en déficit (fongible entre chains). La stable
        # du bridge doit toujours matcher celle du deposit — la conversion
        # passe par le SwapNode (_linkSwaps), jamais ici.
        for bridge in bridgeNodes:
            for deposit in depositNodes:
                if deposit.chain != bridge.chain or deposit.stable != bridge.stable:
                    continue
                if deposit.dex.withdrawBalances.get(deposit.stable, 0.0) > 0:
                    self.edgeList.append(Edge(deposit, bridge))
                if deposit.dex.inbalance < 0:
                    self.edgeList.append(Edge(bridge, deposit))

        self._linkSwaps(bridgeNodes)

    def _linkSwaps(self, bridgeNodes: list[BridgeNode]) -> None:
        # Miroir de _linkBridges : un swap change la stable, jamais la chain
        # (même chain des deux côtés). Les deux réseaux (bridge = même stable
        # entre chains, swap = même chain entre stables) ne se croisent qu'ici.
        bridgesByChain: dict[Chain, list[BridgeNode]] = {}
        for bridge in bridgeNodes:
            bridgesByChain.setdefault(bridge.chain, []).append(bridge)

        for chain, bridgesOnChain in bridgesByChain.items():
            for bridgeIn, bridgeOut in itertools.permutations(bridgesOnChain, 2):
                swapNode = SwapNode(chain, bridgeIn.stable, bridgeOut.stable, self.nodeIndex)
                self.nodeList.append(swapNode)
                self.nodeIndex += 1
                self.edgeList.append(Edge(bridgeIn, swapNode))
                self.edgeList.append(Edge(swapNode, bridgeOut))

    def computeAllCosts(self) -> None:
        for edge in self.edgeList:
            edge.cost = costing.computeCost(edge, self._gasFeeService)

    def computeAllCapacities(self) -> None:
        # Borne "infinie" pour les arêtes non contraintes par un excédent/déficit
        # (bridge<->bridge, swap) : aucun flot ne peut de toute façon dépasser
        # le total des excédents du système, par conservation. Le surplus vit
        # sur les WithdrawNode (SourceNode ne peut plus être que <= 0).
        totalSurplus = sum(
            cast(WithdrawNode, node).balance
            for node in self.nodeList
            if node.type == NodeType.Withdraw
        )
        for edge in self.edgeList:
            edge.capacity = self._edgeCapacity(edge, totalSurplus)

    def _edgeCapacity(self, edge: Edge, unconstrainedCapacity: float) -> float:
        # WithdrawNode n'est jamais que edge.u, SourceNode jamais que edge.v
        # (voir _addSourceAndDepositNodes) : pas de branche symétrique à gérer.
        if edge.u.type == NodeType.Withdraw:
            return cast(WithdrawNode, edge.u).balance

        if edge.v.type == NodeType.SourceNode:
            return abs(cast(SourceNode, edge.v).balance)

        if edge.u.type == NodeType.Deposit and edge.v.type == NodeType.Deposit:
            surplusDeposit = cast(DepositNode, edge.u)
            deficitDeposit = cast(DepositNode, edge.v)
            surplusAvailable = surplusDeposit.dex.withdrawBalances.get(surplusDeposit.stable, 0.0)
            return min(surplusAvailable, abs(deficitDeposit.dex.inbalance))

        if edge.u.type == NodeType.Deposit and edge.v.type == NodeType.Bridge:
            depositU = cast(DepositNode, edge.u)
            return depositU.dex.withdrawBalances.get(depositU.stable, 0.0)

        if edge.u.type == NodeType.Bridge and edge.v.type == NodeType.Deposit:
            return abs(cast(DepositNode, edge.v).dex.inbalance)

        return unconstrainedCapacity  # Bridge <-> Bridge, Bridge <-> Swap

