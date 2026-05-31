# Follow-up gaps & TODO (as of v1.12.0)

Concrete, prioritized gaps surfaced while shipping the recent features (persona
life/qzone v1.11.0, dynamic subagents + status-card foundation v1.12.0). Each
item below maps 1:1 to a GitHub issue filed via `scripts/gh_followup_issues.sh`.

## Status card — feature is foundation-only (highest-priority cluster)
The `agent_status_card` tool + signed token shipped in v1.12.0; the link is not
yet usable end-to-end.

- [ ] **HIGH — public `GET /status/{token}` route.** Verify the token →
  read the per-turn journal (`list_session_turns` / `get_session_turn_ids`) →
  return `{session_key, status, turns[…], current_step}` JSON. Mounts at root
  via `routes/register.build_app_router` (auth only gates `/v1/` + `/admin/*`,
  so `/status` is public). Without it the tool's link 404s.
- [ ] **HIGH — public status-card UI page** `ui/app/status/[token]/page.tsx`
  (outside `(admin)`). Reuse `EventTimeline` / `ToolCallCard` / `SubagentCard`
  read-only (strip kill buttons). Needs a live app run to verify.
- [ ] **MED — trajectory privacy/redaction.** Tool-call args/results in the
  public card may carry sensitive content. Add a redaction pass or a
  per-deployment "what the public card shows" toggle before wide use.
- [ ] **MED — live SSE on the public page** (`/status/{token}/events/live`),
  reusing `JournalBackedEmitter.subscribe`. MVP is poll-refresh.
- [ ] **LOW — `/status` channel command** (user-pull, like `/help` `/whoami`):
  the channel handler replies with the link directly.
- [ ] **LOW — `CORLINMAN_PUBLIC_URL` in the TOML config schema** (currently
  env-only) + surfaced on AppState.
- [ ] **LOW — token revocation.** No way to invalidate a leaked link before its
  TTL expires; consider a per-session epoch in the signature.

## Dynamic subagents — follow-ups
- [ ] **MED — make supervisor policy configurable** via `[subagent]` config
  (today hardcoded defaults: depth 2, 3/parent, 15/tenant).
- [x] **LOW — inline + `run_in_background`.** `subagent.spawn_inline` now
  uses the same async store/dispatcher path as named background spawns and
  persists the inline prompt/model on the background request.
- [x] **LOW — per-session `child_seq` counter.** Servicer dispatch now reserves
  per-session child sequence ids across named, inline, and `spawn_many` calls so
  sequential spawns cannot reuse the same mangled child id.
- [x] **LOW — eager model-override validation.** Servicer dispatch validates
  `model` overrides for named, inline, and `spawn_many` subagent calls before
  launching children and rejects unknown aliases with `model_alias_invalid`.

## Persona life — follow-up
- [x] **MED — `{{persona.life_*}}` placeholder surfacing depends on
  `agent_id == persona_id`.** Servicer context assembly now stamps the
  placeholder metadata with `agent_id` from an explicit `start.extra["agent_id"]`
  or, for humanlike channel bindings, falls back to `start.extra["persona_id"]`
  so life-state writes and placeholder reads share the same persona key.

## Release / ops
- [ ] **HIGH — publish v1.11.0 + v1.12.0 to GitHub.** Both are committed +
  tagged locally and v1.11.0 runs on the prod VPS, but neither is pushed (the
  dev harness has no outbound network to github.com). Push `main` (clean
  fast-forward) + both tags, then create the two GitHub releases.
