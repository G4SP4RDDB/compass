"""Swap hop plumbing — executor dispatch, dry-run math, cap gating, journey
carry-over, plan_loader edge mapping, report round trip and calibration —
with the CoW runner and chain access stubbed out. No network, no key."""

from __future__ import annotations

import pytest

from compass_test import calibration, chain_ops, config, executor, models, plan_loader
from compass_test.models import ExecutedHop, HopComparison, HopType, JourneyComparison, PlannedHop
from connectors import cowswap
from connectors.cowswap import COWSWAP_VENUE_NAME
from connectors.dex_measured_delays import SWAP_FIELD, apply_measured_delays, measured_swap_delay
from graph.edge import Edge, EdgeType
from graph.node import WalletNode
from graph.structures.DEXes import Chain, Stable


def _planned_swap(chain="ARBITRUM", stable="USDC", to_stable="USDT") -> PlannedHop:
    return PlannedHop(
        hopType=HopType.SWAP, dex=COWSWAP_VENUE_NAME, chain=chain, stable=stable, toStable=to_stable,
        estimatedCostUsd=0.02, estimatedTimeSeconds=3.0,
    )


class _Wallet:
    address = "0x477899D0e04E8b510ADA7EcD1Db22d1BdF3F1650"


class _FakeRunner:
    """Quotes $1 -> $0.9869 (fee $0.0132), like the real Arbitrum quote of
    2026-09-10; allowance shortfall configurable."""

    name = COWSWAP_VENUE_NAME

    def __init__(self, shortfall=0):
        self.shortfall = shortfall
        self.placed = []

    @staticmethod
    def supports(chain, stable_in, stable_out):
        return chain in (Chain.BSC, Chain.ARBITRUM) and stable_in != stable_out

    def quote(self, chain, stable_in, stable_out, amount_usd, from_address):
        units_in = 10 ** 6 if chain == Chain.ARBITRUM else 10 ** 18
        return cowswap.CowQuote(
            chain=chain, sell_token="0xsell", buy_token="0xbuy", receiver=from_address,
            sell_amount=round(amount_usd * 0.98676 * units_in), fee_amount=round(amount_usd * 0.01324 * units_in),
            buy_amount=round(amount_usd * 0.986882 * units_in), valid_to=1_800_000_000, quote_id=1, verified=True, expiration="",
        )

    def allowance_shortfall_units(self, w3, chain, stable_in, owner, sell_units):
        return self.shortfall

    def build_approve_tx(self, w3, chain, stable_in, owner, amount_usd):
        return {"gas": 50_000, "gasPrice": 10 ** 9}


@pytest.fixture(autouse=True)
def _no_chain(monkeypatch):
    monkeypatch.setattr(chain_ops, "get_web3", lambda chain: object())
    monkeypatch.setattr(chain_ops, "estimate_dry_run_gas_cost_usd", lambda w3, chain, tx: 0.005)
    monkeypatch.setattr(config, "MAX_USD_PER_HOP", 10.0)
    monkeypatch.setattr(config, "MAX_USD_PER_RUN", 20.0)


class TestExecutorDryRun:
    def test_dry_run_cost_is_sold_minus_quoted_buy(self):
        result = executor.run_hop(_planned_swap(), None, 1.0, _Wallet(), live=False, swap_runner=_FakeRunner())

        assert result.status == "dry_run"
        assert result.actualCostUsd == pytest.approx(1.0 - 0.986882, abs=1e-6)
        assert result.amountReceivedUsd == pytest.approx(0.986882, abs=1e-6)
        assert "no approve tx needed" in result.notes

    def test_dry_run_adds_simulated_approve_gas_when_allowance_is_short(self):
        result = executor.run_hop(_planned_swap(), None, 1.0, _Wallet(), live=False, swap_runner=_FakeRunner(shortfall=5))

        assert result.actualCostUsd == pytest.approx(1.0 - 0.986882 + 0.005, abs=1e-6)
        assert "approve" in result.notes

    def test_bsc_18_decimals_are_honoured(self):
        result = executor.run_hop(_planned_swap(chain="BSC", stable="USDT", to_stable="USDC"), None, 2.0, _Wallet(), live=False, swap_runner=_FakeRunner())

        assert result.amountReceivedUsd == pytest.approx(2 * 0.986882, abs=1e-6)

    def test_unsupported_pair_is_an_error_not_an_exception(self):
        result = executor.run_hop(_planned_swap(chain="ETHEREUM"), None, 1.0, _Wallet(), live=False, swap_runner=_FakeRunner())
        assert result.status == "error"

    def test_caps_apply_to_swaps_too(self):
        with pytest.raises(executor.SafetyCapError, match="MAX_USD_PER_HOP"):
            executor.run_hop(_planned_swap(), None, 10.01, _Wallet(), live=False, swap_runner=_FakeRunner())

    def test_live_gate_applies_to_swaps_too(self, monkeypatch):
        monkeypatch.setattr(config, "ALLOW_LIVE", False)
        with pytest.raises(executor.SafetyCapError, match="COMPASS_TEST_ALLOW_LIVE"):
            executor.run_hop(_planned_swap(), None, 1.0, _Wallet(), live=True, swap_runner=_FakeRunner())


