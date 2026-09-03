from typing import cast

from connectors import cctp, chain_block_times
from connectors.alchemy import AlchemyConnector
from connectors.gas import GasFeeService, GasOperation
from connectors.stable_tokens import STABLE_DECIMALS, get_stable_token_address
from graph.edge import Edge
from graph.node import NodeType, SourceNode, SwapNode, WithdrawNode
from graph.structures.bridges import BridgeProtocol
from graph.urgency import TimeWeightParams, computeTimeWeight

# Pas de protocole réel modélisé pour GENERIC (voir BridgeProtocol) : ordre de
# grandeur délibérément conservateur (proche de V1) plutôt qu'un 0.0 qui
# biaiserait le solveur en faveur d'un bridge dont on ne connaît pas la vitesse.
GENERIC_BRIDGE_DELAY_SECONDS = 20 * 60


def computeCost(edge: Edge, gasFeeService: GasFeeService) -> float:
    if edge.u.type == NodeType.Withdraw:
        # Retrait CEX du DEX surplus (WithdrawNode -> DepositNode de ce même
        # DEX, voir Graph._addSourceAndDepositNodes) : frais fixe facturé par
        # la plateforme avant que les fonds soient mobilisables ailleurs dans
        # le graphe. WithdrawNode n'a pas de `.chain` (scope dex+stable, pas
        # dex+chain) : sans ce garde-fou en premier, la branche générique
        # v.type in (Deposit,Bridge) plus bas planterait en lisant edge.u.chain.
        return cast(WithdrawNode, edge.u).dex.withdrawFeeUsd

    if edge.v.type == NodeType.SourceNode:
        # Dépôt CEX comblant le déficit du DEX destination (DepositNode ->
        # SourceNode de ce même DEX, idem Graph._addSourceAndDepositNodes).
        return cast(SourceNode, edge.v).dex.depositFeeUsd

    if edge.v.type == NodeType.Swap:
        # frais fixe de la tx de swap ; le coût variable (slippage) est géré
        # séparément, voir computeSwapCostBreakpoints
        return gasFeeService.get_gas_cost_usd(edge.v.chain, GasOperation.SWAP)

    if edge.u.type == NodeType.Swap:
        # sortie du swap (Swap -> Bridge de la stable de sortie) : pure
        # comptabilité, déjà facturé à l'entrée juste au-dessus. Sans ce
        # garde-fou, la branche générique v.type in (Deposit,Bridge) plus bas
        # facturerait un second gas fee (TRANSFER) pour la même opération.
        return 0.0

    if edge.u.type == NodeType.Bridge and edge.v.type == NodeType.Bridge:
        # Bridge->Bridge traverse deux chains différentes : le gas est payé
        # sur la chain de départ pour initier le transfert. Une edge par
        # protocole disponible (voir Graph._linkBridges) : edge.bridgeProtocol
        # dit lequel pricer.
        assert edge.bridgeProtocol is not None
        return gasFeeService.get_bridge_gas_cost_usd(edge.u.chain, edge.v.chain, edge.u.stable, edge.bridgeProtocol)

    if edge.v.type in (NodeType.Deposit, NodeType.Bridge):
        # Deposit<->Deposit et Deposit<->Bridge sont toujours sur la même chain
        # (voir Graph._linkImbalancedDeposits / _linkBridges).
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
    swapNode = edge.v
    if not isinstance(swapNode, SwapNode):
        raise ValueError("computeSwapCostBreakpoints attend une edge dont v est un SwapNode")
    if edge.capacity is None:
        raise ValueError("edge.capacity doit être calculé avant d'estimer le coût du swap")

    connector = alchemyConnector or AlchemyConnector()
    sellTokenAddress = get_stable_token_address(swapNode.chain, swapNode.stableIn)
    buyTokenAddress = get_stable_token_address(swapNode.chain, swapNode.stableOut)
    unitsPerDollar = 10**STABLE_DECIMALS

    breakpoints: list[tuple[float, float]] = [(0.0, 0.0)]
    for step in range(1, NUM_SWAP_COST_SEGMENTS + 1):
        amountInUsd = edge.capacity * step / NUM_SWAP_COST_SEGMENTS
        quote = connector.get_quote(
            swapNode.chain, sellTokenAddress, buyTokenAddress, round(amountInUsd * unitsPerDollar)
        )
        amountOutUsd = quote.buy_amount / unitsPerDollar
        costUsd = max(amountInUsd - amountOutUsd, 0.0)
        breakpoints.append((amountInUsd, costUsd))

    return _lowerConvexHull(breakpoints)


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
        # Délai de traitement du dépôt CEX comblant le déficit du DEX destination.
        return cast(SourceNode, edge.v).dex.depositDelaySeconds

    if edge.u.type == NodeType.Bridge and edge.v.type == NodeType.Bridge:
        # Traversée inter-chain réelle : le délai dépend du protocole (une
        # edge par protocole, voir Graph._linkBridges).
        return computeBridgeDelay(edge)

    # Toute autre edge (Deposit<->Deposit, Deposit<->Bridge, Bridge<->Swap) ne
    # quitte jamais sa chain d'origine (voir Graph._linkImbalancedDeposits /
    # _linkBridges / _linkSwaps) : une tx on-chain doit encore attendre sa
    # confirmation avant que les fonds soient mobilisables ailleurs, jamais
    # 0s. edge.u a toujours .chain ici (Withdraw/SourceNode déjà retournés
    # ci-dessus), et edge.u.chain == edge.v.chain par construction.
    return chain_block_times.get_block_delay_seconds(edge.u.chain)


def computeBridgeDelay(edge: Edge) -> float:
    assert edge.bridgeProtocol is not None
    if edge.bridgeProtocol == BridgeProtocol.CCTP_V1:
        return cctp.CCTP_V1_DELAY_SECONDS_BY_CHAIN[edge.u.chain]
    if edge.bridgeProtocol == BridgeProtocol.CCTP_V2:
        return cctp.CCTP_V2_FAST_TRANSFER_DELAY_SECONDS
    return GENERIC_BRIDGE_DELAY_SECONDS
