from connectors.config import (
    ALCHEMY_API_KEY,
    TIME_WEIGHT_EPSILON,
    TIME_WEIGHT_K,
    TIME_WEIGHT_LAMBDA_MAX,
    TIME_WEIGHT_LAMBDA_MIN,
)
from connectors.dex_imbalances import (
    DEFAULT_IMBALANCES_PATH,
    InfeasibleImbalancesError,
    apply_dex_imbalances,
    load_dex_imbalances,
    summarize_dex_imbalances,
)
from connectors.dex_operational_params import apply_all_dex_params
from connectors.gas import GasFeeService
from connectors.wallet_deficits import (
    DEFAULT_WALLET_DEFICITS_PATH,
    apply_wallet_deficits,
    load_wallet_deficits,
    total_wallet_deficit_usd,
)
from connectors.wallet_sources import load_wallet_sources, resolve_wallet_balances
from graph.graph import Graph
from graph.solver import RouteMode, graphSolve
from graph.structures.DEXes import DEX, Chain, Stable
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


def _fetchLiveWalletBalances() -> dict[tuple[Chain, Stable], float | None]:
    """Solde on-chain réel de l'operating wallet par (chain, stable), via
    compass_test.balances.list_wallet_balances (import local : src/ ne
    dépend de compass_test qu'au moment de cet appel best-effort, comme
    avant pour les soldes DEX). None pour une paire dont la lecture a
    échoué (RPC/adresse absente) -> traitée comme 0 par
    resolve_wallet_balances sauf override manuel, jamais un montant inventé.
    Une erreur globale (module/config absents) -> dict vide : le graphe se
    construit quand même, wallets en transit pur."""
    try:
        from compass_test.balances import list_wallet_balances

        return {(Chain[r.chain], Stable[r.stable]): r.balanceUsd for r in list_wallet_balances()}
    except Exception:
        return {}


def _checkCombinedFeasibility(
    imbalances: dict,
    walletDeficit: dict,
    walletBalances: dict[tuple[Chain, Stable], float],
) -> None:
    """Étend connectors.dex_imbalances.check_feasibility (DEX seul) au
    déficit wallet EN USD (retraits utilisateur, connectors.wallet_deficits) :
    ce déficit est comblable par n'importe quel surplus, DEX OU wallet, dans
    n'importe quelle stable (voir graph.node.WalletDeficitNode) — l'ignorer
    ferait déclarer infaisable un déficit wallet en réalité couvert par le
    solde déjà présent dans l'operating wallet, le cas le plus courant. Même
    arrondi au centime que graph.solver.SCALE, pour ne pas déclarer
    infaisable un écart de flottant."""
    dexSummary = summarize_dex_imbalances(imbalances)
    totalDeficitUsd = dexSummary.totalDeficitUsd + total_wallet_deficit_usd(walletDeficit)
    totalSurplusUsd = dexSummary.totalSurplusUsd + sum(walletBalances.values())
    if totalDeficitUsd and round(totalDeficitUsd * 100) > round(totalSurplusUsd * 100):
        raise InfeasibleImbalancesError(
            f"total deficit ${totalDeficitUsd:.2f} (DEX imbalances + wallet payouts) exceeds total "
            f"surplus ${totalSurplusUsd:.2f} (DEX imbalances + wallet balances) — raise a surplus or "
            f"lower a deficit by ${totalDeficitUsd - totalSurplusUsd:.2f}"
        )


