import argparse
import random

from connectors.config import (
    ALCHEMY_API_KEY,
    TIME_WEIGHT_EPSILON,
    TIME_WEIGHT_K,
    TIME_WEIGHT_LAMBDA_MAX,
    TIME_WEIGHT_LAMBDA_MIN,
)
from connectors.dex_operational_params import apply_dex_operational_params, load_dex_operational_params
from connectors.gas import GasFeeService
from graph.graph import Graph
from graph.solver import graphSolve
from graph.structures.DEXes import DEX
from graph.structures.dex_registry import buildDexRegistry
from graph.urgency import TimeWeightParams
from visualization.graph_view import renderGraph
from visualization.web_view import renderGraphHtml, writeOperationsText


class _ZeroGasFeeService(GasFeeService):
    """Utilisé seulement si ALCHEMY_API_KEY est absente : permet de
    construire/visualiser le graphe sans coûts de gas réels, jamais pour
    du routing en conditions réelles."""

    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.0

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, stable, protocol) -> float:
        return 0.0


# Graine par défaut de _generateRandomImbalances : mêmes déséquilibres de
# démo à chaque `python src/main.py`, plutôt qu'un nouveau tirage aléatoire
# à chaque run -- sinon deux runs ne sont jamais comparables après un
# changement de code (coûts/routes différents rien qu'à cause du bruit
# aléatoire, indépendamment du changement testé). `--random` en CLI (ou
# seed=None si appelé depuis du code) revient à un tirage frais.
DEMO_IMBALANCE_SEED = 42


def _generateRandomImbalances(dexList: list[DEX], seed: int | None = DEMO_IMBALANCE_SEED) -> None:
    """Assigne des surplus/déficits aléatoires à des fins de démo (le solveur
    exige une conservation de flot exacte : somme des surplus == somme des
    déficits, sinon le modèle CP-SAT est INFEASIBLE) — à retirer une fois
    connectors/zfund.py branché sur les vraies données de balance/target.
    Un DEX sans stable configurée serait exclu (injoignable dans le graphe :
    aucun DepositNode/WithdrawNode créé pour lui, voir dex_registry.py) —
    tous les DEX du registre en ont au moins une aujourd'hui.

    seed=DEMO_IMBALANCE_SEED (défaut) : déséquilibres reproductibles.
    seed=None : tirage aléatoire frais (voir --random dans main())."""
    rng = random.Random(seed)
    eligible = [dex for dex in dexList if dex.stables]
    rng.shuffle(eligible)
    half = len(eligible) // 2
    deficitDexes, surplusDexes = eligible[:half], eligible[half:]

    # Cents entiers plutôt que dollars flottants : garantit une somme exacte
    # (le solveur convertit en int via solver.SCALE, une somme approximative
    # suffit à rendre le modèle infeasible).
    deficitsCents = [rng.randint(50_000, 300_000) for _ in deficitDexes]  # $500-$3000
    totalCents = sum(deficitsCents)

    weights = [rng.random() for _ in surplusDexes]
    weightSum = sum(weights)
    surplusCents = [round(totalCents * w / weightSum) for w in weights[:-1]]
    surplusCents.append(totalCents - sum(surplusCents))  # le dernier absorbe l'arrondi

    for dex, cents in zip(deficitDexes, deficitsCents):
        dex.inbalance = -cents / 100

    for dex, cents in zip(surplusDexes, surplusCents):
        stable = rng.choice(dex.stables)
        dex.withdrawBalances = {stable: cents / 100}

    print("Déséquilibres aléatoires générés :")
    for dex in deficitDexes:
        print(f"  - {dex.name}: déficit ${-dex.inbalance:.2f}")
    for dex in surplusDexes:
        stable, amount = next(iter(dex.withdrawBalances.items()))
        print(f"  - {dex.name}: surplus ${amount:.2f} ({stable.name})")


def buildAndSolveGraph(seed: int | None = DEMO_IMBALANCE_SEED) -> tuple[Graph, dict[str, DEX], TimeWeightParams]:
    """Construit un graphe de démo (déséquilibres aléatoires, voir
    _generateRandomImbalances) et le résout une première fois. Factorisé hors
    de main() pour être réutilisé par visualization/server.py, qui garde le
    Graph résultant en mémoire pour re-solver à la volée sur un nouveau k
    (voir POST /api/solve) sans reconstruire tout le graphe (Fee(e)/Time(e)
    ne dépendent pas de k, voir costing.py — seul le plan choisi en dépend).
    seed par défaut = DEMO_IMBALANCE_SEED (déséquilibres reproductibles) ;
    passer seed=None pour un tirage aléatoire frais."""
    dexRegistry = buildDexRegistry()
    dexList = list(dexRegistry.values())
    _generateRandomImbalances(dexList, seed=seed)
    apply_dex_operational_params(dexList, load_dex_operational_params())

    gasFeeService = GasFeeService() if ALCHEMY_API_KEY else _ZeroGasFeeService()
    if not ALCHEMY_API_KEY:
        print("ALCHEMY_API_KEY absente : coûts de gas mis à 0 (visualisation seulement).")

    timeWeightParams = TimeWeightParams(
        lambda_min=TIME_WEIGHT_LAMBDA_MIN,
        lambda_max=TIME_WEIGHT_LAMBDA_MAX,
        k=TIME_WEIGHT_K,
        epsilon=TIME_WEIGHT_EPSILON,
    )

    graph = Graph(dexList, swapList=[], gasFeeService=gasFeeService)
    graph.computeAllCapacities()
    graphSolve(graph, timeWeightParams)  # peuple edge.flow sur graph.edgeList, lu par renderGraphHtml

    return graph, dexRegistry, timeWeightParams


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--random",
        action="store_true",
        help="Tirer de nouveaux déséquilibres aléatoires au lieu de la graine fixe par défaut (voir DEMO_IMBALANCE_SEED)",
    )
    args = parser.parse_args()

    graph, dexRegistry, timeWeightParams = buildAndSolveGraph(seed=None if args.random else DEMO_IMBALANCE_SEED)

    print(f"Graphe construit : {len(graph.nodeList)} nodes, {len(graph.edgeList)} edges")
    for name in dexRegistry:
        print(f"  - {name}")

    renderGraph(graph, outputPath="graph.png")
    print("Graphe écrit dans graph.png")

    renderGraphHtml(graph, outputPath="graph.html", timeWeightParams=timeWeightParams)
    print("Graphe interactif écrit dans graph.html")

    writeOperationsText(graph, outputPath="operations.txt")
    print("Opérations choisies par le solveur écrites dans operations.txt")


if __name__ == "__main__":
    main()
