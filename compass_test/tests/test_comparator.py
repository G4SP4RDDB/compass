import pytest

from compass_test import comparator
from compass_test.models import ExecutedHop, HopType, JourneyComparison, PlannedHop


def _planned(cost=1.0, time=60.0, hopType=HopType.WITHDRAW) -> PlannedHop:
    return PlannedHop(hopType=hopType, dex="MEXC", chain="BSC", stable="USDT", estimatedCostUsd=cost, estimatedTimeSeconds=time)


def _executed(cost=1.2, started=0.0, finished=90.0, live=True, status="ok") -> ExecutedHop:
    return ExecutedHop(live=live, startedAt=started, finishedAt=finished, amountRequestedUsd=5.0, actualCostUsd=cost, status=status)


class TestHopComparison:
    def test_cost_and_time_error_are_signed_differences(self):
        hc = comparator.compare_hop(_planned(cost=1.0, time=60.0), _executed(cost=1.5, started=0.0, finished=100.0))

        assert hc.costErrorUsd == pytest.approx(0.5)
        assert hc.costErrorPct == pytest.approx(50.0)
        assert hc.timeErrorSeconds == pytest.approx(40.0)
        assert hc.timeErrorPct == pytest.approx(40.0 / 60.0 * 100.0)

    def test_negative_error_when_actual_is_cheaper_or_faster(self):
        hc = comparator.compare_hop(_planned(cost=2.0, time=100.0), _executed(cost=1.0, started=0.0, finished=50.0))

        assert hc.costErrorUsd == pytest.approx(-1.0)
        assert hc.timeErrorSeconds == pytest.approx(-50.0)

    def test_unconfirmed_hop_has_no_cost_error(self):
        hc = comparator.compare_hop(_planned(), _executed(cost=None, status="unconfirmed"))

        assert hc.costErrorUsd is None
        assert hc.costErrorPct is None

    def test_zero_estimated_cost_yields_no_pct_error_but_keeps_absolute(self):
        hc = comparator.compare_hop(_planned(cost=0.0), _executed(cost=0.3))

        assert hc.costErrorUsd == pytest.approx(0.3)
        assert hc.costErrorPct is None  # would be division by zero, not "infinite accuracy"


class TestJourneyComparison:
    def test_totals_sum_across_hops(self):
        hops = [
            comparator.compare_hop(_planned(cost=1.0, time=60.0, hopType=HopType.WITHDRAW), _executed(cost=1.5, started=0.0, finished=70.0)),
            comparator.compare_hop(_planned(cost=0.01, time=30.0, hopType=HopType.DEPOSIT), _executed(cost=0.02, started=0.0, finished=45.0)),
        ]
        journey = JourneyComparison(fromDex="Aster", toDex="MEXC", stable="USDT", hops=hops)

        assert journey.totalEstimatedCostUsd == pytest.approx(1.01)
        assert journey.totalActualCostUsd == pytest.approx(1.52)
        assert journey.totalEstimatedTimeSeconds == pytest.approx(90.0)
        assert journey.totalActualTimeSeconds == pytest.approx(115.0)
        assert journey.costErrorPct == pytest.approx((1.52 - 1.01) / 1.01 * 100.0)

    def test_any_unconfirmed_hop_makes_total_actual_cost_unknown_not_partial(self):
        hops = [
            comparator.compare_hop(_planned(cost=1.0), _executed(cost=1.0, status="ok")),
            comparator.compare_hop(_planned(cost=1.0), _executed(cost=None, status="unconfirmed")),
        ]
        journey = JourneyComparison(fromDex="A", toDex="B", stable="USDT", hops=hops)

        assert journey.totalActualCostUsd is None
        assert journey.costErrorPct is None

    def test_empty_journey_has_no_pct_errors(self):
        journey = JourneyComparison(fromDex="A", toDex="B", stable="USDT", hops=[])

        assert journey.costErrorPct is None
        assert journey.timeErrorPct is None


class TestReport:
    def test_build_report_dedupes_and_sorts_unsupported_dexes(self):
        report = comparator.build_report(live=False, journeys=[], unsupported_dexes=["Aden", "Gate (Perp DEX)", "Aden"])

        assert report.unsupportedDexes == ["Aden", "Gate (Perp DEX)"]
        assert report.live is False

    def test_report_round_trips_through_dict(self):
        hc = comparator.compare_hop(_planned(), _executed())
        journey = JourneyComparison(fromDex="Aster", toDex="MEXC", stable="USDT", hops=[hc])
        report = comparator.build_report(live=True, journeys=[journey], unsupported_dexes=["Aden"])

        from compass_test.models import TestRunReport

        rebuilt = TestRunReport.from_dict(report.to_dict())

        assert rebuilt.runId == report.runId
        assert rebuilt.journeys[0].fromDex == "Aster"
        assert rebuilt.journeys[0].hops[0].planned.dex == "MEXC"
        assert rebuilt.unsupportedDexes == ["Aden"]