class TestJourneyCarry:
    def test_swap_hop_gets_no_dex_connector_and_carries_received_amount_when_live(self, monkeypatch):
        calls = []

        def fake_run_hop(planned, connector, amount, wallet, live, spent_so_far=0.0):
            calls.append((planned.hopType, connector, amount))
            received = 0.9 if planned.hopType in (HopType.WITHDRAW, HopType.SWAP) else None
            return ExecutedHop(live=live, startedAt=0.0, finishedAt=1.0, amountRequestedUsd=amount, actualCostUsd=0.0, amountReceivedUsd=received)

        monkeypatch.setattr(executor, "run_hop", fake_run_hop)
        hops = [
            PlannedHop(hopType=HopType.WITHDRAW, dex="MEXC", chain="ARBITRUM", stable="USDT", estimatedCostUsd=0, estimatedTimeSeconds=0),
            _planned_swap(stable="USDT", to_stable="USDC"),
            PlannedHop(hopType=HopType.DEPOSIT, dex="Hyperliquid", chain="ARBITRUM", stable="USDC", estimatedCostUsd=0, estimatedTimeSeconds=0),
        ]
        executor.run_journey_hops(hops, connector_for_dex=lambda name: f"conn:{name}", amount_usd=5.0, wallet=_Wallet(), live=True)

        assert calls[0] == (HopType.WITHDRAW, "conn:MEXC", 5.0)
        assert calls[1] == (HopType.SWAP, None, 0.9)
        assert calls[2] == (HopType.DEPOSIT, "conn:Hyperliquid", 0.9)


class TestPlanLoader:
    def test_swap_edge_becomes_a_swap_hop_with_solver_all_in_estimate(self):
        edge = Edge(WalletNode(Chain.BSC, Stable.USDT, 0), WalletNode(Chain.BSC, Stable.USDC, 1), type=EdgeType.Swap)
        edge.cost, edge.realizedSlippageUsd, edge.time, edge.flow = 0.05, 0.4, 3.0, 100.0

        hop = plan_loader._planned_hop_from_edge(edge)

        assert hop.hopType == HopType.SWAP and hop.dex == COWSWAP_VENUE_NAME
        assert (hop.chain, hop.stable, hop.toStable) == ("BSC", "USDT", "USDC")
        assert hop.estimatedCostUsd == pytest.approx(0.45)
        assert hop.solvedFlowUsd == 100.0


class TestReportAndCalibration:
    def test_to_stable_survives_the_json_round_trip_and_defaults_empty(self):
        hop = _planned_swap()
        assert PlannedHop.from_dict(hop.to_dict()).toStable == "USDT"
        legacy = dict(hop.to_dict())
        del legacy["toStable"]
        assert PlannedHop.from_dict(legacy).toStable == ""

    def test_live_ok_swap_becomes_a_measured_swap_delay_costing_can_read(self):
        report = models.TestRunReport(
            runId="r1", createdAt=0.0, live=True,
            journeys=[JourneyComparison(fromDex="A", toDex="B", stable="USDT", hops=[HopComparison(
                planned=_planned_swap(chain="BSC"),
                executed=ExecutedHop(live=True, startedAt=10.0, finishedAt=55.0, amountRequestedUsd=1.0, actualCostUsd=0.01, status="ok"),
            )])],
        )
        measured = calibration.compute_measured_delays([report])

        assert measured[COWSWAP_VENUE_NAME]["BSC"][SWAP_FIELD]["meanSeconds"] == pytest.approx(45.0)

        apply_measured_delays([], measured)
        assert measured_swap_delay(Chain.BSC).meanSeconds == pytest.approx(45.0)
        assert measured_swap_delay(Chain.ARBITRUM) is None
        apply_measured_delays([], {})
        assert measured_swap_delay(Chain.BSC) is None
