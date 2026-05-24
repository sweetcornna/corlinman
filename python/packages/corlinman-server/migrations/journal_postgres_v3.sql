-- corlinman-server agent journal — Postgres schema v3.
--
-- Apply after v2 once per Postgres cluster that backs a multi-gateway
-- HA deployment:
--
--     psql "$CORLINMAN_JOURNAL_POSTGRES_DSN" \
--         -f packages/corlinman-server/migrations/journal_postgres_v3.sql
--
-- v3 adds:
--
--   Auto-resume — ``channel`` column on ``journal_turns`` so the
--   boot-time :class:`AgentResumeService` can dispatch re-delivery to
--   the right surface (QQ vs Telegram vs Discord vs HTTP). Without
--   this tag, the scanner can find the in_progress row but cannot tell
--   *how* to re-deliver the user message.
--
--   The column is ``NOT NULL DEFAULT ''`` so legacy rows journaled
--   before v3 read back as the empty string (interpreted as "unknown
--   channel — fall back to user re-send") rather than NULL.
--
-- Idempotent (``ADD COLUMN IF NOT EXISTS``) — re-running on a cluster
-- that already has v3 applied is a no-op. The same DDL is also embedded
-- in :mod:`corlinman_server.agent_journal_postgres` so a fresh deploy
-- that opens the backend without pre-applying this file Just Works.

ALTER TABLE journal_turns
    ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT '';
