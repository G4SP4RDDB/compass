"""Best-effort projection of TestRunReport (see models.py) into Sentinel's
shared TimescaleDB (sentinelBackend/sentinel, schema `sentinel`), additive to
the JSON files under compass_test/reports/ which remain the source of truth
(see reporter.py). Every public write here is expected to be wrapped in
try/except at the call site — a DB outage must never block a report from
being saved to disk.

Three tables, normalized out of what used to be one flat
`rebalancing_operations` hypertable Compass ran itself:
- `sentinel.rebalance` — one row per journey (a DEX->DEX move, one or more
  hops).
- `sentinel.rebalance_leg` — one row per hop ("hop" is compass_test's own
  vocabulary for one leg of a rebalance; a Bridge hop's two internal legs
  stay merged into one row, mirroring `ExecutedHop`).
- `sentinel.rebalance_leg_stage` — append-only: every intermediate stage a
  leg's live execution passed through (executor.py's `on_stage` callback),
  newly durable — it used to be streamed to the browser and discarded.

The tables are created by Sentinel's own Alembic migration (0006), not by
Compass — there is no schema-creation call in this module.

Connects fresh per call (no pool/background worker): write volume is a
handful of operations per CLI/frontend run, so connect-on-write is plenty.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import config
from .models import HopComparison, HopType, JourneyComparison, TestRunReport

_REBALANCE_INSERT_SQL = """
INSERT INTO sentinel.rebalance (
    rebalance_id, run_id, journey_index, run_created_at, live,
    from_dex, to_dex, stable, status, planned_amount_usd, started_at, finished_at, notes
) VALUES (
    %(rebalance_id)s, %(run_id)s, %(journey_index)s, %(run_created_at)s, %(live)s,
    %(from_dex)s, %(to_dex)s, %(stable)s, %(status)s, %(planned_amount_usd)s,
    %(started_at)s, %(finished_at)s, %(notes)s
)
ON CONFLICT (rebalance_id) DO UPDATE SET
    status = EXCLUDED.status,
    finished_at = EXCLUDED.finished_at,
    notes = EXCLUDED.notes,
    updated_at = now()
"""

_LEG_INSERT_SQL = """
INSERT INTO sentinel.rebalance_leg (
    rebalance_id, leg_index, hop_type, dex, chain, stable, to_stable, to_chain,
    estimated_cost_usd, estimated_time_seconds, configured_time_seconds,
    time_source, solved_flow_usd, min_withdraw_usd, min_deposit_usd,
    leg_live, amount_requested_usd, actual_cost_usd, amount_received_usd,
    tx_hash, tx_confirmed_at, external_id, status, notes,
    gas_cost_usd, fee_cost_usd, slippage_cost_usd,
    started_at, finished_at, actual_time_seconds, cost_error_usd, cost_error_pct,
    time_error_seconds, time_error_pct, cost_bps, slippage_usd
) VALUES (
    %(rebalance_id)s, %(leg_index)s, %(hop_type)s, %(dex)s, %(chain)s, %(stable)s,
    %(to_stable)s, %(to_chain)s,
    %(estimated_cost_usd)s, %(estimated_time_seconds)s, %(configured_time_seconds)s,
    %(time_source)s, %(solved_flow_usd)s, %(min_withdraw_usd)s, %(min_deposit_usd)s,
    %(leg_live)s, %(amount_requested_usd)s, %(actual_cost_usd)s, %(amount_received_usd)s,
    %(tx_hash)s, %(tx_confirmed_at)s, %(external_id)s, %(status)s, %(notes)s,
    %(gas_cost_usd)s, %(fee_cost_usd)s, %(slippage_cost_usd)s,
    %(started_at)s, %(finished_at)s, %(actual_time_seconds)s, %(cost_error_usd)s, %(cost_error_pct)s,
    %(time_error_seconds)s, %(time_error_pct)s, %(cost_bps)s, %(slippage_usd)s
)
ON CONFLICT (rebalance_id, leg_index) DO UPDATE SET
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
    slippage_usd = EXCLUDED.slippage_usd,
    updated_at = now()
