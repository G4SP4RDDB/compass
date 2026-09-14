import json

import pytest

from compass_test import calibration
from compass_test import models
from compass_test.models import ExecutedHop, HopComparison, HopType, JourneyComparison, PlannedHop
from connectors import dex_measured_delays
from connectors.dex_measured_delays import DEPOSIT_FIELD, WITHDRAW_FIELD, apply_measured_delays
from graph.structures.DEXes import DEX, Chain, Stable


def _report(runId: str, hops: list[tuple[str, HopType, str, float, float, bool, str]]) -> models.TestRunReport:
    """hops: (dex, hopType, chain, startedAt, finishedAt, live, status)."""
    comparisons = [
        HopComparison(
            planned=PlannedHop(hopType=hopType, dex=dex, chain=chain, stable="USDT", estimatedCostUsd=0.0, estimatedTimeSeconds=300.0),
            executed=ExecutedHop(live=live, startedAt=started, finishedAt=finished, amountRequestedUsd=1.0, actualCostUsd=0.0, status=status),
        )
        for dex, hopType, chain, started, finished, live, status in hops
    ]
    return models.TestRunReport(runId=runId, createdAt=0.0, live=True, journeys=[JourneyComparison(fromDex="A", toDex="B", stable="USDT", hops=comparisons)])


class TestComputeMeasuredDelays:
    def test_single_live_ok_hop_becomes_the_measured_value(self):
        reports = [_report("r1", [("Aden", HopType.WITHDRAW, "BSC", 100.0, 136.5, True, "ok")])]

        measured = calibration.compute_measured_delays(reports)

        entry = measured["Aden"]["BSC"][WITHDRAW_FIELD]
        assert entry["meanSeconds"] == pytest.approx(36.5)
        assert entry["n"] == 1
        assert entry["sampleRunIds"] == ["r1"]
        assert DEPOSIT_FIELD not in measured["Aden"]["BSC"]

    def test_two_samples_are_averaged(self):
        reports = [
            _report("r1", [("Hyperliquid", HopType.WITHDRAW, "ARBITRUM", 0.0, 92.0, True, "ok")]),
            _report("r2", [("Hyperliquid", HopType.WITHDRAW, "ARBITRUM", 1000.0, 1228.0, True, "ok")]),
        ]

        entry = calibration.compute_measured_delays(reports)["Hyperliquid"]["ARBITRUM"][WITHDRAW_FIELD]

        assert entry["meanSeconds"] == pytest.approx(160.0)
        assert entry["n"] == 2
        assert entry["minSeconds"] == pytest.approx(92.0)
        assert entry["maxSeconds"] == pytest.approx(228.0)

    def test_dry_run_error_and_unconfirmed_hops_are_excluded(self):
        reports = [
            _report(
                "r1",
                [
                    ("MEXC", HopType.WITHDRAW, "BSC", 0.0, 10.0, False, "dry_run"),
                    ("MEXC", HopType.WITHDRAW, "BSC", 0.0, 600.0, True, "unconfirmed"),
                    ("MEXC", HopType.WITHDRAW, "BSC", 0.0, 5.0, True, "error"),
                    ("MEXC", HopType.DEPOSIT, "BSC", 0.0, 45.0, True, "ok"),
                ],
            )
        ]

        measured = calibration.compute_measured_delays(reports)

        assert WITHDRAW_FIELD not in measured["MEXC"]["BSC"]
        assert measured["MEXC"]["BSC"][DEPOSIT_FIELD]["meanSeconds"] == pytest.approx(45.0)

    def test_dex_without_any_live_run_has_no_entry_at_all(self):
        reports = [_report("r1", [("Lighter", HopType.WITHDRAW, "ARBITRUM", 0.0, 10.0, False, "dry_run")])]

        assert calibration.compute_measured_delays(reports) == {}

    def test_only_the_last_max_samples_count_ordered_by_finish_time(self, monkeypatch):
        monkeypatch.setattr(dex_measured_delays, "MAX_SAMPLES", 3)
        # Reports listed out of chronological order on purpose: ordering
        # must follow the hop's finishedAt, not the list order.
        reports = [
            _report("old", [("Aster", HopType.WITHDRAW, "BSC", 0.0, 1000.0, True, "ok")]),  # 1000s, oldest, must drop
            _report("r3", [("Aster", HopType.WITHDRAW, "BSC", 3000.0, 3030.0, True, "ok")]),  # 30s
            _report("r1", [("Aster", HopType.WITHDRAW, "BSC", 1000.0, 1010.0, True, "ok")]),  # 10s
            _report("r2", [("Aster", HopType.WITHDRAW, "BSC", 2000.0, 2020.0, True, "ok")]),  # 20s
        ]

        entry = calibration.compute_measured_delays(reports)["Aster"]["BSC"][WITHDRAW_FIELD]

        assert entry["n"] == 3
        assert entry["sampleRunIds"] == ["r1", "r2", "r3"]
        assert entry["meanSeconds"] == pytest.approx(20.0)

    def test_grouping_is_per_chain(self):
        reports = [
            _report("r1", [("MEXC", HopType.WITHDRAW, "BSC", 0.0, 50.0, True, "ok")]),
            _report("r2", [("MEXC", HopType.WITHDRAW, "ARBITRUM", 0.0, 90.0, True, "ok")]),
        ]

        measured = calibration.compute_measured_delays(reports)

        assert measured["MEXC"]["BSC"][WITHDRAW_FIELD]["meanSeconds"] == pytest.approx(50.0)
        assert measured["MEXC"]["ARBITRUM"][WITHDRAW_FIELD]["meanSeconds"] == pytest.approx(90.0)


