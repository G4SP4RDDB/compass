from typing import cast

from connectors import chain_block_times
from connectors.alchemy import AlchemyConnector
from connectors.gas import GasFeeService, GasOperation
from connectors.stable_tokens import STABLE_DECIMALS, get_stable_token_address
from graph.edge import Edge, EdgeType
from graph.node import NodeType, SourceNode, WalletNode, WithdrawNode
from graph.structures.bridges import adenBridgeFeeUsd
from graph.urgency import TimeWeightParams, computeTimeWeight

# Pas de simulation réelle du bridge interne d'Aden (voir BridgeProtocol,
# GasFeeService.get_bridge_gas_cost_usd) : ordre de grandeur délibérément
# conservateur plutôt qu'un 0.0 qui biaiserait le solveur en sa faveur alors
# qu'on ne connaît pas sa vitesse réelle -- à affiner avec de vraies données.
ADEN_INTERNAL_BRIDGE_DELAY_SECONDS = 20 * 60


def computeCost(edge: Edge, gasFeeService: GasFeeService) -> float:
    if edge.u.type == NodeType.Withdraw:
        # Retrait CEX du DEX surplus (WithdrawNode -> WalletNode, voir
        # Graph._linkWithdrawalsAndDeposits) : frais fixe facturé par la
        # plateforme avant que les fonds soient mobilisables ailleurs dans
        # le graphe. WithdrawNode n'a pas de `.chain` (scope dex+stable, pas
        # dex+chain) : sans ce garde-fou en premier, une branche plus bas
        # lisant edge.u.chain (Swap, Bridge, Deposit) planterait.
        return cast(WithdrawNode, edge.u).dex.withdrawFeeUsd

    if edge.v.type == NodeType.SourceNode:
        # Dépôt comblant le déficit du DEX destination (voir
        # Graph._linkWithdrawalsAndDeposits) — deux formes selon
        # DEX.requiresDepositAddress :
        dex = cast(SourceNode, edge.v).dex
        if edge.u.type == NodeType.Wallet:
            # Dépôt DIRECT (WalletNode -> SourceNode, tous les DEX du
            # registre aujourd'hui) : un seul appel de contrat fait à la fois
            # le virement ET le crédit -- ce seul edge porte donc le gas du
            # virement EN PLUS du frais de dépôt éventuel du DEX, pas de hop
            # séparé pour ça.
            return gasFeeService.get_gas_cost_usd(cast(WalletNode, edge.u).chain, GasOperation.TRANSFER) + dex.depositFeeUsd
        # DepositNode -> SourceNode (DEX.requiresDepositAddress=True) : le
        # gas du virement est déjà facturé sur le hop précédent (Wallet ->
        # DepositNode, branche v.type==Deposit plus bas), ici seulement le
        # frais de crédit CEX.
        return dex.depositFeeUsd

    if edge.type == EdgeType.Swap:
        # Une seule transaction on-chain, atomique, du wallet source au
        # wallet destination (voir graph.node.WalletNode) : le frais fixe de
        # la tx couvre toute l'opération. Le coût variable (slippage) est géré
        # séparément, voir computeSwapCostBreakpoints.
        return gasFeeService.get_gas_cost_usd(edge.u.chain, GasOperation.SWAP)

    if edge.type == EdgeType.Bridge:
        # Bridge traverse deux chains différentes en un seul appel de contrat :
        # le gas est payé une seule fois, sur la chain de départ, pour toute
        # l'opération. Seule route bridgée aujourd'hui : le bridge interne
        # d'Aden entre BSC et Arbitrum (voir graph.structures.bridges), qui
        # facture EN PLUS son propre frais fixe, PAR SENS (pas symétrique) --
        # même logique que gas + dex.depositFeeUsd juste au-dessus, mais côté
        # bridge plutôt que côté dépôt.
        assert edge.bridgeProtocol is not None
        gasCost = gasFeeService.get_bridge_gas_cost_usd(edge.u.chain, edge.v.chain, edge.bridgeProtocol)
        return gasCost + adenBridgeFeeUsd(edge.u.chain, edge.v.chain)

    if edge.v.type == NodeType.Deposit:
        # Wallet->DepositNode (entrée CEX-style, voir DEX.requiresDepositAddress) :
        # toujours sur la même chain des deux côtés.
        return gasFeeService.get_gas_cost_usd(edge.u.chain, GasOperation.TRANSFER)

    return 0.0


NUM_SWAP_COST_SEGMENTS = 4


def computeSwapCostBreakpoints(
    edge: Edge, alchemyConnector: AlchemyConnector | None = None
) -> list[tuple[float, float]]:
    """Points (montant en USD, coût en USD) approximant la courbe de coût d'un
    swap, en NUM_SWAP_COST_SEGMENTS segments linéaires.

    Coût échantillonné directement via QuoterV2 (Uniswap V3, eth_call en
    lecture seule) : contrairement à notre ancienne approximation
    constant-product (V2), ceci lit la vraie liquidité concentrée du pool,
    donc le montant de sortie réel. L'enveloppe convexe reste un filet de
    sécurité bon marché si les ticks traversés cassent la convexité stricte.
    """
    if edge.type != EdgeType.Swap:
        raise ValueError("computeSwapCostBreakpoints attend une edge de type EdgeType.Swap")
    if edge.capacity is None:
        raise ValueError("edge.capacity doit être calculé avant d'estimer le coût du swap")

    walletIn, walletOut = cast(WalletNode, edge.u), cast(WalletNode, edge.v)
    connector = alchemyConnector or AlchemyConnector()
    sellTokenAddress = get_stable_token_address(walletIn.chain, walletIn.stable)
    buyTokenAddress = get_stable_token_address(walletIn.chain, walletOut.stable)
    unitsPerDollar = 10**STABLE_DECIMALS

    breakpoints: list[tuple[float, float]] = [(0.0, 0.0)]
    for step in range(1, NUM_SWAP_COST_SEGMENTS + 1):
        amountInUsd = edge.capacity * step / NUM_SWAP_COST_SEGMENTS
        quote = connector.get_quote(
            walletIn.chain, sellTokenAddress, buyTokenAddress, round(amountInUsd * unitsPerDollar)
        )
        amountOutUsd = quote.buy_amount / unitsPerDollar
        costUsd = max(amountInUsd - amountOutUsd, 0.0)
        breakpoints.append((amountInUsd, costUsd))

    return _lowerConvexHull(breakpoints)


