import pytest

from connectors import chain_block_times
from graph import costing
from graph.edge import Edge
from graph.node import SourceNode, WalletNode, WithdrawNode
from graph.structures.DEXes import DEX, Chain, MeasuredDelay, Stable


def _dex() -> DEX:
    dex = DEX([Chain.BSC], [Stable.USDT], name="MEXC")
    dex.withdrawDelaySecondsByChain[Chain.BSC] = 300.0
    dex.depositDelaySecondsByChain[Chain.BSC] = 60.0
    return dex


def _withdrawEdge(dex: DEX) -> Edge:
    return Edge(WithdrawNode(Stable.USDT, nodeIndex=0, dex=dex, balance=0.0), WalletNode(Chain.BSC, Stable.USDT, nodeIndex=1))


def _depositEdge(dex: DEX) -> Edge:
    return Edge(WalletNode(Chain.BSC, Stable.USDT, nodeIndex=0), SourceNode(balance=0.0, nodeIndex=1, dex=dex))


@pytest.fixture(autouse=True)
def _fixedBlockDelay(monkeypatch):
    monkeypatch.setattr(chain_block_times, "get_block_delay_seconds", lambda chain: 10.0)


class TestWithdraw:
    def test_without_measurement_the_configured_delay_is_used(self):
        edge = _withdrawEdge(_dex())

        assert costing.computeDelay(edge) == pytest.approx(300.0)
        assert costing.delaySource(edge) == costing.DELAY_SOURCE_CONFIGURED

    def test_measurement_overrides_configured_delay_from_a_single_sample(self):
        dex = _dex()
        dex.measuredWithdrawDelayByChain[Chain.BSC] = MeasuredDelay(meanSeconds=50.4, samplesSeconds=[50.4])
        edge = _withdrawEdge(dex)

        assert costing.computeDelay(edge) == pytest.approx(50.4)
        assert costing.delaySource(edge) == costing.DELAY_SOURCE_MEASURED
        # The configured formula remains available untouched, for display.
        assert costing.computeConfiguredDelay(edge) == pytest.approx(300.0)

    def test_measurement_on_another_chain_does_not_apply(self):
        dex = DEX([Chain.BSC, Chain.ARBITRUM], [Stable.USDT], name="MEXC")
        dex.withdrawDelaySecondsByChain[Chain.BSC] = 300.0
        dex.measuredWithdrawDelayByChain[Chain.ARBITRUM] = MeasuredDelay(meanSeconds=5.0, samplesSeconds=[5.0])

        assert costing.computeDelay(_withdrawEdge(dex)) == pytest.approx(300.0)


class TestDeposit:
    def test_without_measurement_block_delay_plus_configured(self):
        edge = _depositEdge(_dex())

        assert costing.computeDelay(edge) == pytest.approx(10.0 + 60.0)
        assert costing.delaySource(edge) == costing.DELAY_SOURCE_CONFIGURED

    def test_measurement_replaces_the_whole_sum_without_adding_block_delay(self):
        dex = _dex()
        dex.measuredDepositDelayByChain[Chain.BSC] = MeasuredDelay(meanSeconds=45.5, samplesSeconds=[45.5])
        edge = _depositEdge(dex)

        assert costing.computeDelay(edge) == pytest.approx(45.5)  # NOT 45.5 + 10
        assert costing.delaySource(edge) == costing.DELAY_SOURCE_MEASURED
        assert costing.computeConfiguredDelay(edge) == pytest.approx(70.0)


class TestMeasuredDelayStats:
    def test_std_min_max_over_samples(self):
        m = MeasuredDelay(meanSeconds=20.0, samplesSeconds=[10.0, 20.0, 30.0])

        assert m.n == 3
        assert m.stdSeconds == pytest.approx(10.0)
        assert m.minSeconds == pytest.approx(10.0)
        assert m.maxSeconds == pytest.approx(30.0)

    def test_single_sample_has_zero_std(self):
        assert MeasuredDelay(meanSeconds=7.0, samplesSeconds=[7.0]).stdSeconds == 0.0
