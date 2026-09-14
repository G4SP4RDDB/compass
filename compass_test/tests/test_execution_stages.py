"""executor.py's on_stage callback — the contract the frontend's live
execution animation depends on (see visualization/server.py's streamed
POST /api/test-hop and graph_template.html's runLiveHop/createStageTracker):
which (stage_code, domain) pairs fire, in what order, and that "onchain"
vs "exchange" is never mixed up between the two. No network, no key —
chain_ops/connector calls are all faked."""

from __future__ import annotations

from compass_test import chain_ops, config, executor
from compass_test.chain_ops import OnChainTxResult
from compass_test.models import HopType, PlannedHop
from connectors import cowswap
from connectors.cowswap import COWSWAP_VENUE_NAME
from graph.structures.DEXes import Chain, Stable


class _Wallet:
    address = "0x477899D0e04E8b510ADA7EcD1Db22d1BdF3F1650"

    def sign_transaction(self, tx):
        return object()


class _WithdrawResult:
    externalId = "ext-123"


class _DepositConnector:
    name = "Ondo Perps"

    def __init__(self):
        self._balance = 100.0

    def build_deposit_tx(self, w3, from_address, chain, stable, amount_usd):
        return {"gas": 21_000}

    def poll_balance_usd(self, stable):
        # Credited on the first poll after the on-chain leg — enough to
        # exercise the "waiting_dex" stage without a real sleep loop.
        self._balance += 1000.0
        return self._balance


class _WithdrawConnector:
    name = "MEXC"

    def withdraw(self, chain, stable, amount_usd, to_address):
        return _WithdrawResult()


def _planned(hop_type: HopType) -> PlannedHop:
    return PlannedHop(hopType=hop_type, dex="X", chain="ARBITRUM", stable="USDC", estimatedCostUsd=0, estimatedTimeSeconds=0)


def _fake_onchain_result() -> OnChainTxResult:
    return OnChainTxResult(
        tx_hash="0xdeadbeef", submitted_at=0.0, confirmed_at=1.0, gas_used=21_000,
        effective_gas_price_wei=1_000_000_000, gas_cost_usd=0.01,
    )


class TestDepositStages:
    def test_stage_order_and_domains(self, monkeypatch):
        monkeypatch.setattr(chain_ops, "get_web3", lambda chain: object())
        monkeypatch.setattr(chain_ops, "send_and_wait", lambda w3, chain, signed: _fake_onchain_result())
        monkeypatch.setattr(config, "ALLOW_LIVE", True)
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 100.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)
        monkeypatch.setattr(config, "POLL_TIMEOUT_SECONDS", 5.0)
        monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.01)

        seen = []
        executor.run_hop(
            _planned(HopType.DEPOSIT), _DepositConnector(), 10.0, _Wallet(), live=True,
            on_stage=lambda stage, message, domain: seen.append((stage, domain)),
        )

        assert seen == [
            ("preparing", "exchange"),
            ("signing", "onchain"),
            ("onchain_confirmed", "onchain"),
            ("waiting_dex", "exchange"),
        ]
        # The two halves of the trip must never be mislabeled: on-chain
        # confirmation and the DEX's own crediting are different failure
        # modes, and this is exactly the distinction that got lost before.
        assert [d for s, d in seen if s == "onchain_confirmed"] == ["onchain"]
        assert [d for s, d in seen if s == "waiting_dex"] == ["exchange"]

    def test_waiting_dex_message_quotes_the_solver_s_time_estimate(self, monkeypatch):
        monkeypatch.setattr(chain_ops, "get_web3", lambda chain: object())
        monkeypatch.setattr(chain_ops, "send_and_wait", lambda w3, chain, signed: _fake_onchain_result())
        monkeypatch.setattr(config, "ALLOW_LIVE", True)
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 100.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)
        monkeypatch.setattr(config, "POLL_TIMEOUT_SECONDS", 5.0)
        monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.01)
        planned = PlannedHop(
            hopType=HopType.DEPOSIT, dex="Ondo Perps", chain="ARBITRUM", stable="USDC",
            estimatedCostUsd=0, estimatedTimeSeconds=150.0,
        )

        messages = {}
        executor.run_hop(
            planned, _DepositConnector(), 10.0, _Wallet(), live=True,
            on_stage=lambda stage, message, domain: messages.setdefault(stage, message),
        )

        assert "usually ~2m30s" in messages["waiting_dex"]
        assert "Ondo Perps" in messages["waiting_dex"]

    def test_dry_run_emits_no_stages(self, monkeypatch):
        monkeypatch.setattr(chain_ops, "get_web3", lambda chain: object())
        monkeypatch.setattr(chain_ops, "estimate_dry_run_gas_cost_usd", lambda w3, chain, tx: 0.005)
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 100.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)

        seen = []
        executor.run_hop(
            _planned(HopType.DEPOSIT), _DepositConnector(), 10.0, _Wallet(), live=False,
            on_stage=lambda stage, message, domain: seen.append((stage, domain)),
        )

        assert seen == []  # a dry run finishes fast enough that staged progress has nothing to show


