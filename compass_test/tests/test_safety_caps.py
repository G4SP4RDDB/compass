import pytest

from compass_test import config, executor
from compass_test.models import HopType, PlannedHop


def _planned() -> PlannedHop:
    return PlannedHop(hopType=HopType.WITHDRAW, dex="MEXC", chain="BSC", stable="USDT", estimatedCostUsd=0.1, estimatedTimeSeconds=60.0)


class TestPerHopCap:
    def test_amount_above_max_usd_per_hop_is_refused_before_touching_connector_or_wallet(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 10.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)

        with pytest.raises(executor.SafetyCapError, match="MAX_USD_PER_HOP"):
            executor.run_hop(_planned(), connector=None, amount_usd=10.01, wallet=None, live=False)

    def test_amount_at_or_below_cap_passes_the_cap_check(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 10.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)

        # Dry-run withdraw never touches `connector`/`wallet` before
        # returning, so a passing cap check is observable without any live
        # dependency — a raised SafetyCapError here would mean the cap logic
        # itself is wrong, not a downstream network/auth failure.
        result = executor.run_hop(_planned(), connector=None, amount_usd=10.0, wallet=None, live=False)
        assert result.status == "dry_run"


class TestPerRunCap:
    def test_cumulative_spend_above_max_usd_per_run_is_refused(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 10.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 15.0)

        with pytest.raises(executor.SafetyCapError, match="MAX_USD_PER_RUN"):
            executor.run_hop(_planned(), connector=None, amount_usd=10.0, wallet=None, live=False, spent_so_far=10.0)


class TestLiveGate:
    def test_live_run_refused_when_allow_live_env_flag_is_not_set(self, monkeypatch):
        monkeypatch.setattr(config, "ALLOW_LIVE", False)
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 10.0)
        monkeypatch.setattr(config, "MAX_USD_PER_RUN", 100.0)

        with pytest.raises(executor.SafetyCapError, match="COMPASS_TEST_ALLOW_LIVE"):
            executor.run_hop(_planned(), connector=None, amount_usd=1.0, wallet=None, live=True)

    def test_live_gate_is_checked_before_any_cap_math(self, monkeypatch):
        # Even an amount that would ALSO fail the cap check should surface
        # the live-gate error first — the more fundamental guard.
        monkeypatch.setattr(config, "ALLOW_LIVE", False)
        monkeypatch.setattr(config, "MAX_USD_PER_HOP", 1.0)

        with pytest.raises(executor.SafetyCapError, match="COMPASS_TEST_ALLOW_LIVE"):
            executor.run_hop(_planned(), connector=None, amount_usd=999.0, wallet=None, live=True)