class TestRoundTripAndApply:
    def test_saved_json_reloads_and_applies_to_the_dex_only_where_measured(self, tmp_path):
        reports = [
            _report("r1", [("MEXC", HopType.WITHDRAW, "BSC", 0.0, 50.0, True, "ok")]),
            _report("r2", [("MEXC", HopType.DEPOSIT, "BSC", 0.0, 45.0, True, "ok")]),
        ]
        path = tmp_path / "measured.json"
        dex_measured_delays.save_measured_delays(calibration.compute_measured_delays(reports), path)
        assert json.loads(path.read_text())["MEXC"]["BSC"][WITHDRAW_FIELD]["n"] == 1

        mexc = DEX([Chain.BSC, Chain.ARBITRUM], [Stable.USDT], name="MEXC")
        aster = DEX([Chain.BSC], [Stable.USDT], name="Aster")
        apply_measured_delays([mexc, aster], dex_measured_delays.load_measured_delays(path))

        assert mexc.measuredWithdrawDelayByChain[Chain.BSC].meanSeconds == pytest.approx(50.0)
        assert mexc.measuredDepositDelayByChain[Chain.BSC].meanSeconds == pytest.approx(45.0)
        assert Chain.ARBITRUM not in mexc.measuredWithdrawDelayByChain  # never measured -> config stays
        assert aster.measuredWithdrawDelayByChain == {}
        # Configured values are untouched by a measurement.
        assert mexc.withdrawDelaySecondsByChain[Chain.BSC] == pytest.approx(300.0)

    def test_missing_file_means_no_measurements(self, tmp_path):
        assert dex_measured_delays.load_measured_delays(tmp_path / "absent.json") == {}

    def test_reapplying_clears_stale_measurements(self):
        mexc = DEX([Chain.BSC], [Stable.USDT], name="MEXC")
        reports = [_report("r1", [("MEXC", HopType.WITHDRAW, "BSC", 0.0, 50.0, True, "ok")])]
        apply_measured_delays([mexc], calibration.compute_measured_delays(reports))
        assert Chain.BSC in mexc.measuredWithdrawDelayByChain

        apply_measured_delays([mexc], {})

        assert mexc.measuredWithdrawDelayByChain == {}
