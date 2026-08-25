import numpy as np
from graph.node import Node

from ortools.graph.python import min_cost_flow


def graphSolve(nodeList: list[Node]):

    #Convertir les "Nodes" en Nodes au sens Numpy

    smcf = min_cost_flow.SimpleMinCostFlow()
            
            


