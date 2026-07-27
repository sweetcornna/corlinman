# PLAN — Agent status card (shareable channel link → status + trajectory)

Goal: the agent can hand the user a **clickable URL in the channel** that opens a
web "status card" showing the agent's **current status** and **work trajectory**
(reasoning → tool calls → subagent spawns/results) for that conversation — without
the user needing an admin login.

## What already exists (researched — reuse heavily)

- **Backend trajectory data is already there.** The per-turn **journal** (`AgentJournal`,
  sqlite) stores turns (`status` = in_progress|completed|errored, `elapsed_ms`,
  `tool_call_count`, cost) + a granular `turn_events` timeline (`TurnStart`,
  `ToolStateRunning/Completed`, `SubagentSpawned/Completed`, …). The
  `JournalBackedEmitter` tees persist + live SSE. Admin routes already expose it:
  `GET /admin/sessions`, `/admin/sessions/{key}/turns`, `/admin/sessions/{key}/events/live`
  (resumable SSE). Keyed by **`session_key`** (the conversation identity).
- **Rich UI components exist** (admin): `EventTimeline` (replay+live), `SubagentRow` +
  state-presentation, `ToolCallCard`, `SubagentCard`, elapsed-counter hook, Tidepool
  glass theme. All currently behind the **admin-session cookie**.
- **Channels ship URLs verbatim** in the agent's normal reply text — no special send
  tool needed (the agent just includes the link in its reply).

## The three real gaps

1. **No public/token access** — every status endpoint requires the admin cookie. A
   chat user clicking the link isn't the admin.
2. **No public base URL** — gateway boots on `127.0.0.1:6005`; there's no
   `CORLINMAN_PUBLIC_URL` so nothing can build an absolute, shareable link.
3. **No "give me my status link" affordance** — the agent has no way to mint/scope a
   per-session link.

## Design (5 pieces)

### 1. Public base URL config
Add `CORLINMAN_PUBLIC_URL` (env) / `gateway.public_url` (TOML), surfaced on AppState +
the agent servicer. Empty → the status tool returns a clear "operator must set
CORLINMAN_PUBLIC_URL" envelope instead of a broken localhost link.

### 2. Stateless signed share token  (`gateway/status_token.py`)
`make_status_token(session_key, ttl=24h)` → an opaque HMAC-signed token
(`itsdangerous`-style: `b64(session_key|exp).<hmac>`), `verify_status_token(token)` →
`session_key | None`. Signed with a server secret (dedicated
`CORLINMAN_STATUS_SIGNING_KEY`, else derived from the existing admin/API key). **No DB** —
the token *is* the read-only, time-limited, single-session capability.

### 3. Token-gated public API  (`gateway/routes/status.py`, mounted OUTSIDE admin auth)
Read-only, scoped to the token's one `session_key`, reusing the journal queries +
emitter:
- `GET /status/{token}` → `{session_key, status, started_at, last_activity, turns:[…],
  current_step}` (current_step = last `ToolStateRunning` with no matching Completed).
- `GET /status/{token}/events` (replay) + `/status/{token}/events/live` (SSE) → the
  trajectory. Invalid/expired token → 403. No kill/approve actions (viewer-only).

### 4. Public status-card page  (`ui/app/status/[token]/page.tsx`, outside `(admin)`)
A standalone, mobile-friendly page (opened from chat) — no admin shell/auth. Header:
current status pill + elapsed. Body: the trajectory via a **read-only** reuse of
`EventTimeline` + `ToolCallCard` + `SubagentCard` (kill buttons stripped), driven by the
token endpoints. Live-updates via the token SSE while the turn is in progress.

### 5. The affordance — how the link reaches the channel
- **Builtin tool `agent_status_card`**: returns `{url}` for the *current* session (the
  servicer builds it from `CORLINMAN_PUBLIC_URL` + `make_status_token(start.session_key)`).
  The agent includes the URL in its reply ("查看我的实时状态 👉 <url>"), which channels send
  verbatim. Wired like the other builtins.
- **`/status` channel command** (optional, like `/help` `/whoami`): the channel handler
  replies with the link directly — lets the *user* pull it without prompting the agent.

## Scope split

**MVP** (pieces 1–3 + a minimal page 4 + the tool 5a): signed token, public status API
(status + replay), a clean single-page status card (current status + trajectory replay,
poll-refresh), and the `agent_status_card` tool. Ships the core "click a link → see
status + trajectory".

**Full** (+ live SSE on the public page, the `/status` channel command, polished
mobile card with nested subagent cards + action-trace styling, token expiry/refresh UX).

## Verify
Backend: ruff/mypy + route tests (token sign/verify, 403 on bad token, scoping to one
session). UI: build + a real run (`/run`) opening a status link against a live session —
this feature genuinely needs the app run to verify, unlike the pure-backend work.

## Risks / decisions
- **Token leakage**: anyone with the link sees that session's trajectory. Mitigate with
  expiry (24h) + the link only exposing one session, read-only. (Acceptable for a
  user-shareable status card; note in docs.)
- **Signing key bootstrap**: needs a stable secret across restarts (derive from the
  persisted admin key so it survives reboots).
- **Trajectory privacy**: tool-call args/results may contain sensitive content. Consider
  a redaction pass or a per-deployment toggle for what the public card shows.
