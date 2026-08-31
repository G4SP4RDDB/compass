from connectors.config import ALCHEMY_API_KEY
from connectors.gas import GasFeeService
from graph.graph import Graph
from graph.structures.dex_registry import buildDexRegistry
from visualization.graph_view import renderGraph
from visualization.web_view import renderGraphHtml


class _ZeroGasFeeService(GasFeeService):
    """Utilisé seulement si ALCHEMY_API_KEY est absente : permet de
    construire/visualiser le graphe sans coûts de gas réels, jamais pour
    du routing en conditions réelles."""

    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.0


def main():
    dexRegistry = buildDexRegistry()
    dexList = list(dexRegistry.values())

    gasFeeService = GasFeeService() if ALCHEMY_API_KEY else _ZeroGasFeeService()
    if not ALCHEMY_API_KEY:
        print("ALCHEMY_API_KEY absente : coûts de gas mis à 0 (visualisation seulement).")

    graph = Graph(dexList, swapList=[], gasFeeService=gasFeeService)
    graph.computeAllCapacities()

    print(f"Graphe construit : {len(graph.nodeList)} nodes, {len(graph.edgeList)} edges")
    for name in dexRegistry:
        print(f"  - {name}")

    renderGraph(graph, outputPath="graph.png")
    print("Graphe écrit dans graph.png")

    renderGraphHtml(graph, outputPath="graph.html")
    print("Graphe interactif écrit dans graph.html")


if __name__ == "__main__":
    main()
