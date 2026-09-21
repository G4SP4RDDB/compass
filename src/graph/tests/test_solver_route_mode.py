import pytest

from connectors.gas import GasFeeService
from graph.graph import Graph
from graph.node import NodeType, SourceNode, WalletNode
from graph.solver import RouteMode, graphSolve
from graph.structures.DEXes import DEX, Chain, Stable
from graph.urgency import TimeWeightParams


class _ZeroGasFeeService(GasFeeService):
    """Stub sans appel réseau : les coûts w(e, d) des tests sont posés à la
    main sur les edges pour isoler le comportement du RouteMode."""

    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.0

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.0


def _depositEdge(graph: Graph, toDex: DEX, chain: Chain):
    for edge in graph.edgeList:
        if edge.u.type != NodeType.Wallet or edge.v.type != NodeType.SourceNode:
            continue
        wallet = edge.u
        assert isinstance(wallet, WalletNode)
        source = edge.v
        assert isinstance(source, SourceNode)
        if wallet.chain == chain and source.dex is toDex:
            return edge
    raise AssertionError(f"aucune arête wallet->source vers {toDex.name} sur {chain}")


def _zeroOperationalParams(dex: DEX) -> DEX:
    dex.withdrawFeeUsdByChain = {chain: 0.0 for chain in dex.withdrawFeeUsdByChain}
    dex.withdrawDelaySecondsByChain = {chain: 0.0 for chain in dex.withdrawDelaySecondsByChain}
    dex.depositFeeUsdByChain = {chain: 0.0 for chain in dex.depositFeeUsdByChain}
    dex.depositDelaySecondsByChain = {chain: 0.0 for chain in dex.depositDelaySecondsByChain}
    return dex


class TestCheapestVsFastestRouteMode:
    """Deux routes parallèles vers le même DEX déficitaire : une bon-marché
    mais lente, une chère mais rapide (même montage que
    TestUrgencyFlipsRoutingChoice dans test_solver_time_weight.py). Le
    RouteMode all-or-nothing doit basculer strictement, sans passer par
    λ(σ_d) -- il n'y a ici NI position ouverte NI urgence configurée, donc le
    blend par défaut (mode=None) est indifférent aux deux extrêmes tant que
    λ_min/λ_max restent modestes ; CHEAPEST/FASTEST doivent rester tranchés
    quels que soient lambda_min/lambda_max."""

    def _build(self):
        dexDeficit = _zeroOperationalParams(DEX([Chain.ETHEREUM, Chain.ARBITRUM], [Stable.USDC], name="deficit"))
        dexDeficit.inbalance = -50.0

        dexSurplus = _zeroOperationalParams(DEX([Chain.ETHEREUM, Chain.ARBITRUM], [Stable.USDC], name="surplus"))
        dexSurplus.withdrawBalances = {Stable.USDC: 50.0}

        graph = Graph(
            [dexDeficit, dexSurplus],
            swapList=[],
            gasFeeService=_ZeroGasFeeService(),
        )

        slowCheap = _depositEdge(graph, dexDeficit, Chain.ETHEREUM)
        fastExpensive = _depositEdge(graph, dexDeficit, Chain.ARBITRUM)

        slowCheap.cost, slowCheap.time = 1.0, 100.0
        fastExpensive.cost, fastExpensive.time = 20.0, 1.0

        return graph, slowCheap, fastExpensive

    def _params(self) -> TimeWeightParams:
        # lambda_max délibérément modeste : sans mode=CHEAPEST/FASTEST pour
        # trancher, le blend par défaut resterait cost-dominated ici -- ce
        # test vérifie justement que mode COURT-CIRCUITE ce blend plutôt que
        # de dépendre de la calibration de lambda.
        return TimeWeightParams(lambda_min=0.01, lambda_max=5.0, k=2.0, epsilon=0.1)

    def test_cheapest_mode_ignores_time_entirely(self):
        graph, slowCheap, fastExpensive = self._build()

        graphSolve(graph, self._params(), RouteMode.CHEAPEST)

        assert slowCheap.flow == pytest.approx(50.0)
        assert fastExpensive.flow == pytest.approx(0.0)

    def test_fastest_mode_ignores_fee_entirely(self):
        graph, slowCheap, fastExpensive = self._build()

        graphSolve(graph, self._params(), RouteMode.FASTEST)

        assert fastExpensive.flow == pytest.approx(50.0)
        assert slowCheap.flow == pytest.approx(0.0)

    def test_default_mode_none_is_unchanged_lambda_blend(self):
        """mode=None (aucun code existant ne passe mode) doit continuer à se
        comporter EXACTEMENT comme avant cette fonctionnalité : régime non
        urgent (pas de position ouverte -> sigma = +inf -> lambda(sigma) ->
        lambda_min), le coût domine, donc la route bon marché est choisie --
        même assertion que TestUrgencyFlipsRoutingChoice.test_normal_regime_
        prefers_cheap_slow_route."""
        graph, slowCheap, fastExpensive = self._build()

        graphSolve(graph, self._params())

        assert slowCheap.flow == pytest.approx(50.0)
        assert fastExpensive.flow == pytest.approx(0.0)
