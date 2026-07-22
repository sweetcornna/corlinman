-- corlinman-server agent journal — Postgres schema v1.
--
-- Apply once per Postgres cluster that backs a multi-gateway HA deployment:
--
--     psql "$CORLINMAN_JOURNAL_POSTGRES_DSN" \
--         -f packages/corlinman-server/migrations/journal_postgres_v1.sql
--
-- The DDL is idempotent (every CREATE uses IF NOT EXISTS), so it is safe
-- to re-run during deploys. Future schema changes ship as
-- ``journal_postgres_v2.sql`` etc. — there is no migration tool yet;
-- operators run them in order.
--
-- Table names are prefixed ``journal_`` to coexist with other apps that
-- might share the same Postgres database.

CREATE TABLE IF NOT EXISTS journal_turns (
    turn_id       BIGSERIAL PRIMARY KEY,
    session_key   TEXT   NOT NULL,
    status        TEXT   NOT NULL,
    started_at_ms BIGINT NOT NULL,
    ended_at_ms           BIGINT,
    user_text             TEXT,
    error                 TEXT,
    elapsed_ms            BIGINT,
    estimated_cost_usd    DOUBLE PRECISION,
    cost_status           TEXT,
    tool_call_count       INTEGER NOT NULL DEFAULT 0,
    reasoning_token_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS journal_turns_session_status_idx
    ON journal_turns(session_key, status, started_at_ms DESC);

CREATE INDEX IF NOT EXISTS journal_turns_status_started_idx
    ON journal_turns(status, started_at_ms);

CREATE TABLE IF NOT EXISTS journal_turn_messages (
    turn_id         BIGINT  NOT NULL REFERENCES journal_turns(turn_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT,
    tool_call_id    TEXT,
    tool_calls_json TEXT,
    PRIMARY KEY (turn_id, seq)
);
