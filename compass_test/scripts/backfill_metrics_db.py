"""One-off: backfills every compass_test/reports/*.json into Sentinel's
TimescaleDB (sentinel.rebalance / rebalance_leg). Safe to re-run —
metrics_db.upsert_report is idempotent on (rebalance_id) / (rebalance_id,
leg_index). Assumes Sentinel's Alembic migration 0006 has already created
the tables — this script does not create schema itself.

Usage: python -m compass_test.scripts.backfill_metrics_db
"""

from __future__ import annotations

from .. import metrics_db, reporter


def main() -> None:
    reports = reporter.list_reports()
    total_legs = 0
    for report in reports:
        total_legs += metrics_db.upsert_report(report)
    print(f"backfilled {len(reports)} reports, {total_legs} leg rows")


if __name__ == "__main__":
    main()
