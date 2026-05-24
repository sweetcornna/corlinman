-- corlinman-server agent journal — Postgres schema v4.
--
-- Apply after v3 once per Postgres cluster that backs a multi-gateway
-- HA deployment:
--
--     psql "$CORLINMAN_JOURNAL_POSTGRES_DSN" \
--         -f packages/corlinman-server/migrations/journal_postgres_v4.sql
--
-- v4 adds:
--
--   ask_user — ``pending_question_json`` column on ``journal_turns``
--   so the chat handler can stash the JSON args of an ``ask_user``
--   tool call that ended a turn (question + canned answer options).
--   Purely informational at this layer; future admin surfaces can
--   read it to badge sessions that are waiting on a user reply.
--
--   The column is nullable (no default needed) — legacy rows
--   journaled before v4 round-trip as NULL.
--
-- Idempotent (``ADD COLUMN IF NOT EXISTS``) — re-running on a cluster
-- that already has v4 applied is a no-op. The same DDL is also embedded
-- in :mod:`corlinman_server.agent_journal_postgres` so a fresh deploy
-- that opens the backend without pre-applying this file Just Works.

ALTER TABLE journal_turns
    ADD COLUMN IF NOT EXISTS pending_question_json TEXT;