"""

_STAGE_INSERT_SQL = """
INSERT INTO sentinel.rebalance_leg_stage (rebalance_id, leg_index, seq, ts, stage_code, domain, message)
VALUES (
    %(rebalance_id)s, %(leg_index)s,
    COALESCE(
        (SELECT max(seq) + 1 FROM sentinel.rebalance_leg_stage
         WHERE rebalance_id = %(rebalance_id)s AND leg_index = %(leg_index)s),
        0
    ),
    %(ts)s, %(stage_code)s, %(domain)s, %(message)s
)
ON CONFLICT (rebalance_id, leg_index, seq) DO NOTHING
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(config.TIMESCALE_DB_URL, row_factory=dict_row)


def _to_dt(unix_seconds: float | None) -> datetime | None:
    if unix_seconds is None:
        return None
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


def rebalance_id(run_id: str, journey_index: int) -> str:
    return f"{run_id}:{journey_index}"


def _rebalance_status(hops: list[HopComparison]) -> str:
    """planned (no hop run yet) | executing | settled | failed | partial.
    Every hop reaching upsert_report already has a terminal ExecutedHop, so
    in practice this only ever returns settled/failed/executing — planned
    is the table's own default for a row nothing has written yet."""
    if not hops:
        return "planned"
    statuses = [h.executed.status for h in hops]
    if any(s == "error" for s in statuses):
        return "failed"
    if all(s in ("ok", "dry_run") for s in statuses):
        return "settled"
    return "executing"  # unconfirmed, or a mix still resolving


