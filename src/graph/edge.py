from enum import Enum
from graph.structures.DEXes import DEX, Chain
from typing import Sequence
from graph.structures.DEXes import Stable
from enum import Enum,auto
from abc import ABC, abstractmethod
from graph.node import Node,NodeType


class Edge:
    def __init__(self,u: Node,v: Node):
        self.u = u
        self.v = v
        self.computeCost()
        self.computeDelay()

    def computeCost(self) -> float:
        if (self.v.type == NodeType.Swap):
            print("FAHH")
            #Problème on sait pas encore combien on envoie sur le edge ici 
            #Appeler la fonction de calcul convexe 
        elif (self.v.type == NodeType.Deposit or self.v.type == NodeType.Bridge):
            print("sdjdkj")
            #gas fees sur la chain en question 
    
    def computeDelay(self) -> float:
        if (self.v.type == NodeType.Bridge):
            print("DJijj")
            #Compute le bridge pour la chain 
        elif (self.v.type == NodeType.Swap):
            print("isjddsj")
            #fonction pour compute le délai cowswap sur la chain 
        elif (self.v.type == NodeType.Deposit):
            #Délai de bloc pour la transaction


    


