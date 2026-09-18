-- rebalancing_operations: one row per HopComparison (PlannedHop + ExecutedHop
-- — "hop" is compass_test's internal vocabulary for one leg of a rebalance;
-- this table calls the same thing an "operation", matching the app's own
-- existing term for it (operations.txt, the "Chosen Operations" tab).
-- See compass_test/models.py for the Python-side shape this mirrors.
--
-- Idempotent by design: safe to run against a fresh container (mounted at
-- /docker-entrypoint-initdb.d/ in docker-compose.yml) or an already-running
-- one (metrics_db.ensure_schema()).
--
-- No separate runs/journeys tables: Timescale performs best un-joined for
-- the time_bucket()/GROUP BY queries the dashboard needs, and run/journey
-- context (run_id, live, from_dex/to_dex, journey_stable) is cheap to
-- denormalize onto each operation row at today's volume (low hundreds of rows).
--
-- Idempotency key (run_id, journey_index, operation_index, started_at):
-- stable because TestRunReport.journeys/JourneyComparison.hops are ordered
-- lists written once by reporter.save_report() and never mutated in place.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS rebalancing_operations (
    -- Identity / idempotency -------------------------------------------
    run_id          TEXT        NOT NULL,   -- TestRunReport.runId, e.g. "20260918T105522Z"
    journey_index   INTEGER     NOT NULL,   -- position in TestRunReport.journeys
    operation_index INTEGER     NOT NULL,   -- position in JourneyComparison.hops
    -- Time partitioning column -------------------------------------------
    started_at      TIMESTAMPTZ NOT NULL,   -- ExecutedHop.startedAt
    finished_at     TIMESTAMPTZ NOT NULL,   -- ExecutedHop.finishedAt
    -- Run-level context, denormalized ------------------------------------
    run_created_at  TIMESTAMPTZ NOT NULL,   -- TestRunReport.createdAt
    live            BOOLEAN     NOT NULL,   -- TestRunReport.live
    -- Journey-level context, denormalized ---------------------------------
    from_dex        TEXT        NOT NULL,
    to_dex          TEXT        NOT NULL,
    journey_stable  TEXT        NOT NULL,
    -- PlannedHop -----------------------------------------------------------
    operation_type           TEXT             NOT NULL,  -- "Withdraw"|"Deposit"|"Swap"|"Bridge"
    dex                      TEXT             NOT NULL,
    chain                    TEXT             NOT NULL,
    stable                   TEXT             NOT NULL,
    to_stable                TEXT             NOT NULL DEFAULT '',
    to_chain                 TEXT             NOT NULL DEFAULT '',
    estimated_cost_usd       DOUBLE PRECISION NOT NULL,
    estimated_time_seconds   DOUBLE PRECISION NOT NULL,
    configured_time_seconds  DOUBLE PRECISION,
    time_source              TEXT             NOT NULL DEFAULT 'configured',
    solved_flow_usd          DOUBLE PRECISION NOT NULL DEFAULT 0,
    min_withdraw_usd         DOUBLE PRECISION NOT NULL DEFAULT 0,
    min_deposit_usd          DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- ExecutedHop ------------------------------------------------------
    operation_live            BOOLEAN         NOT NULL,  -- ExecutedHop.live (usually == `live` above)
    amount_requested_usd     DOUBLE PRECISION NOT NULL,
    actual_cost_usd          DOUBLE PRECISION,            -- nullable: failed/timed-out live measurement
    amount_received_usd      DOUBLE PRECISION,
    tx_hash                  TEXT,
    tx_confirmed_at          TIMESTAMPTZ,
    external_id               TEXT,
    status                    TEXT            NOT NULL DEFAULT 'ok', -- "ok"|"unconfirmed"|"error"|"dry_run"
    notes                     TEXT            NOT NULL DEFAULT '',
    -- actual_cost_usd's breakdown (see models.ExecutedHop.gasCostUsd/
    -- feeCostUsd/slippageCostUsd) — on-chain gas we paid directly, a fee
    -- deducted by a DEX/exchange off-chain, and price impact + venue
    -- network fee (all-in) respectively. NULL on a hop whose breakdown
    -- isn't known (actual_cost_usd also NULL then), or on a legacy row
    -- backfilled from a report written before these fields existed — see
    -- metrics_db.py's per-operation-type reconstruction for that case.
    gas_cost_usd              DOUBLE PRECISION,
    fee_cost_usd               DOUBLE PRECISION,
    slippage_cost_usd           DOUBLE PRECISION,
    -- Derived, persisted so SQL never recomputes Python business logic ----
    actual_time_seconds       DOUBLE PRECISION NOT NULL,  -- finished_at - started_at
    cost_error_usd             DOUBLE PRECISION,
    cost_error_pct              DOUBLE PRECISION,
    time_error_seconds           DOUBLE PRECISION NOT NULL,
    time_error_pct                DOUBLE PRECISION,
    -- Extra backtesting metrics, trivially derived at insert time --------
    cost_bps                       DOUBLE PRECISION,        -- actual_cost_usd / amount_requested_usd * 10000
    slippage_usd                    DOUBLE PRECISION,       -- amount_requested_usd - amount_received_usd

    inserted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, journey_index, operation_index, started_at)
);

SELECT create_hypertable(
    'rebalancing_operations', 'started_at',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

-- Common query paths: per-DEX time series, status-distribution, backfill lookups.
CREATE INDEX IF NOT EXISTS idx_rebalancing_operations_dex_time
    ON rebalancing_operations (dex, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_rebalancing_operations_status
    ON rebalancing_operations (status);
CREATE INDEX IF NOT EXISTS idx_rebalancing_operations_run_id
    ON rebalancing_operations (run_id);
