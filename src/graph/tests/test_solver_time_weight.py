from typing import cast

import pytest

from connectors.gas import GasFeeService
from graph.graph import Graph
from graph.node import NodeType, SourceNode, WalletNode
from graph.solver import graphSolve, totalCostUsd
from graph.structures.DEXes import DEX, Chain, Stable
from graph.structures.positions import Position
from graph.urgency import TimeWeightParams, computeTimeWeight


class _ZeroGasFeeService(GasFeeService):
    """Stub sans appel réseau : les coûts w(e, d) des tests sont posés à la
    main sur les edges pour isoler le comportement du poids temporel."""

    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.0

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.0


def _depositEdge(graph: Graph, toDex: DEX, chain: Chain) -> object:
    """L'edge de dépôt DIRECT (WalletNode -> SourceNode, voir
    DEX.requiresDepositAddress=False, le défaut) vers `toDex` sur `chain` —
    WalletNode est partagé entre tous les DEX (pas de "fromDex" à filtrer),
    voir Graph._linkWithdrawalsAndDeposits."""
    for edge in graph.edgeList:
        if edge.u.type != NodeType.Wallet or edge.v.type != NodeType.SourceNode:
            continue
        wallet = cast(WalletNode, edge.u)
        if wallet.chain == chain and cast(SourceNode, edge.v).dex is toDex:
            return edge
    raise AssertionError(f"aucune arête wallet->source vers {toDex.name} sur {chain}")


def _zeroOperationalParams(dex: DEX) -> DEX:
    """Neutralise les frais/délais de dépôt/retrait CEX (voir DEX.__init__) :
    ces tests isolent le comportement de λ(σ_d) sur des edges wallet->source
    dont cost/time sont réécrits à la main, pas les défauts
    DEFAULT_WITHDRAW_*/DEFAULT_DEPOSIT_*."""
    dex.withdrawFeeUsd = 0.0
    dex.withdrawDelaySeconds = 0.0
    dex.depositFeeUsd = 0.0
    dex.depositDelaySeconds = 0.0
    return dex


class TestTwoCommoditiesSameLatency:
    """Deux DEX en déficit, alimentés par un même surplus, latence identique
    sur leurs deux arêtes d'entrée mais urgences différentes : le coût total
    doit refléter la commodité la plus urgente payant un poids temporel
    plus élevé sur exactement la même latence."""

    def _build(self, sigmaUrgent: float, sigmaNormal: float):
        dexUrgent = _zeroOperationalParams(DEX([Chain.ETHEREUM], [Stable.USDC], name="urgent"))
        dexUrgent.inbalance = -50.0
        dexUrgent.positions = [Position(sigma=sigmaUrgent)]

        dexNormal = _zeroOperationalParams(DEX([Chain.ETHEREUM], [Stable.USDC], name="normal"))
        dexNormal.inbalance = -50.0
        dexNormal.positions = [Position(sigma=sigmaNormal)]

        dexSurplus = _zeroOperationalParams(DEX([Chain.ETHEREUM], [Stable.USDC], name="surplus"))
        dexSurplus.withdrawBalances = {Stable.USDC: 100.0}

        graph = Graph(
            [dexUrgent, dexNormal, dexSurplus],
            swapList=[],
            gasFeeService=_ZeroGasFeeService(),
        )

        edgeToUrgent = _depositEdge(graph, dexUrgent, Chain.ETHEREUM)
        edgeToNormal = _depositEdge(graph, dexNormal, Chain.ETHEREUM)
        edgeToUrgent.time = 100.0
        edgeToNormal.time = 100.0

        return graph, dexUrgent, dexNormal, dexSurplus

    def test_urgent_destination_pays_more_for_the_same_latency(self):
        params = TimeWeightParams(lambda_min=0.01, lambda_max=5.0, k=2.0, epsilon=0.1)
        graph, dexUrgent, dexNormal, dexSurplus = self._build(sigmaUrgent=0.001, sigmaNormal=1000.0)

        solver = graphSolve(graph, params)

        edgeToUrgent = _depositEdge(graph, dexUrgent, Chain.ETHEREUM)
        # both edges must still carry their full $50 (no cheaper alternative)
        assert edgeToUrgent.flow == pytest.approx(50.0)

        expected = (
            computeTimeWeight(0.001, params) * 100.0 + computeTimeWeight(1000.0, params) * 100.0
        )
        assert totalCostUsd(solver) == pytest.approx(expected, abs=1e-3)


class TestUrgencyFlipsRoutingChoice:
    """Deux routes parallèles vers le même DEX déficitaire : une bon-marché
    mais lente, une chère mais rapide. En régime normal (σ_d grand), la route
    lente et bon marché doit être choisie. En régime d'urgence (σ_d -> 0), le
    solveur doit basculer vers la route rapide malgré son coût plus élevé."""

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

        return graph, dexDeficit, slowCheap, fastExpensive

    def test_normal_regime_prefers_cheap_slow_route(self):
        params = TimeWeightParams(lambda_min=0.01, lambda_max=5.0, k=2.0, epsilon=0.1)
        graph, dexDeficit, slowCheap, fastExpensive = self._build()
        dexDeficit.positions = [Position(sigma=1000.0)]  # loin de toute liquidation

        graphSolve(graph, params)

        assert slowCheap.flow == pytest.approx(50.0)
        assert fastExpensive.flow == pytest.approx(0.0)

    def test_urgent_regime_prefers_fast_expensive_route(self):
        params = TimeWeightParams(lambda_min=0.01, lambda_max=5.0, k=2.0, epsilon=0.1)
        graph, dexDeficit, slowCheap, fastExpensive = self._build()
        dexDeficit.positions = [Position(sigma=0.001)]  # quasi liquidé

        graphSolve(graph, params)

        assert fastExpensive.flow == pytest.approx(50.0)
        assert slowCheap.flow == pytest.approx(0.0)
