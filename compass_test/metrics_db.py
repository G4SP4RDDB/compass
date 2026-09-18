"""Best-effort TimescaleDB projection of TestRunReport (see models.py),
additive to the JSON files under compass_test/reports/ which remain the
source of truth (see reporter.py). Every public write here is expected to
be wrapped in try/except at the call site — a DB outage must never block a
report from being saved to disk.

Stores one row per HopComparison in rebalancing_operations — "hop" is
compass_test's internal vocabulary for one leg of a rebalance; this module
calls the same thing an "operation" throughout its SQL/API surface,
matching the app's own existing term for it (operations.txt, the "Chosen
Operations" tab). The underlying Python types (HopType, PlannedHop,
ExecutedHop, HopComparison) keep their names — only this projection's
columns/fields are renamed.

Connects fresh per call (no pool/background worker): write volume is a
handful of operations per CLI/frontend run, so connect-on-write is plenty.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import config
from .models import HopComparison, HopType, TestRunReport

SCHEMA_PATH = Path(__file__).resolve().parent / "sql" / "schema.sql"

_INSERT_SQL = """
INSERT INTO rebalancing_operations (
    run_id, journey_index, operation_index, started_at, finished_at,
    run_created_at, live, from_dex, to_dex, journey_stable,
    operation_type, dex, chain, stable, to_stable, to_chain,
    estimated_cost_usd, estimated_time_seconds, configured_time_seconds,
    time_source, solved_flow_usd, min_withdraw_usd, min_deposit_usd,
    operation_live, amount_requested_usd, actual_cost_usd, amount_received_usd,
    tx_hash, tx_confirmed_at, external_id, status, notes,
    gas_cost_usd, fee_cost_usd, slippage_cost_usd,
    actual_time_seconds, cost_error_usd, cost_error_pct,
    time_error_seconds, time_error_pct, cost_bps, slippage_usd
) VALUES (
    %(run_id)s, %(journey_index)s, %(operation_index)s, %(started_at)s, %(finished_at)s,
    %(run_created_at)s, %(live)s, %(from_dex)s, %(to_dex)s, %(journey_stable)s,
    %(operation_type)s, %(dex)s, %(chain)s, %(stable)s, %(to_stable)s, %(to_chain)s,
    %(estimated_cost_usd)s, %(estimated_time_seconds)s, %(configured_time_seconds)s,
    %(time_source)s, %(solved_flow_usd)s, %(min_withdraw_usd)s, %(min_deposit_usd)s,
    %(operation_live)s, %(amount_requested_usd)s, %(actual_cost_usd)s, %(amount_received_usd)s,
    %(tx_hash)s, %(tx_confirmed_at)s, %(external_id)s, %(status)s, %(notes)s,
    %(gas_cost_usd)s, %(fee_cost_usd)s, %(slippage_cost_usd)s,
    %(actual_time_seconds)s, %(cost_error_usd)s, %(cost_error_pct)s,
    %(time_error_seconds)s, %(time_error_pct)s, %(cost_bps)s, %(slippage_usd)s
)
ON CONFLICT (run_id, journey_index, operation_index, started_at) DO UPDATE SET
    finished_at = EXCLUDED.finished_at,
    actual_cost_usd = EXCLUDED.actual_cost_usd,
    amount_received_usd = EXCLUDED.amount_received_usd,
    tx_hash = EXCLUDED.tx_hash,
    tx_confirmed_at = EXCLUDED.tx_confirmed_at,
    external_id = EXCLUDED.external_id,
    status = EXCLUDED.status,
    notes = EXCLUDED.notes,
    gas_cost_usd = EXCLUDED.gas_cost_usd,
    fee_cost_usd = EXCLUDED.fee_cost_usd,
    slippage_cost_usd = EXCLUDED.slippage_cost_usd,
    actual_time_seconds = EXCLUDED.actual_time_seconds,
    cost_error_usd = EXCLUDED.cost_error_usd,
    cost_error_pct = EXCLUDED.cost_error_pct,
    time_error_seconds = EXCLUDED.time_error_seconds,
    time_error_pct = EXCLUDED.time_error_pct,
    cost_bps = EXCLUDED.cost_bps,
    slippage_usd = EXCLUDED.slippage_usd
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(config.TIMESCALE_DB_URL, row_factory=dict_row)