def _cost_breakdown(hop: HopComparison) -> tuple[float | None, float | None, float | None]:
    """(gas, fee, slippage) for this operation. Uses ExecutedHop's own
    breakdown fields when present (every hop run through the current
    executor.py — see its per-hop-type ExecutedHop() calls: these are real
    measured figures, not an estimate, see docs/timescale-schema.md
    "Rebalancing" in the sentinel repo). Falls back to reconstructing them
    from operation_type ONLY for a report written before those fields
    existed — exact for Withdraw (100% fee, by construction — see
    executor._run_withdraw) and Deposit (100% gas — _run_deposit); an
    approximation for Swap (whole cost attributed to slippage, since
    approve-tx gas is usually the small minority of it when present at
    all) and Bridge (whole cost attributed to fee, since the withdraw
    leg's fee is usually the dominant component next to the deposit leg's
    gas) — both noted as approximate because that legacy data has no
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


def _rebalance_row(report: TestRunReport, journey_index: int, journey: JourneyComparison) -> dict[str, Any]:
    hops = journey.hops
    started = [h.executed.startedAt for h in hops]
    finished = [h.executed.finishedAt for h in hops]
    return {
        "rebalance_id": rebalance_id(report.runId, journey_index),
        "run_id": report.runId,
        "journey_index": journey_index,
        "run_created_at": _to_dt(report.createdAt),
        "live": report.live,
        "from_dex": journey.fromDex,
        "to_dex": journey.toDex,
        "stable": journey.stable,
        "status": _rebalance_status(hops),
        # The amount that entered the journey — each later hop's requested
        # amount is the previous hop's actually-received amount, not a
        # second independent "planned" figure (see executor.run_journey_hops).
        "planned_amount_usd": hops[0].executed.amountRequestedUsd if hops else None,
        "started_at": _to_dt(min(started)) if started else None,
        "finished_at": _to_dt(max(finished)) if finished else None,
        "notes": "",
    }


def _leg_row(rid: str, leg_index: int, hop: HopComparison) -> dict[str, Any]:
    planned, executed = hop.planned, hop.executed
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
        "rebalance_id": rid,
        "leg_index": leg_index,
        "hop_type": planned.hopType.value,
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
        "leg_live": executed.live,
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
        "started_at": _to_dt(executed.startedAt),
        "finished_at": _to_dt(executed.finishedAt),
        "actual_time_seconds": executed.actualTimeSeconds,
        "cost_error_usd": hop.costErrorUsd,
        "cost_error_pct": hop.costErrorPct,
        "time_error_seconds": hop.timeErrorSeconds,
        "time_error_pct": hop.timeErrorPct,
        "cost_bps": cost_bps,
        "slippage_usd": slippage_usd,
    }


def upsert_report(report: TestRunReport, conn: psycopg.Connection | None = None) -> int:
    """Upserts one sentinel.rebalance row per journey and one
    sentinel.rebalance_leg row per hop, in one transaction (idempotent on
    rebalance_id / (rebalance_id, leg_index) — safe for reporter.py
    re-writing latest.json or re-running the backfill script). Returns the
    number of leg rows written."""
    rebalance_rows = []
    leg_rows = []
    for ji, journey in enumerate(report.journeys):
        rid = rebalance_id(report.runId, ji)
        rebalance_rows.append(_rebalance_row(report, ji, journey))
        for oi, hop in enumerate(journey.hops):
            leg_rows.append(_leg_row(rid, oi, hop))
    if not rebalance_rows:
        return 0
    owns_conn = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(_REBALANCE_INSERT_SQL, rebalance_rows)
            if leg_rows:
                cur.executemany(_LEG_INSERT_SQL, leg_rows)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()
    return len(leg_rows)


def append_stage(
    rid: str,
    leg_index: int,
    stage_code: str,
    message: str,
    domain: str,
    ts: datetime | None = None,
    conn: psycopg.Connection | None = None,
) -> None:
    """Appends one row to sentinel.rebalance_leg_stage, at the next `seq` for
    this (rebalance_id, leg_index) — the intermediary-phase log a live hop's
    on_stage callback used to only stream to the browser and discard. Called
    once per hop execution at a time (see hop_runner.py), so the max(seq)+1
    read is never raced."""
    owns_conn = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            _STAGE_INSERT_SQL,
            {
                "rebalance_id": rid,
                "leg_index": leg_index,
                "ts": ts or datetime.now(timezone.utc),
                "stage_code": stage_code,
                "domain": domain,
                "message": message,
            },
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


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


def list_rebalances(days: int = 30) -> list[dict[str, Any]]:
    """Backs the "Rebalance History" tab's list view."""
    return _fetch(
        """
        SELECT rebalance_id, run_id, journey_index, from_dex, to_dex, stable, status,
               live, planned_amount_usd, started_at, finished_at
        FROM sentinel.rebalance
        WHERE started_at IS NULL OR started_at >= now() - make_interval(days => %(days)s)
        ORDER BY started_at DESC NULLS LAST
        """,
        {"days": days},
    )


def leg_stages(rid: str, leg_index: int) -> list[dict[str, Any]]:
    """A finished leg's stage timeline, in order — shaped like the
    {"type": "stage", "stage": ..., "message": ..., "domain": ...} events
    POST /api/test-hop streams live, so the frontend can replay it through
    the same renderer."""
    return _fetch(
        """
        SELECT seq, ts, stage_code AS stage, domain, message
        FROM sentinel.rebalance_leg_stage
        WHERE rebalance_id = %(rebalance_id)s AND leg_index = %(leg_index)s
        ORDER BY seq
        """,
        {"rebalance_id": rid, "leg_index": leg_index},
    )


def summary(days: int = 30, live_only: bool = False) -> dict[str, Any]:
    """Success rate is always computed over LIVE operations only (`live =
    true`), regardless of the `live_only` display filter — a dry-run hop's
    status is "dry_run" by construction (see executor.py), not a pass/fail
    signal, so mixing it into a success rate misrepresents it as a failure."""
    return _fetch_one(
        """
        SELECT
            count(*) AS operation_count,
            count(DISTINCT r.run_id) AS run_count,
            sum(l.actual_cost_usd) AS total_cost_usd,
            avg(l.actual_time_seconds) AS avg_time_seconds,
            avg(l.cost_bps) AS avg_cost_bps,
            sum(l.gas_cost_usd) AS total_gas_cost_usd,
            sum(l.fee_cost_usd) AS total_fee_cost_usd,
            sum(l.slippage_cost_usd) AS total_slippage_cost_usd,
            avg((l.status = 'ok')::int) FILTER (WHERE r.live)::float AS success_rate,
            count(*) FILTER (WHERE r.live) AS live_operation_count
        FROM sentinel.rebalance_leg l
        JOIN sentinel.rebalance r ON r.rebalance_id = l.rebalance_id
        WHERE l.started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR r.live = true)
        """,
        {"days": days, "live_only": live_only},
    )


