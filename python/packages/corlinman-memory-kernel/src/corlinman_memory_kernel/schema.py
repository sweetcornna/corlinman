"""mk_* DDL — the memory-kernel tables, co-habiting ``memory.sqlite``.

Everything here is additive (``CREATE TABLE IF NOT EXISTS`` only): the
legacy ``files``/``chunks``/``memory_host_docs`` shapes owned by
``corlinman-memory-host`` are never altered, so a pre-kernel gateway can
open a post-kernel database and vice versa. Rollback is flag-flip only.

Design notes (see the memory-kernel plan for the full rationale):

- ``mk_items`` is the canonical memory record: one atomic claim per row,
  scoped by ``(tenant_id, scope_user_id, persona_id)``, **bi-temporal**
  (``valid_from_ms``/``valid_to_ms`` = when the fact held in the world;
  ``recorded_at_ms``/``invalidated_by`` = when the system learned /
  retired it). Reconciliation and trust floors *invalidate* rows — they
  never delete.
- ``mk_observations`` is the hot-path ingest queue: one row per completed
  turn, drained by the sleep-time reconcile job (``processed_at_ms``).
- ``mk_recall_ledger`` records exactly what was injected per turn — the
  substrate for the implicit trust loop and the shadow evals.
- ``mk_core`` / ``mk_scope_grants`` / ``mk_affect_state`` are created now
  (cheap) and populated by later waves.
"""

from __future__ import annotations

MK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mk_items (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    scope_user_id   TEXT,
    persona_id      TEXT NOT NULL DEFAULT '',
    visibility      TEXT NOT NULL DEFAULT 'private',
    kind            TEXT NOT NULL,
    text            TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_ref      TEXT,
    node_id         TEXT,
    risk            TEXT NOT NULL DEFAULT 'low',
    confidence      REAL NOT NULL DEFAULT 0.6,
    importance      REAL NOT NULL DEFAULT 0.5,
    trust           REAL NOT NULL DEFAULT 0.5,
    utility         REAL NOT NULL DEFAULT 0.5,
    valid_from_ms   INTEGER NOT NULL,
    valid_to_ms     INTEGER,
    recorded_at_ms  INTEGER NOT NULL,
    invalidated_by  TEXT,
    invalid_reason  TEXT,
    affect_e        REAL,
    affect_p        REAL,
    affect_a        REAL,
    affect_salience REAL NOT NULL DEFAULT 0.0,
    last_recalled_ms INTEGER,
    recall_count    INTEGER NOT NULL DEFAULT 0,
    use_count       INTEGER NOT NULL DEFAULT 0,
    contradict_count INTEGER NOT NULL DEFAULT 0,
    embedding       BLOB,
    embedding_dim   INTEGER,
    schema_version  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mk_items_scope
    ON mk_items(tenant_id, scope_user_id, persona_id, valid_to_ms);
CREATE INDEX IF NOT EXISTS idx_mk_items_kind
    ON mk_items(tenant_id, kind, valid_to_ms);

-- trigram tokenizer (not unicode61): unicode61 treats a contiguous CJK
-- run as ONE token, so Chinese recall only matched exact whole strings.
-- Trigram gives substring matching for CJK and English alike; the query
-- builder compensates for its 3-char minimum (see _fts_match_query).
CREATE VIRTUAL TABLE IF NOT EXISTS mk_items_fts USING fts5(
    text,
    content='mk_items',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS mk_items_ai AFTER INSERT ON mk_items BEGIN
    INSERT INTO mk_items_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS mk_items_ad AFTER DELETE ON mk_items BEGIN
    INSERT INTO mk_items_fts(mk_items_fts, rowid, text)
        VALUES('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS mk_items_au AFTER UPDATE ON mk_items BEGIN
    INSERT INTO mk_items_fts(mk_items_fts, rowid, text)
        VALUES('delete', old.rowid, old.text);
    INSERT INTO mk_items_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS mk_edges (
    src_id        TEXT NOT NULL,
    dst_id        TEXT NOT NULL,
    rel           TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (src_id, dst_id, rel)
);

CREATE TABLE IF NOT EXISTS mk_observations (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    session_key     TEXT NOT NULL,
    channel         TEXT,
    channel_user_id TEXT,
    scope_user_id   TEXT,
    persona_id      TEXT NOT NULL DEFAULT '',
    user_text       TEXT NOT NULL,
    reply_text      TEXT NOT NULL,
    ts_ms           INTEGER NOT NULL,
    affect_e        REAL,
    affect_p        REAL,
    affect_a        REAL,
    processed_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mk_obs_pending
    ON mk_observations(tenant_id, processed_at_ms, ts_ms);

CREATE TABLE IF NOT EXISTS mk_recall_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_key      TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    lane          TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    score         REAL NOT NULL,
    shown_chars   INTEGER NOT NULL,
    ts_ms         INTEGER NOT NULL,
    verdict       TEXT,
    verdict_score REAL,
    verdict_tier  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mk_ledger_turn ON mk_recall_ledger(turn_key);
CREATE INDEX IF NOT EXISTS idx_mk_ledger_item ON mk_recall_ledger(item_id, ts_ms);

CREATE TABLE IF NOT EXISTS mk_core (
    tenant_id     TEXT NOT NULL,
    scope_user_id TEXT NOT NULL DEFAULT '',
    persona_id    TEXT NOT NULL DEFAULT '',
    block         TEXT NOT NULL,
    content       TEXT NOT NULL,
    char_budget   INTEGER NOT NULL DEFAULT 1200,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, scope_user_id, persona_id, block)
);

CREATE TABLE IF NOT EXISTS mk_scope_grants (
    from_persona TEXT NOT NULL,
    to_persona   TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT '*',
    granted_by   TEXT NOT NULL,
    ts_ms        INTEGER NOT NULL,
    PRIMARY KEY (from_persona, to_persona, kind)
);

-- Persona mood is deliberately GLOBAL per persona (not per user): the
-- persona is one character with one mood/diary across all its chats,
-- matching agent_persona_state's keying — every conversation partner
-- influences and experiences the same mood.
CREATE TABLE IF NOT EXISTS mk_affect_state (
    persona_id    TEXT PRIMARY KEY,
    mood_e        REAL NOT NULL DEFAULT 0,
    mood_p        REAL NOT NULL DEFAULT 0,
    mood_a        REAL NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);
"""
