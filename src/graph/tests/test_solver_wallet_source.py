from typing import cast

import pytest

from connectors.gas import GasFeeService
from graph.edge import EdgeType
from graph.graph import Graph
from graph.node import NodeType, SourceNode, WalletNode, WithdrawNode
from graph.solver import graphSolve
from graph.structures.DEXes import DEX, Chain, Stable
from graph.urgency import TimeWeightParams
from visualization.journeys import decomposeJourneys


class _FlatGas(GasFeeService):
    def __init__(self) -> None:
        pass

    def get_gas_cost_usd(self, chain, operation) -> float:
        return 0.01

    def get_bridge_gas_cost_usd(self, source_chain, destination_chain, protocol) -> float:
        return 0.5


@pytest.fixture(autouse=True)
def _noNetworkSwapQuotes(monkeypatch):
    """Swap edges (WalletNode -> WalletNode, other stable) get priced through
    a live Uniswap QuoterV2 eth_call in solver._addSwapCost — slow and
    network-dependent. Every scenario here is same-stable, so swaps are
    never needed: make the quote raise ConnectorError, which _addSwapCost
    already handles by disabling the edge."""
    from connectors.exceptions import ConnectorError
    from graph import costing

    def _unavailable(edge, alchemyConnector=None):
        raise ConnectorError("swap quotes disabled in tests")

    monkeypatch.setattr(costing, "computeSwapCostBreakpoints", _unavailable)


def _params() -> TimeWeightParams:
    return TimeWeightParams(lambda_min=0.0, lambda_max=0.0, k=1.0)


def _dex(name: str, chains: list[Chain], stable: Stable) -> DEX:
    dex = DEX(chains, [stable], name=name)
    for chain in chains:
        dex.withdrawFeeUsdByChain[chain] = 0.2
        dex.withdrawDelaySecondsByChain[chain] = 0.0
        dex.depositDelaySecondsByChain[chain] = 0.0
    return dex


def _withdrawn(graph: Graph, dex: DEX) -> float:
    return sum(e.flow or 0.0 for e in graph.edgeList if e.u.type == NodeType.Withdraw and cast(WithdrawNode, e.u).dex is dex)


def _walletDeposits(graph: Graph, chain: Chain, toDex: DEX) -> float:
    return sum(
        e.flow or 0.0
        for e in graph.edgeList
        if e.u.type == NodeType.Wallet
        and cast(WalletNode, e.u).chain == chain
        and e.v.type == NodeType.SourceNode
        and cast(SourceNode, e.v).dex is toDex
    )


def _bridged(graph: Graph) -> float:
    return sum(e.flow or 0.0 for e in graph.edgeList if e.type == EdgeType.Bridge)


class TestWalletAsSource:
    def test_wallet_cash_beats_withdraw_plus_bridge(self):
        """The user's case: +1000 on Aden (BSC), $3 in the Arbitrum wallet,
        $1 deficit on Lighter (Arbitrum). Before: Aden withdraw -> bridge ->
        deposit. Now: a single $1 deposit from the wallet, Aden untouched."""
        aden = _dex("Aden", [Chain.BSC], Stable.USDT)
        aden.withdrawBalances = {Stable.USDT: 1000.0}
        lighter = _dex("Lighter", [Chain.ARBITRUM], Stable.USDT)
        lighter.inbalance = -1.0

        graph = Graph([aden, lighter], swapList=[], gasFeeService=_FlatGas(), walletBalances={(Chain.ARBITRUM, Stable.USDT): 3.0})
        graphSolve(graph, _params())

        assert _walletDeposits(graph, Chain.ARBITRUM, lighter) == pytest.approx(1.0)
        assert _withdrawn(graph, aden) == pytest.approx(0.0)
        assert _bridged(graph) == pytest.approx(0.0)

    def test_wallet_balance_is_an_upper_bound_and_dex_covers_the_rest(self):
        """Deficit $5, wallet holds $3: the wallet alone can't cover it, so
        Aden must be withdrawn and bridged for at least $2. Fees are
        fixed-charge (paid once per used edge, not per dollar), so once the
        withdraw+bridge edges are open the solver is indifferent between
        "3 from the wallet + 2 bridged" and "5 bridged" — both cost the
        same. Assert the guarantees, not the tie-break: the deficit is
        filled, the wallet never overdraws, and the bridged amount covers
        exactly what the wallet didn't."""
        aden = _dex("Aden", [Chain.BSC], Stable.USDT)
        aden.withdrawBalances = {Stable.USDT: 1000.0}
        lighter = _dex("Lighter", [Chain.ARBITRUM], Stable.USDT)
        lighter.inbalance = -5.0

        graph = Graph([aden, lighter], swapList=[], gasFeeService=_FlatGas(), walletBalances={(Chain.ARBITRUM, Stable.USDT): 3.0})
        graphSolve(graph, _params())

        deposited = _walletDeposits(graph, Chain.ARBITRUM, lighter)
        withdrawn = _withdrawn(graph, aden)
        assert deposited == pytest.approx(5.0)
        assert _bridged(graph) == pytest.approx(withdrawn)
        assert 2.0 - 1e-6 <= withdrawn <= 5.0 + 1e-6  # wallet contributes between 0 and its $3
        assert deposited - withdrawn <= 3.0 + 1e-6

    def test_wallet_with_zero_balance_is_pure_transit(self):
        aden = _dex("Aden", [Chain.BSC], Stable.USDT)
        aden.withdrawBalances = {Stable.USDT: 1000.0}
        lighter = _dex("Lighter", [Chain.ARBITRUM], Stable.USDT)
        lighter.inbalance = -1.0

        graph = Graph([aden, lighter], swapList=[], gasFeeService=_FlatGas())
        graphSolve(graph, _params())

        assert _withdrawn(graph, aden) == pytest.approx(1.0)
        assert _bridged(graph) == pytest.approx(1.0)

    def test_wallet_only_plan_with_no_dex_surplus_is_feasible(self):
        """No DEX surplus at all: bridge/swap capacity must still allow the
        wallet money to travel (computeAllCapacities counts wallet balances)."""
        lighter = _dex("Lighter", [Chain.ARBITRUM], Stable.USDT)
        lighter.inbalance = -2.0

        graph = Graph([lighter], swapList=[], gasFeeService=_FlatGas(), walletBalances={(Chain.BSC, Stable.USDT): 10.0})
        graphSolve(graph, _params())

        assert _bridged(graph) == pytest.approx(2.0)
        assert _walletDeposits(graph, Chain.ARBITRUM, lighter) == pytest.approx(2.0)

    def test_journey_decomposition_starts_at_the_wallet(self):
        aden = _dex("Aden", [Chain.BSC], Stable.USDT)
        aden.withdrawBalances = {Stable.USDT: 1000.0}
        lighter = _dex("Lighter", [Chain.ARBITRUM], Stable.USDT)
        lighter.inbalance = -1.0
        graph = Graph([aden, lighter], swapList=[], gasFeeService=_FlatGas(), walletBalances={(Chain.ARBITRUM, Stable.USDT): 3.0})
        graphSolve(graph, _params())

        journeys = decomposeJourneys(graph)

        assert len(journeys) == 1
        j = journeys[0]
        assert j.fromWallet is True
        assert j.fromDex == "Wallet ARBITRUM/USDT"
        assert j.toDex == "Lighter"
        assert j.amount == pytest.approx(1.0)
        assert len(j.hops) == 1 and j.hops[0].u.type == NodeType.Wallet
