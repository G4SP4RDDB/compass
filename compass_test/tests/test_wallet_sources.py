import pytest

from connectors import wallet_sources as ws
from graph.structures.DEXes import Chain, Stable

ARB_USDC = (Chain.ARBITRUM, Stable.USDC)
BSC_USDT = (Chain.BSC, Stable.USDT)


class TestResolve:
    def test_live_balance_used_by_default(self):
        assert ws.resolve_wallet_balances({ARB_USDC: 3.0}, {}) == {ARB_USDC: 3.0}

    def test_disabled_pair_is_zero(self):
        assert ws.resolve_wallet_balances({ARB_USDC: 3.0}, {"ARBITRUM/USDC": {"enabled": False, "overrideUsd": None}}) == {ARB_USDC: 0.0}

    def test_override_replaces_live(self):
        assert ws.resolve_wallet_balances({ARB_USDC: 3.0}, {"ARBITRUM/USDC": {"enabled": True, "overrideUsd": 1.5}}) == {ARB_USDC: 1.5}

    def test_unknown_live_balance_is_zero_unless_overridden(self):
        assert ws.resolve_wallet_balances({ARB_USDC: None}, {}) == {ARB_USDC: 0.0}
        assert ws.resolve_wallet_balances({ARB_USDC: None}, {"ARBITRUM/USDC": {"enabled": True, "overrideUsd": 2.0}}) == {ARB_USDC: 2.0}

    def test_pairs_are_independent(self):
        resolved = ws.resolve_wallet_balances({ARB_USDC: 3.0, BSC_USDT: 7.0}, {"BSC/USDT": {"enabled": False}})
        assert resolved == {ARB_USDC: 3.0, BSC_USDT: 0.0}


class TestValidate:
    def test_round_trip(self, tmp_path):
        cleaned = ws.validate_wallet_sources({"ARBITRUM/USDC": {"enabled": False}, "BSC/USDT": {"overrideUsd": 2}})
        assert cleaned == {"ARBITRUM/USDC": {"enabled": False, "overrideUsd": None}, "BSC/USDT": {"enabled": True, "overrideUsd": 2.0}}
        path = tmp_path / "ws.json"
        ws.save_wallet_sources(cleaned, path)
        assert ws.load_wallet_sources(path) == cleaned
        assert ws.load_wallet_sources(tmp_path / "absent.json") == {}

    @pytest.mark.parametrize(
        "payload, match",
        [
            ([], "JSON object"),
            ({"ARBITRUM": {}}, "invalid wallet key"),
            ({"MARS/USDC": {}}, "invalid wallet key"),
            ({"ARBITRUM/USDC": {"enabled": "yes"}}, "enabled must be"),
            ({"ARBITRUM/USDC": {"overrideUsd": -1}}, "overrideUsd"),
        ],
    )
    def test_rejects_bad_payloads(self, payload, match):
        with pytest.raises(ValueError, match=match):
            ws.validate_wallet_sources(payload)
