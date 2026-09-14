import pytest

from connectors import dex_imbalances as di
from graph.structures.DEXes import DEX, Chain, Stable


def _registry() -> dict[str, DEX]:
    aster = DEX([Chain.BSC], [Stable.USDT], name="Aster")
    aster.requiresSameChainWithdraw = True
    return {
        "MEXC": DEX([Chain.BSC, Chain.ARBITRUM], [Stable.USDT], name="MEXC"),
        "Hyperliquid": DEX([Chain.ARBITRUM], [Stable.USDC], name="Hyperliquid"),
        "Aster": aster,
    }


class TestValidate:
    def test_surplus_and_deficit_round_trip(self):
        cleaned = di.validate_dex_imbalances(
            {"MEXC": {"kind": "surplus", "amountUsd": 20, "stable": "USDT"}, "Hyperliquid": {"kind": "deficit", "amountUsd": 5.5}},
            _registry(),
        )
        assert cleaned["MEXC"] == {"kind": "surplus", "amountUsd": 20.0, "stable": "USDT", "chain": None}
        assert cleaned["Hyperliquid"] == {"kind": "deficit", "amountUsd": 5.5}

    def test_null_or_no_kind_removes_the_entry(self):
        cleaned = di.validate_dex_imbalances({"MEXC": None, "Hyperliquid": {"kind": None, "amountUsd": 3}}, _registry())
        assert cleaned == {}

    def test_surplus_defaults_stable_to_the_dexs_only_stable(self):
        cleaned = di.validate_dex_imbalances({"MEXC": {"kind": "surplus", "amountUsd": 1}}, _registry())
        assert cleaned["MEXC"]["stable"] == "USDT"

    @pytest.mark.parametrize(
        "payload, match",
        [
            ({"Nope": {"kind": "deficit", "amountUsd": 1}}, "unknown DEX"),
            ({"MEXC": {"kind": "both", "amountUsd": 1}}, "kind must be"),
            ({"MEXC": {"kind": "deficit", "amountUsd": 0}}, "amountUsd"),
            ({"MEXC": {"kind": "deficit", "amountUsd": "5"}}, "amountUsd"),
            ({"MEXC": {"kind": "surplus", "amountUsd": 1, "stable": "USDC"}}, "stable must be"),
            ({"MEXC": {"kind": "surplus", "amountUsd": 1, "chain": "SOLANA"}}, "chain must be"),
            ({"Aster": {"kind": "surplus", "amountUsd": 1}}, "pick a chain"),
        ],
    )
    def test_rejects_bad_entries_with_a_readable_message(self, payload, match):
        with pytest.raises(ValueError, match=match):
            di.validate_dex_imbalances(payload, _registry())

    def test_same_chain_dex_surplus_keeps_its_chain(self):
        cleaned = di.validate_dex_imbalances({"Aster": {"kind": "surplus", "amountUsd": 4, "chain": "BSC"}}, _registry())
        assert cleaned["Aster"]["chain"] == "BSC"


class TestApply:
    def test_sets_deficit_surplus_and_resets_untouched_dexes(self):
        reg = _registry()
        reg["Hyperliquid"].inbalance = -99.0  # stale from a previous apply
        di.apply_dex_imbalances(
            list(reg.values()),
            {
                "MEXC": {"kind": "surplus", "amountUsd": 20.0, "stable": "USDT", "chain": None},
                "Aster": {"kind": "surplus", "amountUsd": 4.0, "stable": "USDT", "chain": "BSC"},
            },
        )
        assert reg["MEXC"].inbalance == 20.0
        assert reg["MEXC"].withdrawBalances == {Stable.USDT: 20.0}
        assert reg["MEXC"].withdrawChainByStable == {}
        assert reg["Aster"].withdrawChainByStable == {Stable.USDT: Chain.BSC}
        assert reg["Hyperliquid"].inbalance == 0.0
        assert reg["Hyperliquid"].withdrawBalances == {}

    def test_deficit_is_negative_inbalance_without_withdrawable(self):
        reg = _registry()
        di.apply_dex_imbalances(list(reg.values()), {"Hyperliquid": {"kind": "deficit", "amountUsd": 5.5}})
        assert reg["Hyperliquid"].inbalance == -5.5
        assert reg["Hyperliquid"].withdrawBalances == {}


class TestFeasibility:
    def test_surplus_may_exceed_deficit(self):
        summary = di.check_feasibility(
            {"MEXC": {"kind": "surplus", "amountUsd": 20.0}, "Hyperliquid": {"kind": "deficit", "amountUsd": 5.0}}
        )
        assert summary.feasible and summary.problem is None
        assert summary.totalSurplusUsd == 20.0 and summary.totalDeficitUsd == 5.0

    def test_deficit_above_surplus_is_rejected_with_the_gap(self):
        with pytest.raises(di.InfeasibleImbalancesError, match=r"exceeds total surplus.*\$3\.00"):
            di.check_feasibility({"MEXC": {"kind": "surplus", "amountUsd": 2.0}, "Hyperliquid": {"kind": "deficit", "amountUsd": 5.0}})

    def test_deficit_without_any_surplus_is_rejected(self):
        with pytest.raises(di.InfeasibleImbalancesError, match="no DEX has a surplus"):
            di.check_feasibility({"Hyperliquid": {"kind": "deficit", "amountUsd": 5.0}})

    def test_no_deficit_is_fine_even_with_nothing_set(self):
        assert di.check_feasibility({}).problem is None
        assert di.check_feasibility({"MEXC": {"kind": "surplus", "amountUsd": 2.0}}).problem is None

    def test_cent_rounding_does_not_flag_float_noise(self):
        assert di.check_feasibility({"MEXC": {"kind": "surplus", "amountUsd": 0.1 + 0.2}, "Hyperliquid": {"kind": "deficit", "amountUsd": 0.3}}).feasible

    def test_round_trip_file(self, tmp_path):
        path = tmp_path / "imb.json"
        data = {"MEXC": {"kind": "surplus", "amountUsd": 20.0, "stable": "USDT", "chain": None}}
        di.save_dex_imbalances(data, path)
        assert di.load_dex_imbalances(path) == data
        assert di.load_dex_imbalances(tmp_path / "absent.json") == {}
