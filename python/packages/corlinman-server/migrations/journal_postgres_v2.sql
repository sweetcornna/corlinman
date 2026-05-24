-- corlinman-server agent journal — Postgres schema v2.
--
-- Apply after v1 once per Postgres cluster that backs a multi-gateway
-- HA deployment:
--
--     psql "$CORLINMAN_JOURNAL_POSTGRES_DSN" \
--         -f packages/corlinman-server/migrations/journal_postgres_v2.sql
--
-- v2 adds:
--
--   S4 — ``user_id`` column on ``journal_turns`` so
--   ``find_resumable_turn`` can scope its match by channel sender.
--   Without this scoping, two distinct users in the same group-chat
--   ``session_key`` who happened to type the same text could each
--   resume the other's in-progress turn (replay attack).
--
--   C5 — partial unique index on
--   ``(session_key, user_text, COALESCE(user_id, ''))`` WHERE
--   ``status = 'in_progress'``. Lets the Postgres backend issue
--   ``INSERT ... ON CONFLICT DO NOTHING RETURNING turn_id`` so two
--   gateways racing ``begin_turn`` for the same tuple safely collapse
--   to one row; the loser falls back to ``find_resumable_turn``.
--
-- Every statement is idempotent (``IF NOT EXISTS`` / ``ADD COLUMN
-- IF NOT EXISTS``) so this file is re-runnable on a cluster that has
-- already been migrated.

ALTER TABLE journal_turns ADD COLUMN IF NOT EXISTS user_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS journal_turns_in_progress_uniq
    ON journal_turns (session_key, user_text, COALESCE(user_id, ''))
    WHERE status = 'in_progress';
