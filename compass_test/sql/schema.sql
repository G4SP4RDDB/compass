-- rebalancing_hops: one row per HopComparison (PlannedHop + ExecutedHop),
-- see compass_test/models.py for the Python-side shape this mirrors.
--
-- Idempotent by design: safe to run against a fresh container (mounted at
-- /docker-entrypoint-initdb.d/ in docker-compose.yml) or an already-running
-- one (metrics_db.ensure_schema()).
--
-- No separate runs/journeys tables: Timescale performs best un-joined for
-- the time_bucket()/GROUP BY queries the dashboard needs, and run/journey
-- context (run_id, live, from_dex/to_dex, journey_stable) is cheap to
-- denormalize onto each hop row at today's volume (low hundreds of rows).
--
-- Idempotency key (run_id, journey_index, hop_index, started_at): stable
-- because TestRunReport.journeys/JourneyComparison.hops are ordered lists
-- written once by reporter.save_report() and never mutated in place.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS rebalancing_hops (
    -- Identity / idempotency -------------------------------------------
    run_id          TEXT        NOT NULL,   -- TestRunReport.runId, e.g. "20260918T105522Z"
    journey_index   INTEGER     NOT NULL,   -- position in TestRunReport.journeys
    hop_index       INTEGER     NOT NULL,   -- position in JourneyComparison.hops
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
    hop_type                 TEXT             NOT NULL,  -- "Withdraw"|"Deposit"|"Swap"|"Bridge"
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
    hop_live                 BOOLEAN          NOT NULL,  -- ExecutedHop.live (usually == `live` above)
    amount_requested_usd     DOUBLE PRECISION NOT NULL,
    actual_cost_usd          DOUBLE PRECISION,            -- nullable: failed/timed-out live measurement
    amount_received_usd      DOUBLE PRECISION,
    tx_hash                  TEXT,
    tx_confirmed_at          TIMESTAMPTZ,
    external_id               TEXT,
    status                    TEXT            NOT NULL DEFAULT 'ok', -- "ok"|"unconfirmed"|"error"|"dry_run"
    notes                     TEXT            NOT NULL DEFAULT '',
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

    PRIMARY KEY (run_id, journey_index, hop_index, started_at)
);

SELECT create_hypertable(
    'rebalancing_hops', 'started_at',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

-- Common query paths: per-DEX time series, status-distribution, backfill lookups.
CREATE INDEX IF NOT EXISTS idx_rebalancing_hops_dex_time
    ON rebalancing_hops (dex, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_rebalancing_hops_status
    ON rebalancing_hops (status);
CREATE INDEX IF NOT EXISTS idx_rebalancing_hops_run_id
    ON rebalancing_hops (run_id);
