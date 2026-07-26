-- corlinman-server agent journal — Postgres schema v6.
--
-- Apply after v5 once per Postgres cluster that backs a multi-gateway
-- HA deployment:
--
--     psql "$CORLINMAN_JOURNAL_POSTGRES_DSN" \
--         -f packages/corlinman-server/migrations/journal_postgres_v6.sql
--
-- v6 persists the configured channel runtime that originated each turn.
-- Blank means legacy/unknown and is deliberately distinct from the QQ
-- instance named ``default``.

ALTER TABLE journal_turns
    ADD COLUMN IF NOT EXISTS runtime_instance_id TEXT NOT NULL DEFAULT '';

DROP INDEX IF EXISTS journal_turns_in_progress_uniq;
CREATE UNIQUE INDEX journal_turns_in_progress_uniq
    ON journal_turns (
        session_key,
        user_text,
        COALESCE(user_id, ''),
        channel,
        runtime_instance_id
    )
    WHERE status = 'in_progress';
