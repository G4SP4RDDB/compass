from enum import Enum
from graph.structures.DEXes import DEX,Chain
from typing import Sequence
from graph.structures.DEXes import Stable
from enum import Enum,auto
from abc import ABC, abstractmethod
from graph.node import Node,SourceNode,NodeType,DepositNode,SwapNode
from graph.edge import Edge


class Graph():

    def __init__(self,dexList: list[DEX],bridgeList:list[Bridge],swapList: list[swapVenues]):
        self.nodeList:list[Node] = []
        self.Edges: list[Edge] = []
        self.Edges = []
        #Ajouter les noeuds => Dex + ses points de deposit
        nodeIndex = 0
        for dex in dexList:
            sourceDexRoot = SourceNode(dex.inbalance,nodeIndex)
            nodeIndex+=1
            
            self.nodeList.append(sourceDexRoot)
            for chain in dex.chains:
                depositNode = DepositNode(chain,nodeIndex)
                self.nodeList.append(depositNode)
                self.Edges.append(Edge(sourceDexRoot,depositNode))
                nodeIndex+=1
        # Link des DEXs ayant des points de deposits similaires
        for i in range(0,len(self.nodeList)):
            for j in range(1+i,len(self.nodeList)):
                u = self.nodeList[i]
                v = self.nodeList[j]
                if (u.type == NodeType.Deposit and v.type == NodeType.Deposit):
                    if ()

        

        
        



        

