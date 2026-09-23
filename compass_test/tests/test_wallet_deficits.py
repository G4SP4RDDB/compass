import pytest

from connectors import wallet_deficits as wd


class TestValidate:
    def test_deficit_round_trips(self):
        cleaned = wd.validate_wallet_deficits({"kind": "deficit", "amountUsd": 250.0})
        assert cleaned == {"kind": "deficit", "amountUsd": 250.0}

    def test_null_or_no_kind_removes_the_entry(self):
        assert wd.validate_wallet_deficits(None) == {}
        assert wd.validate_wallet_deficits({"kind": None, "amountUsd": 3}) == {}

    @pytest.mark.parametrize(
        "payload, match",
        [
            ({"kind": "surplus", "amountUsd": 1}, "kind must be"),
            ({"kind": "deficit", "amountUsd": 0}, "amountUsd"),
            ({"kind": "deficit", "amountUsd": "5"}, "amountUsd"),
            ("not a dict", "must be a JSON object"),
        ],
    )
    def test_rejects_bad_entries_with_a_readable_message(self, payload, match):
        with pytest.raises(ValueError, match=match):
            wd.validate_wallet_deficits(payload)


class TestApply:
    def test_converts_to_a_single_negative_usd_amount(self):
        assert wd.apply_wallet_deficits({"kind": "deficit", "amountUsd": 250.0}) == -250.0

    def test_empty_is_no_deficit(self):
        assert wd.apply_wallet_deficits({}) == 0.0


class TestTotal:
    def test_reads_the_amount(self):
        assert wd.total_wallet_deficit_usd({"kind": "deficit", "amountUsd": 250.0}) == 250.0

    def test_empty_is_zero(self):
        assert wd.total_wallet_deficit_usd({}) == 0.0


class TestRoundTripFile:
    def test_round_trip_file(self, tmp_path):
        path = tmp_path / "wd.json"
        data = {"kind": "deficit", "amountUsd": 250.0}
        wd.save_wallet_deficits(data, path)
        assert wd.load_wallet_deficits(path) == data
        assert wd.load_wallet_deficits(tmp_path / "absent.json") == {}
