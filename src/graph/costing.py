from connectors.alchemy import AlchemyConnector
from connectors.gas import GasFeeService, GasOperation
from connectors.stable_tokens import STABLE_DECIMALS, get_stable_token_address
from graph.edge import Edge
from graph.node import NodeType, SwapNode


def computeCost(edge: Edge, gasFeeService: GasFeeService) -> float:
    if NodeType.SourceNode in (edge.u.type, edge.v.type) or NodeType.Withdraw in (edge.u.type, edge.v.type):
        # edges virtuels (accounting du déficit/surplus), pas de tx réelle :
        # le vrai coût est facturé sur le hop suivant (Deposit->Bridge, etc).
        # WithdrawNode n'a pas de `.chain` (scope dex+stable, pas dex+chain) :
        # sans ce garde-fou, la branche générique v.type in (Deposit,Bridge)
        # plus bas planterait en lisant edge.u.chain.
        return 0.0

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
        # sur la chain de départ pour initier le transfert.
        return gasFeeService.get_gas_cost_usd(edge.u.chain, GasOperation.BRIDGE_SEND)

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


def computeDelay(edge: Edge) -> float:
    if edge.v.type == NodeType.Bridge:
        pass  # TODO: délai du bridge pour la chain
    elif edge.v.type == NodeType.Swap:
        pass  # TODO: délai CowSwap sur la chain
    elif edge.v.type == NodeType.Deposit:
        pass  # TODO: délai de bloc pour la transaction
    return 0.0
