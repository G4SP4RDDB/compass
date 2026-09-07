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

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.0


# Graine par défaut de _generateMockImbalances : mêmes déséquilibres de
# démo à chaque `python src/main.py`, plutôt qu'un nouveau tirage aléatoire
# à chaque run -- sinon deux runs ne sont jamais comparables après un
# changement de code (coûts/routes différents rien qu'à cause du bruit
# aléatoire, indépendamment du changement testé). `--random` en CLI (ou
# seed=None si appelé depuis du code) revient à un tirage frais.
DEMO_IMBALANCE_SEED = 42


# Repli quand aucun connecteur de solde réel n'est disponible pour un DEX
# donné (voir _fetchRealOrMockBalanceUsd) — plage volontairement petite et
# réaliste (comparable aux soldes réels observés en session sur MEXC/Aster,
# de l'ordre de quelques $ à quelques dizaines de $), plus la même que les
# $500-$3000 précédemment tirés au hasard sans aucun ancrage réel.
_MOCK_BALANCE_RANGE_USD = (5.0, 150.0)


def _fetchRealOrMockBalanceUsd(dex: DEX, rng: random.Random) -> tuple[float, bool]:
    """Retourne (balanceUsd, isReal). Tente une lecture EN DIRECT du solde via
    compass_test/balances.py — un lecteur de solde par DEX, un appel direct à
    l'API de CE DEX précis avec les identifiants de piggybank-arb/.env (voir
    ce module pour le détail : Aster/Aden/Ondo Perps viennent de
    sentinelBackend, les autres de la doc publique de chaque DEX). Aujourd'hui
    les 8 DEX du registre ont un lecteur qui fonctionne. Repli sur
    _MOCK_BALANCE_RANGE_USD si ce DEX n'a pas encore de lecteur, si les
    identifiants sont absents, ou sur toute erreur réseau — ne bloque jamais
    la construction du graphe sur la disponibilité d'une API externe. Import
    local (pas en tête de module) : src/ ne doit pas dépendre de
    compass_test/ au chargement, seulement au moment de cet appel best-
    effort."""
    try:
        from compass_test.balances import get_real_balance_usd

        result = get_real_balance_usd(dex.name)
        if result.balanceUsd is not None and result.balanceUsd > 0:
            return result.balanceUsd, True
    except Exception:
        pass
    return rng.uniform(*_MOCK_BALANCE_RANGE_USD), False


