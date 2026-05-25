-- corlinman-server agent journal — SQLite schema v4: turn_events.
--
-- Adds per-turn event timeline storage (W1.2 of the Task Observability
-- overhaul). Every ``EventEnvelope`` the gateway emits gets persisted
-- here so the admin UI replay endpoint can stream a past turn with the
-- same fidelity as a live observer.
--
-- Migration policy:
--   * Idempotent — every statement uses ``IF NOT EXISTS`` or is gated
--     by a Python-side ``PRAGMA table_info`` check before being run.
--     Re-running on a hot-restarted DB is a no-op.
--   * Additive only — no existing column/table is changed in a way
--     that would break a pre-migration reader.
--   * SQLite has no native ``ADD COLUMN IF NOT EXISTS``; the loader
--     issues these ALTERs only when the column is missing.
--
-- Applied automatically by :class:`SqliteJournalBackend` at open time
-- (see ``_apply_turn_events_migration``). Operators can also pre-apply
-- via ``sqlite3 <journal.db> < 004_turn_events.sql`` if they wish.

CREATE TABLE IF NOT EXISTS turn_events (
    turn_id      TEXT    NOT NULL,
    sequence     INTEGER NOT NULL,
    event_type   TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    PRIMARY KEY (turn_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_turn_events_turn
    ON turn_events(turn_id);

CREATE INDEX IF NOT EXISTS idx_turn_events_timestamp
    ON turn_events(timestamp_ms);

-- The following ALTERs are emitted by the Python loader only when the
-- column is absent (SQLite < 3.35 has no ``ADD COLUMN IF NOT EXISTS``,
-- and even on 3.35+ we keep the gate so older deployments roundtrip).
-- Documented here so a DBA reading the .sql sees the full target shape.
--
--   ALTER TABLE turns ADD COLUMN elapsed_ms INTEGER;
--   ALTER TABLE turns ADD COLUMN estimated_cost_usd REAL;
--   ALTER TABLE turns ADD COLUMN cost_status TEXT;
--   ALTER TABLE turns ADD COLUMN tool_call_count INTEGER DEFAULT 0;
--   ALTER TABLE turns ADD COLUMN reasoning_token_count INTEGER DEFAULT 0;
