import pytest

from connectors import wallet_deficits as wd
from graph.structures.DEXes import Stable


class TestValidate:
    def test_deficit_round_trips(self):
        cleaned = wd.validate_wallet_deficits({"USDC": {"kind": "deficit", "amountUsd": 250.0}})
        assert cleaned == {"USDC": {"kind": "deficit", "amountUsd": 250.0}}

    def test_null_or_no_kind_removes_the_entry(self):
        cleaned = wd.validate_wallet_deficits({"USDC": None, "USDT": {"kind": None, "amountUsd": 3}})
        assert cleaned == {}

    @pytest.mark.parametrize(
        "payload, match",
        [
            ({"BTC": {"kind": "deficit", "amountUsd": 1}}, "unknown stable"),
            ({"USDC": {"kind": "surplus", "amountUsd": 1}}, "kind must be"),
            ({"USDC": {"kind": "deficit", "amountUsd": 0}}, "amountUsd"),
            ({"USDC": {"kind": "deficit", "amountUsd": "5"}}, "amountUsd"),
        ],
    )
    def test_rejects_bad_entries_with_a_readable_message(self, payload, match):
        with pytest.raises(ValueError, match=match):
            wd.validate_wallet_deficits(payload)


class TestApply:
    def test_converts_to_negative_balance_by_stable(self):
        assert wd.apply_wallet_deficits({"USDC": {"kind": "deficit", "amountUsd": 250.0}}) == {Stable.USDC: -250.0}

    def test_empty_is_no_deficit(self):
        assert wd.apply_wallet_deficits({}) == {}


class TestTotal:
    def test_sums_every_stable(self):
        deficits = {"USDC": {"kind": "deficit", "amountUsd": 250.0}, "USDT": {"kind": "deficit", "amountUsd": 10.0}}
        assert wd.total_wallet_deficit_usd(deficits) == 260.0


class TestRoundTripFile:
    def test_round_trip_file(self, tmp_path):
        path = tmp_path / "wd.json"
        data = {"USDC": {"kind": "deficit", "amountUsd": 250.0}}
        wd.save_wallet_deficits(data, path)
        assert wd.load_wallet_deficits(path) == data
        assert wd.load_wallet_deficits(tmp_path / "absent.json") == {}
