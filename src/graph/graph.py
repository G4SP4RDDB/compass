import itertools
from typing import cast

from connectors.gas import GasFeeService
from graph import costing
from graph.edge import Edge
from graph.node import BridgeNode, DepositNode, Node, NodeType, SourceNode
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.swap import SwapVenue


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
        self._addSourceAndDepositNodes(dexList)
        self._linkImbalancedDeposits()
        self._addSourcesBridges()
        self._linkBridges()
        self.computeAllCosts()

    def _addSourceAndDepositNodes(self, dexList: list[DEX]) -> None:
        for dex in dexList:
            sourceNode = SourceNode(dex.inbalance, self.nodeIndex, dex)
            self.nodeList.append(sourceNode)
            self.nodeIndex += 1

            for chain in dex.chains:
                depositNode = DepositNode(chain, self.nodeIndex, dex)
                self.nodeList.append(depositNode)
                # Le flot doit pouvoir sortir d'un SourceNode en surplus (supply
                # positif) et entrer dans un SourceNode en déficit (supply
                # négatif) : l'arête est orientée selon le signe de l'imbalance.
                if dex.inbalance >= 0:
                    self.edgeList.append(Edge(sourceNode, depositNode))
                else:
                    self.edgeList.append(Edge(depositNode, sourceNode))
                self.nodeIndex += 1

    def _linkImbalancedDeposits(self) -> None:
        #Créer une arête entre deux nodes de deposits
        depositNodes = [
            cast(DepositNode, node)
            for node in self.nodeList
            if node.type == NodeType.Deposit and node.dex.inbalance != 0
        ]
        surplusNodes = [n for n in depositNodes if n.dex.inbalance > 0]
        deficitNodes = [n for n in depositNodes if n.dex.inbalance < 0]

        for surplusNode in surplusNodes:
            for deficitNode in deficitNodes:
                if surplusNode.dex is deficitNode.dex:
                    continue
                if surplusNode.chain != deficitNode.chain:
                    continue
                self.edgeList.append(Edge(surplusNode, deficitNode))
    def _addSourcesBridges(self) -> None:
        for chain in Chain:
            self.nodeList.append(BridgeNode(chain, Stable.USDC, self.nodeIndex))
            self.nodeIndex += 1

    def _linkBridges(self) -> None:
        bridgeNodes = [cast(BridgeNode, n) for n in self.nodeList if n.type == NodeType.Bridge]
        depositNodes = [cast(DepositNode, n) for n in self.nodeList if n.type == NodeType.Deposit]

        # Chaque bridge est relié à tous les autres bridges (graphe complet,
        # une arête dirigée par sens : A->B et B->A sont deux edges distincts).
        for bridgeA, bridgeB in itertools.permutations(bridgeNodes, 2):
            self.edgeList.append(Edge(bridgeA, bridgeB))

        # Un deposit en surplus peut envoyer vers le bridge de sa chain ; un
        # deposit en déficit peut recevoir depuis le bridge de sa chain. Jamais
        # l'inverse (pas de sortie depuis un déficit, pas d'entrée vers un
        # surplus), et un deposit équilibré n'a besoin ni de l'un ni de l'autre.
        for bridge in bridgeNodes:
            for deposit in depositNodes:
                if deposit.chain != bridge.chain:
                    continue
                if deposit.dex.inbalance > 0:
                    self.edgeList.append(Edge(deposit, bridge))
                elif deposit.dex.inbalance < 0:
                    self.edgeList.append(Edge(bridge, deposit))

    def computeAllCosts(self) -> None:
        for edge in self.edgeList:
            edge.cost = costing.computeCost(edge, self._gasFeeService)

    def computeAllCapacities(self) -> None:
        # Borne "infinie" pour les arêtes non contraintes par un excédent/déficit
        # (bridge<->bridge) : aucun flot ne peut de toute façon dépasser le total
        # des excédents du système, par conservation.
        totalSurplus = sum(
            cast(SourceNode, node).balance
            for node in self.nodeList
            if node.type == NodeType.SourceNode and node.balance > 0
        )
        for edge in self.edgeList:
            edge.capacity = self._edgeCapacity(edge, totalSurplus)

    def _edgeCapacity(self, edge: Edge, unconstrainedCapacity: float) -> float:
        if edge.u.type == NodeType.SourceNode:
            return abs(cast(SourceNode, edge.u).balance)

        if edge.v.type == NodeType.SourceNode:
            return abs(cast(SourceNode, edge.v).balance)

        if edge.u.type == NodeType.Deposit and edge.v.type == NodeType.Deposit:
            surplusDeposit = cast(DepositNode, edge.u)
            deficitDeposit = cast(DepositNode, edge.v)
            return min(surplusDeposit.dex.inbalance, abs(deficitDeposit.dex.inbalance))

        if edge.u.type == NodeType.Deposit and edge.v.type == NodeType.Bridge:
            return cast(DepositNode, edge.u).dex.inbalance

        if edge.u.type == NodeType.Bridge and edge.v.type == NodeType.Deposit:
            return abs(cast(DepositNode, edge.v).dex.inbalance)

        return unconstrainedCapacity  # Bridge <-> Bridge