def computeRealizedSwapSlippageUsd(edge: Edge, alchemyConnector: AlchemyConnector | None = None) -> float:
    """Slippage RÉEL d'un swap au montant EXACT retenu par le solveur
    (edge.flow) — appelée une fois APRÈS résolution (voir
    graph.solver.graphSolve), contrairement à computeSwapCostBreakpoints qui
    échantillonne NUM_SWAP_COST_SEGMENTS points jusqu'à edge.capacity pour
    construire l'approximation utilisée PENDANT l'optimisation (le montant
    réel n'est pas encore connu à ce stade). Une seule cotation QuoterV2
    suffit ici : le montant exact est déjà connu, pas besoin d'interpoler
    une courbe. edge.cost (voir computeCost) ne porte jamais que le gas fixe
    de la tx de swap — ce slippage est délibérément gardé séparé (voir
    Edge.realizedSlippageUsd) pour ne jamais être accumulé d'un re-solve à
    l'autre du même Graph."""
    if edge.type != EdgeType.Swap:
        raise ValueError("computeRealizedSwapSlippageUsd attend une edge de type EdgeType.Swap")
    if not edge.flow:
        raise ValueError("edge.flow doit être connu (post-résolution) pour calculer le slippage réalisé")

    walletIn, walletOut = cast(WalletNode, edge.u), cast(WalletNode, edge.v)
    connector = alchemyConnector or AlchemyConnector()
    sellTokenAddress = get_stable_token_address(walletIn.chain, walletIn.stable)
    buyTokenAddress = get_stable_token_address(walletIn.chain, walletOut.stable)
    unitsPerDollar = 10**STABLE_DECIMALS

    quote = connector.get_quote(walletIn.chain, sellTokenAddress, buyTokenAddress, round(edge.flow * unitsPerDollar))
    amountOutUsd = quote.buy_amount / unitsPerDollar
    return max(edge.flow - amountOutUsd, 0.0)


def _lowerConvexHull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    hull: list[tuple[float, float]] = []
    for point in points:
        while len(hull) >= 2 and _cross(hull[-2], hull[-1], point) <= 0:
            hull.pop()
        hull.append(point)
    return hull


def _cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def computeTimeWeightedCost(fee: float, edgeTime: float, sigmaD: float, params: TimeWeightParams) -> float:
    """w(e, d) = Fee(e) + λ(σ_d) · Time(e), pour la commodité destinée au DEX d
    dont l'urgence de liquidation est σ_d."""
    return fee + computeTimeWeight(sigmaD, params) * edgeTime


def computeDelay(edge: Edge) -> float:
    if edge.u.type == NodeType.Withdraw:
        # Délai de traitement du retrait CEX avant que les fonds soient
        # mobilisables ailleurs (même edge que dans computeCost ci-dessus).
        return cast(WithdrawNode, edge.u).dex.withdrawDelaySeconds

    if edge.v.type == NodeType.SourceNode:
        dex = cast(SourceNode, edge.v).dex
        if edge.u.type == NodeType.Wallet:
            # Dépôt DIRECT (voir computeCost) : ce même edge porte aussi la
            # confirmation on-chain du virement, pas de hop séparé pour ça.
            return chain_block_times.get_block_delay_seconds(cast(WalletNode, edge.u).chain) + dex.depositDelaySeconds
        # DepositNode -> SourceNode (DEX.requiresDepositAddress=True) : la
        # confirmation on-chain est déjà comptée sur le hop précédent
        # (Wallet -> DepositNode), ici seulement le délai de traitement CEX.
        return dex.depositDelaySeconds

    if edge.type == EdgeType.Bridge:
        # Traversée inter-chain réelle, en une seule tx atomique (voir
        # computeCost) : le délai dépend du protocole (une edge par
        # protocole, voir Graph._linkBridges), pas de la chain de départ.
        return computeBridgeDelay(edge)

    # Toute autre edge (Wallet->DepositNode, Wallet->Wallet Swap) ne quitte
    # jamais sa chain d'origine (voir Graph._linkWithdrawalsAndDeposits /
    # _linkSwaps) : une tx on-chain doit encore attendre sa confirmation
    # avant que les fonds soient mobilisables ailleurs, jamais 0s — UNE SEULE
    # fois, cette edge étant la tx entière (voir computeCost pour Swap).
    # edge.u a toujours .chain ici (Withdraw/SourceNode déjà retournés
    # ci-dessus), et edge.u.chain == edge.v.chain par construction.
    return chain_block_times.get_block_delay_seconds(edge.u.chain)


def computeBridgeDelay(edge: Edge) -> float:
    # Un seul protocole existe aujourd'hui (voir graph.structures.bridges.
    # BridgeProtocol) : pas de branchement à faire, juste le placeholder
    # conservateur ci-dessus tant qu'on n'a pas de vraie donnée sur le bridge
    # interne d'Aden.
    assert edge.bridgeProtocol is not None
    return ADEN_INTERNAL_BRIDGE_DELAY_SECONDS
