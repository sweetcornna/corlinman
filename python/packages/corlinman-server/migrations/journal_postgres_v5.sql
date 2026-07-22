-- corlinman-server agent journal — Postgres schema v5.
--
-- Apply after v4 once per Postgres cluster that backs a multi-gateway
-- HA deployment:
--
--     psql "$CORLINMAN_JOURNAL_POSTGRES_DSN" \
--         -f packages/corlinman-server/migrations/journal_postgres_v5.sql
--
-- v5 adds the per-turn aggregate and cost columns already used by the
-- SQLite journal's session-turn listing. The embedded startup DDL in
-- ``agent_journal_postgres`` carries the same idempotent ALTER statements.

ALTER TABLE journal_turns ADD COLUMN IF NOT EXISTS elapsed_ms BIGINT;
ALTER TABLE journal_turns
    ADD COLUMN IF NOT EXISTS estimated_cost_usd DOUBLE PRECISION;
ALTER TABLE journal_turns ADD COLUMN IF NOT EXISTS cost_status TEXT;
ALTER TABLE journal_turns
    ADD COLUMN IF NOT EXISTS tool_call_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE journal_turns
    ADD COLUMN IF NOT EXISTS reasoning_token_count INTEGER NOT NULL DEFAULT 0;