def _generateMockImbalances(dexList: list[DEX], seed: int | None = DEMO_IMBALANCE_SEED) -> None:
    """Assigne des surplus/déficits de démo (le solveur exige une conservation
    de flot exacte : somme des surplus == somme des déficits, sinon le modèle
    CP-SAT est INFEASIBLE) — désormais ANCRÉS sur de vrais soldes DEX quand un
    connecteur est disponible (voir _fetchRealOrMockBalanceUsd), plutôt que
    purement aléatoires : un surplus DEX ne peut plus jamais "offrir" plus que
    ce qu'il détient réellement. Encore un mock au sens où (a) le déficit
    reste sans ancrage réel (pas de vraie donnée de TARGET, voir
    connectors/zfund.py, volontairement pas branché ici) et (b) le montant
    withdrawable retenu n'est qu'une fraction aléatoire du solde, pas le
    solde entier (une vraie desk ne viderait jamais un compte).
    Un DEX sans stable configurée serait exclu (injoignable dans le graphe :
    aucun DepositNode/WithdrawNode créé pour lui, voir dex_registry.py) —
    tous les DEX du registre en ont au moins une aujourd'hui.

    seed=DEMO_IMBALANCE_SEED (défaut) : tirage MOCK reproductible (la partie
    solde réel, elle, varie forcément avec le vrai compte).
    seed=None : tirage mock frais (voir --random dans main())."""
    rng = random.Random(seed)
    eligible = [dex for dex in dexList if dex.stables]
    rng.shuffle(eligible)
    half = len(eligible) // 2
    deficitDexes, surplusDexes = eligible[:half], eligible[half:]

    # Cents entiers plutôt que dollars flottants : garantit une somme exacte
    # (le solveur convertit en int via solver.SCALE, une somme approximative
    # suffit à rendre le modèle infeasible).
    isRealBalance: dict[DEX, bool] = {}
    surplusCents: dict[DEX, int] = {}
    for dex in surplusDexes:
        balanceUsd, isReal = _fetchRealOrMockBalanceUsd(dex, rng)
        isRealBalance[dex] = isReal
        # Retient une fraction aléatoire (30%-90%) du solde, jamais sa
        # totalité : garde une marge de fonctionnement, comme le ferait un
        # vrai rebalancing.
        surplusCents[dex] = round(balanceUsd * rng.uniform(0.3, 0.9) * 100)
    totalCents = sum(surplusCents.values())

    # Déficits : split proportionnel du total des surplus (garantit la
    # conservation EXACTE par construction, plutôt que de tirer des déficits
    # indépendamment et espérer que les surplus suffisent) — toujours mock,
    # faute d'une vraie donnée de TARGET par DEX (voir connectors/zfund.py).
    weights = [rng.random() for _ in deficitDexes]
    weightSum = sum(weights)
    deficitsCents = [round(totalCents * w / weightSum) for w in weights[:-1]]
    deficitsCents.append(totalCents - sum(deficitsCents))  # le dernier absorbe l'arrondi

    for dex, cents in zip(deficitDexes, deficitsCents):
        dex.inbalance = -cents / 100

    for dex, cents in surplusCents.items():
        stable = rng.choice(dex.stables)
        dex.withdrawBalances = {stable: cents / 100}
        # dex.requiresSameChainWithdraw (ex: Aster) : ce surplus n'est
        # évacuable QUE vers la chain où il a été crédité (voir
        # Graph._linkWithdrawalsAndDeposits) — faute de vraie donnée de
        # balance PAR CHAIN (le connecteur ne renvoie qu'un total DEX, pas de
        # répartition par chain), on tire cette chain au hasard parmi celles
        # du DEX, seule partie encore purement mock de ce côté.
        if dex.requiresSameChainWithdraw:
            dex.withdrawChainByStable = {stable: rng.choice(dex.chains)}

    print("Déséquilibres générés (mock, surplus ancré sur le solde réel quand disponible) :")
    for dex in deficitDexes:
        print(f"  - {dex.name}: déficit ${-dex.inbalance:.2f} (mock)")
    for dex in surplusDexes:
        stable, amount = next(iter(dex.withdrawBalances.items()))
        chain = dex.withdrawChainByStable.get(stable)
        chainNote = f", withdrawable only on {chain.name}" if chain is not None else ""
        sourceNote = "real balance" if isRealBalance[dex] else "mock balance"
        print(f"  - {dex.name}: surplus ${amount:.2f} ({stable.name}{chainNote}) [{sourceNote}]")


def buildAndSolveGraph(seed: int | None = DEMO_IMBALANCE_SEED) -> tuple[Graph, dict[str, DEX], TimeWeightParams]:
    """Construit un graphe de démo (déséquilibres mock ancrés sur de vrais
    soldes DEX quand disponibles, voir _generateMockImbalances) et le résout
    une première fois. Factorisé hors de main() pour être réutilisé par
    visualization/server.py, qui garde le Graph résultant en mémoire pour
    re-solver à la volée sur un nouveau k (voir POST /api/solve) sans
    reconstruire tout le graphe (Fee(e)/Time(e) ne dépendent pas de k, voir
    costing.py — seul le plan choisi en dépend).
    seed par défaut = DEMO_IMBALANCE_SEED (partie mock reproductible ; la
    partie solde réel, elle, varie forcément avec le vrai compte) ;
    passer seed=None pour un tirage mock frais."""
    dexRegistry = buildDexRegistry()
    dexList = list(dexRegistry.values())
    _generateMockImbalances(dexList, seed=seed)
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