def ensure_schema(conn: psycopg.Connection | None = None) -> None:
    """Runs the idempotent DDL in sql/schema.sql. Safe to call on every
    process start (reporter.save_report, server.py startup) and from the
    backfill script — not only via docker-entrypoint-initdb.d, since that
    only runs against a genuinely fresh volume."""
    owns_conn = conn is None
    conn = conn or connect()
    try:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def _to_dt(unix_seconds: float | None) -> datetime | None:
    if unix_seconds is None:
        return None
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


def _cost_breakdown(hop: HopComparison) -> tuple[float | None, float | None, float | None]:
    """(gas, fee, slippage) for this operation. Uses ExecutedHop's own
    breakdown fields when present (every hop run through the current
    executor.py — see its per-hop-type ExecutedHop() calls). Falls back to
    reconstructing them from operation_type for a report written before
    those fields existed: exact for Withdraw (100% fee, by construction —
    see executor._run_withdraw) and Deposit (100% gas — _run_deposit);
    an approximation for Swap (whole cost attributed to slippage, since
    approve-tx gas is usually the small minority of it when present at
    all) and Bridge (whole cost attributed to fee, since the withdraw
    leg's fee is usually the dominant component next to the deposit leg's
    gas) — both noted as approximate because the legacy data has no
    recorded split for those two types."""
    executed = hop.executed
    if executed.gasCostUsd is not None or executed.feeCostUsd is not None or executed.slippageCostUsd is not None:
        return executed.gasCostUsd, executed.feeCostUsd, executed.slippageCostUsd
    cost = executed.actualCostUsd
    if cost is None:
        return None, None, None
    hop_type = hop.planned.hopType
    if hop_type == HopType.WITHDRAW:
        return 0.0, cost, 0.0
    if hop_type == HopType.DEPOSIT:
        return cost, 0.0, 0.0
    if hop_type == HopType.SWAP:
        return 0.0, 0.0, cost  # approximate: legacy data has no gas/slippage split
    return 0.0, cost, 0.0  # Bridge, approximate: legacy data has no gas/fee split


def _operation_row(report: TestRunReport, journey_index: int, operation_index: int, hop: HopComparison) -> dict[str, Any]:
    planned, executed = hop.planned, hop.executed
    journey = report.journeys[journey_index]
    amount_received = executed.amountReceivedUsd
    slippage_usd = (
        executed.amountRequestedUsd - amount_received if amount_received is not None else None
    )
    cost_bps = (
        executed.actualCostUsd / executed.amountRequestedUsd * 10000.0
        if executed.actualCostUsd is not None and executed.amountRequestedUsd > 1e-9
        else None
    )
    gas_cost_usd, fee_cost_usd, slippage_cost_usd = _cost_breakdown(hop)
    return {
        "run_id": report.runId,
        "journey_index": journey_index,
        "operation_index": operation_index,
        "started_at": _to_dt(executed.startedAt),
        "finished_at": _to_dt(executed.finishedAt),
        "run_created_at": _to_dt(report.createdAt),
        "live": report.live,
        "from_dex": journey.fromDex,
        "to_dex": journey.toDex,
        "journey_stable": journey.stable,
        "operation_type": planned.hopType.value,
        "dex": planned.dex,
        "chain": planned.chain,
        "stable": planned.stable,
        "to_stable": planned.toStable,
        "to_chain": planned.toChain,
        "estimated_cost_usd": planned.estimatedCostUsd,
        "estimated_time_seconds": planned.estimatedTimeSeconds,
        "configured_time_seconds": planned.configuredTimeSeconds,
        "time_source": planned.timeSource,
        "solved_flow_usd": planned.solvedFlowUsd,
        "min_withdraw_usd": planned.minWithdrawUsd,
        "min_deposit_usd": planned.minDepositUsd,
        "operation_live": executed.live,
        "amount_requested_usd": executed.amountRequestedUsd,
        "actual_cost_usd": executed.actualCostUsd,
        "amount_received_usd": amount_received,
        "tx_hash": executed.txHash,
        "tx_confirmed_at": _to_dt(executed.txConfirmedAt),
        "external_id": executed.externalId,
        "status": executed.status,
        "notes": executed.notes,
        "gas_cost_usd": gas_cost_usd,
        "fee_cost_usd": fee_cost_usd,
        "slippage_cost_usd": slippage_cost_usd,
        "actual_time_seconds": executed.actualTimeSeconds,
        "cost_error_usd": hop.costErrorUsd,
        "cost_error_pct": hop.costErrorPct,
        "time_error_seconds": hop.timeErrorSeconds,
        "time_error_pct": hop.timeErrorPct,
        "cost_bps": cost_bps,
        "slippage_usd": slippage_usd,
    }


