from connectors.gas import GasFeeService, GasOperation
from connectors.stable_tokens import get_stable_token_address
from connectors.uniswap import UniswapConnector
from graph.edge import Edge
from graph.node import NodeType, SwapNode


def computeCost(edge: Edge, gasFeeService: GasFeeService) -> float:
    if NodeType.SourceNode in (edge.u.type, edge.v.type):
        return 0.0  # edge virtuel (accounting de l'imbalance), pas de tx réelle

    if edge.v.type == NodeType.Swap:
        # frais fixe de la tx de swap ; le coût variable (slippage) est géré
        # séparément, voir computeSwapCostBreakpoints
        return gasFeeService.get_gas_cost_usd(edge.v.chain, GasOperation.SWAP)

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
UNISWAP_FEE_BPS = 30  # 0.3%, frais standard d'un pool Uniswap V2


def computeSwapCostBreakpoints(
    edge: Edge, uniswapConnector: UniswapConnector | None = None
) -> list[tuple[float, float]]:
    """Points (montant en USD, coût en USD) approximant la courbe de coût d'un
    swap, en NUM_SWAP_COST_SEGMENTS segments linéaires.

    Coût dérivé de la formule constant-product d'Uniswap V2 (x*y=k) à partir
    des réserves réelles du pool : contrairement à des cotes CowSwap
    échantillonnées (bruitées, pas de formule fermée), cette fonction est
    mathématiquement convexe, donc l'approximation par segments est exacte
    sans traitement supplémentaire (l'enveloppe convexe reste un filet de
    sécurité bon marché contre les arrondis flottants).
    """
    swapNode = edge.v
    if not isinstance(swapNode, SwapNode):
        raise ValueError("computeSwapCostBreakpoints attend une edge dont v est un SwapNode")
    if edge.capacity is None:
        raise ValueError("edge.capacity doit être calculé avant d'estimer le coût du swap")

    connector = uniswapConnector or UniswapConnector()
    sellTokenAddress = get_stable_token_address(swapNode.chain, swapNode.stableIn)
    buyTokenAddress = get_stable_token_address(swapNode.chain, swapNode.stableOut)
    reserves = connector.get_reserves(swapNode.chain, sellTokenAddress, buyTokenAddress)

    breakpoints: list[tuple[float, float]] = [(0.0, 0.0)]
    for step in range(1, NUM_SWAP_COST_SEGMENTS + 1):
        amountInUsd = edge.capacity * step / NUM_SWAP_COST_SEGMENTS
        costUsd = _constantProductCost(amountInUsd, reserves.reserve_in, reserves.reserve_out)
        breakpoints.append((amountInUsd, costUsd))

    return _lowerConvexHull(breakpoints)


def _constantProductCost(amountIn: float, reserveIn: float, reserveOut: float) -> float:
    amountInWithFee = amountIn * (10_000 - UNISWAP_FEE_BPS) / 10_000
    amountOut = amountInWithFee * reserveOut / (reserveIn + amountInWithFee)
    return max(amountIn - amountOut, 0.0)


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