class TestWithdrawStages:
    def test_stage_order_and_domains(self, monkeypatch):
        monkeypatch.setattr(chain_ops, "get_web3", lambda chain: object())
        balances = iter([0.0, 10.0])  # before, then credited
        monkeypatch.setattr(chain_ops, "get_stable_balance_usd", lambda *a, **k: next(balances))
        monkeypatch.setattr(config, "ALLOW_LIVE", True)
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 100.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)
        monkeypatch.setattr(config, "POLL_TIMEOUT_SECONDS", 5.0)
        monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.01)

        seen = []
        executor.run_hop(
            _planned(HopType.WITHDRAW), _WithdrawConnector(), 10.0, _Wallet(), live=True,
            on_stage=lambda stage, message, domain: seen.append((stage, domain)),
        )

        assert seen == [
            ("requesting", "exchange"),
            ("dex_accepted", "exchange"),
            ("waiting_onchain", "onchain"),
        ]


class TestSwapStages:
    def test_stage_order_and_domains_without_approve(self, monkeypatch):
        monkeypatch.setattr(chain_ops, "get_web3", lambda chain: object())
        monkeypatch.setattr(chain_ops, "get_stable_balance_usd", lambda *a, **k: 0.0)
        monkeypatch.setattr(config, "ALLOW_LIVE", True)
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 100.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)
        monkeypatch.setattr(config, "POLL_TIMEOUT_SECONDS", 5.0)
        monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.01)

        class _Runner:
            name = COWSWAP_VENUE_NAME

            def supports(self, chain, stable_in, stable_out):
                return True

            def quote(self, chain, stable_in, stable_out, amount_usd, from_address):
                return cowswap.CowQuote(
                    chain=chain, sell_token="0xsell", buy_token="0xbuy", receiver=from_address,
                    sell_amount=1_000_000, fee_amount=10_000, buy_amount=986_000,
                    valid_to=1_800_000_000, quote_id=1, verified=True, expiration="",
                )

            def allowance_shortfall_units(self, w3, chain, stable_in, owner, sell_units):
                return 0

            def place_order(self, chain, quote, wallet):
                return type("Placed", (), {"uid": "uid-1", "order": quote})()

            def wait_for_settlement(self, chain, uid, timeout_s, interval_s):
                return type(
                    "Outcome", (), {
                        "status": cowswap.ORDER_STATUS_FULFILLED, "finished_at": 1.0,
                        "executed_buy_units": 986_000, "executed_sell_units": 1_000_000, "tx_hash": "0xsettled",
                    },
                )()

        seen = []
        planned = PlannedHop(
            hopType=HopType.SWAP, dex=COWSWAP_VENUE_NAME, chain="ARBITRUM", stable="USDC", toStable="USDT",
            estimatedCostUsd=0, estimatedTimeSeconds=0,
        )
        executor.run_hop(
            planned, None, 1.0, _Wallet(), live=True, swap_runner=_Runner(),
            on_stage=lambda stage, message, domain: seen.append((stage, domain)),
        )

        assert seen == [
            ("order_placed", "exchange"),
            ("waiting_settlement", "exchange"),
        ]