def upsert_report(report: TestRunReport, conn: psycopg.Connection | None = None) -> int:
    """Flattens report.journeys[*].hops[*] into rebalancing_operations rows
    and upserts them (idempotent on run_id/journey_index/operation_index/
    started_at — safe for reporter.py re-writing latest.json or re-running
    the backfill script). Returns the number of operation rows written."""
    rows = [
        _operation_row(report, ji, oi, hop)
        for ji, journey in enumerate(report.journeys)
        for oi, hop in enumerate(journey.hops)
    ]
    if not rows:
        return 0
    owns_conn = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, rows)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()
    return len(rows)


def _fetch(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _fetch_one(sql: str, params: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() or {}


def summary(days: int = 30, live_only: bool = False) -> dict[str, Any]:
    """Success rate is always computed over LIVE operations only (`live =
    true`), regardless of the `live_only` display filter — a dry-run hop's
    status is "dry_run" by construction (see executor.py), not a pass/fail
    signal, so mixing it into a success rate misrepresents it as a failure."""
    return _fetch_one(
        """
        SELECT
            count(*) AS operation_count,
            count(DISTINCT run_id) AS run_count,
            sum(actual_cost_usd) AS total_cost_usd,
            avg(actual_time_seconds) AS avg_time_seconds,
            avg(cost_bps) AS avg_cost_bps,
            sum(gas_cost_usd) AS total_gas_cost_usd,
            sum(fee_cost_usd) AS total_fee_cost_usd,
            sum(slippage_cost_usd) AS total_slippage_cost_usd,
            avg((status = 'ok')::int) FILTER (WHERE live)::float AS success_rate,
            count(*) FILTER (WHERE live) AS live_operation_count
        FROM rebalancing_operations
        WHERE started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR live = true)
        """,
        {"days": days, "live_only": live_only},
    )


def daily_counts(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT time_bucket('1 day', started_at) AS day, count(*) AS operation_count
        FROM rebalancing_operations
        WHERE started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR live = true)
        GROUP BY day
        ORDER BY day
        """,
        {"days": days, "live_only": live_only},
    )


def cost_over_time(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT time_bucket('1 day', started_at) AS day,
               sum(actual_cost_usd) AS total_cost_usd,
               avg(cost_bps) AS avg_cost_bps
        FROM rebalancing_operations
        WHERE started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR live = true)
        GROUP BY day
        ORDER BY day
        """,
        {"days": days, "live_only": live_only},
    )


def cost_breakdown_over_time(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    """Daily gas/fee/slippage totals — backs the dashboard's stacked cost
    breakdown chart (see _cost_breakdown for what each bucket means)."""
    return _fetch(
        """
        SELECT time_bucket('1 day', started_at) AS day,
               sum(gas_cost_usd) AS total_gas_cost_usd,
               sum(fee_cost_usd) AS total_fee_cost_usd,
               sum(slippage_cost_usd) AS total_slippage_cost_usd
        FROM rebalancing_operations
        WHERE started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR live = true)
        GROUP BY day
        ORDER BY day
        """,
        {"days": days, "live_only": live_only},
    )


def delay_over_time(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT time_bucket('1 day', started_at) AS day,
               avg(actual_time_seconds) AS avg_time_seconds,
               avg(time_error_pct) AS avg_time_error_pct
        FROM rebalancing_operations
        WHERE started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR live = true)
        GROUP BY day
        ORDER BY day
        """,
        {"days": days, "live_only": live_only},
    )


def by_dex(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT dex,
               count(*) AS operation_count,
               sum(actual_cost_usd) AS total_cost_usd,
               avg(actual_time_seconds) AS avg_time_seconds
        FROM rebalancing_operations
        WHERE started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR live = true)
        GROUP BY dex
        ORDER BY operation_count DESC
        """,
        {"days": days, "live_only": live_only},
    )
