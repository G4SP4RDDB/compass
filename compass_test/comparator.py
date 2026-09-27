"""Pure functions turning (PlannedHop, ExecutedHop) pairs into the comparison
objects the report/frontend read — no network, no keys, fully unit-testable
(see tests/test_comparator.py)."""

from __future__ import annotations

from .models import HopComparison, JourneyComparison, PlannedHop, TestRunReport, new_run_id
from .models import ExecutedHop  # noqa: F401 (re-exported for callers' convenience)
from .plan_loader import PlannedJourney


def compare_hop(planned: PlannedHop, executed: ExecutedHop) -> HopComparison:
    return HopComparison(planned=planned, executed=executed)


def compare_journey(planned_journey: PlannedJourney, hop_comparisons: list[HopComparison]) -> JourneyComparison:
    return JourneyComparison(
        fromDex=planned_journey.fromDex,
        toDex=planned_journey.toDex,
        stable=planned_journey.stable,
        hops=hop_comparisons,
    )


def build_report(
    live: bool,
    journeys: list[JourneyComparison],
    unsupported_dexes: list[str],
    run_id: str | None = None,
) -> TestRunReport:
    """`run_id`, when given, lets a caller that already generated one before
    execution (to tag rebalance_leg_stage rows as they streamed live — see
    hop_runner.run_single_hop / cli.py's `run` command) reuse it here, so the
    report and the stages already written under it share one id."""
    import time

    return TestRunReport(
        runId=run_id or new_run_id(),
        createdAt=time.time(),
        live=live,
        journeys=journeys,
        unsupportedDexes=sorted(set(unsupported_dexes)),
    )