def daily_counts(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT time_bucket('1 day', l.started_at) AS day, count(*) AS operation_count
        FROM sentinel.rebalance_leg l
        JOIN sentinel.rebalance r ON r.rebalance_id = l.rebalance_id
        WHERE l.started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR r.live = true)
        GROUP BY day
        ORDER BY day
        """,
        {"days": days, "live_only": live_only},
    )


def cost_over_time(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT time_bucket('1 day', l.started_at) AS day,
               sum(l.actual_cost_usd) AS total_cost_usd,
               avg(l.cost_bps) AS avg_cost_bps
        FROM sentinel.rebalance_leg l
        JOIN sentinel.rebalance r ON r.rebalance_id = l.rebalance_id
        WHERE l.started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR r.live = true)
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
        SELECT time_bucket('1 day', l.started_at) AS day,
               sum(l.gas_cost_usd) AS total_gas_cost_usd,
               sum(l.fee_cost_usd) AS total_fee_cost_usd,
               sum(l.slippage_cost_usd) AS total_slippage_cost_usd
        FROM sentinel.rebalance_leg l
        JOIN sentinel.rebalance r ON r.rebalance_id = l.rebalance_id
        WHERE l.started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR r.live = true)
        GROUP BY day
        ORDER BY day
        """,
        {"days": days, "live_only": live_only},
    )


def delay_over_time(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT time_bucket('1 day', l.started_at) AS day,
               avg(l.actual_time_seconds) AS avg_time_seconds,
               avg(l.time_error_pct) AS avg_time_error_pct
        FROM sentinel.rebalance_leg l
        JOIN sentinel.rebalance r ON r.rebalance_id = l.rebalance_id
        WHERE l.started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR r.live = true)
        GROUP BY day
        ORDER BY day
        """,
        {"days": days, "live_only": live_only},
    )


def by_dex(days: int = 30, live_only: bool = False) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT l.dex AS dex,
               count(*) AS operation_count,
               sum(l.actual_cost_usd) AS total_cost_usd,
               avg(l.actual_time_seconds) AS avg_time_seconds
        FROM sentinel.rebalance_leg l
        JOIN sentinel.rebalance r ON r.rebalance_id = l.rebalance_id
        WHERE l.started_at >= now() - make_interval(days => %(days)s)
          AND (%(live_only)s = false OR r.live = true)
        GROUP BY l.dex
        ORDER BY operation_count DESC
        """,
        {"days": days, "live_only": live_only},
    )


def phase_durations(days: int = 30) -> list[dict[str, Any]]:
    """Average/median seconds spent in each stage_code before the next stage
    fired, across every leg — what rebalancing_operations's single opaque
    actual_time_seconds could never show. A leg's LAST stage has no "next"
    and is excluded (its tail duration is already inside actual_time_seconds
    on rebalance_leg)."""
    return _fetch(
        """
        WITH gaps AS (
            SELECT stage_code,
                   lead(ts) OVER (PARTITION BY rebalance_id, leg_index ORDER BY seq) - ts AS duration
            FROM sentinel.rebalance_leg_stage
            WHERE ts >= now() - make_interval(days => %(days)s)
        )
        SELECT stage_code,
               avg(extract(epoch FROM duration)) AS avg_seconds,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM duration)) AS median_seconds,
               count(*) AS sample_count
        FROM gaps
        WHERE duration IS NOT NULL
        GROUP BY stage_code
        ORDER BY avg_seconds DESC NULLS LAST
        """,
        {"days": days},
    )
