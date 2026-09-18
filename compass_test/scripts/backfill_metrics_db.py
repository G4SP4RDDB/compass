"""One-off: backfills every compass_test/reports/*.json into TimescaleDB.
Safe to re-run — metrics_db.upsert_report is idempotent on
(run_id, journey_index, hop_index, started_at).

Usage: python -m compass_test.scripts.backfill_metrics_db
"""

from __future__ import annotations

from .. import metrics_db, reporter


def main() -> None:
    metrics_db.ensure_schema()
    reports = reporter.list_reports()
    total_operations = 0
    for report in reports:
        total_operations += metrics_db.upsert_report(report)
    print(f"backfilled {len(reports)} reports, {total_operations} operation rows")


if __name__ == "__main__":
    main()