def buildAndSolveGraph(
    imbalancesPath: str | None = str(DEFAULT_IMBALANCES_PATH),
    walletDeficitsPath: str | None = str(DEFAULT_WALLET_DEFICITS_PATH),
    walletBalances: dict[tuple[Chain, Stable], float] | None = None,
    mode: RouteMode = RouteMode.CHEAPEST,
) -> tuple[Graph, dict[str, DEX], TimeWeightParams]:
    """Construit le graphe à partir des déséquilibres SAISIS À LA MAIN
    (connectors/dex_imbalances.json, édités depuis le panel "Details" du
    frontend — voir connectors.dex_imbalances) et le résout une première
    fois. Plus aucun tirage aléatoire : un DEX sans entrée est équilibré.
    Lève connectors.dex_imbalances.InfeasibleImbalancesError (message
    lisible) si les déficits dépassent les surplus, AVANT de toucher au
    solveur. Factorisé hors de main() pour être réutilisé par
    visualization/server.py, qui garde le Graph résultant en mémoire pour
    re-solver à la volée sur un nouveau k (voir POST /api/solve) sans
    reconstruire tout le graphe (Fee(e)/Time(e) ne dépendent pas de k, voir
    costing.py — seul le plan choisi en dépend), et le reconstruit depuis
    ce fichier sur POST /api/recompute ("Run solver" côté frontend).
    imbalancesPath=None : aucun déséquilibre (tous les DEX équilibrés, plan
    vide) -- utilisé par visualization/server.py pour démarrer quand même
    quand le fichier sur disque est infaisable.
    walletDeficitsPath : idem, mais pour connectors/wallet_deficits.json (le
    déficit dû à des retraits utilisateur, voir connectors.wallet_deficits et
    graph.node.WalletDeficitNode). None -> aucun déficit wallet.
    walletBalances : solde de l'operating wallet par (chain, stable) offert
    au solveur comme source bornée (voir graph.node.WalletNode.balance).
    None (défaut) -> lu en direct on-chain puis filtré par
    connectors/wallet_sources.json (désactivation / override par paire).
    mode : RouteMode.CHEAPEST (défaut) ou RouteMode.FASTEST, voir
    graph.solver.RouteMode -- bascule all-or-nothing choisie côté frontend,
    transmise telle quelle à graphSolve."""
    imbalances = load_dex_imbalances(imbalancesPath) if imbalancesPath is not None else {}
    walletDeficitsRaw = load_wallet_deficits(walletDeficitsPath) if walletDeficitsPath is not None else {}
    dexRegistry = buildDexRegistry()
    dexList = list(dexRegistry.values())
    apply_dex_imbalances(dexList, imbalances)
    apply_all_dex_params(dexList)

    if walletBalances is None:
        walletBalances = resolve_wallet_balances(_fetchLiveWalletBalances(), load_wallet_sources())

    _checkCombinedFeasibility(imbalances, walletDeficitsRaw, walletBalances)

    gasFeeService = GasFeeService() if ALCHEMY_API_KEY else _ZeroGasFeeService()
    if not ALCHEMY_API_KEY:
        print("ALCHEMY_API_KEY absente : coûts de gas mis à 0 (visualisation seulement).")

    timeWeightParams = TimeWeightParams(
        lambda_min=TIME_WEIGHT_LAMBDA_MIN,
        lambda_max=TIME_WEIGHT_LAMBDA_MAX,
        k=TIME_WEIGHT_K,
        epsilon=TIME_WEIGHT_EPSILON,
    )

    graph = Graph(
        dexList,
        swapList=[],
        gasFeeService=gasFeeService,
        walletBalances=walletBalances,
        walletDeficitUsd=apply_wallet_deficits(walletDeficitsRaw),
    )
    graph.computeAllCapacities()
    graphSolve(graph, timeWeightParams, mode)  # peuple edge.flow sur graph.edgeList, lu par renderGraphHtml

    return graph, dexRegistry, timeWeightParams


def main():
    mode = RouteMode.CHEAPEST
    graph, dexRegistry, timeWeightParams = buildAndSolveGraph(mode=mode)

    print(f"Graphe construit : {len(graph.nodeList)} nodes, {len(graph.edgeList)} edges")
    for name in dexRegistry:
        print(f"  - {name}")

    renderGraph(graph, outputPath="graph.png")
    print("Graphe écrit dans graph.png")

    renderGraphHtml(graph, outputPath="graph.html", timeWeightParams=timeWeightParams, mode=mode)
    print("Graphe interactif écrit dans graph.html")

    writeOperationsText(graph, outputPath="operations.txt")
    print("Opérations choisies par le solveur écrites dans operations.txt")


if __name__ == "__main__":
    main()
