# Follow-up gaps & TODO (as of v1.12.0)

Concrete, prioritized gaps surfaced while shipping the recent features (persona
life/qzone v1.11.0, dynamic subagents + status-card foundation v1.12.0). Each
item below maps 1:1 to a GitHub issue filed via `scripts/gh_followup_issues.sh`.

## Status card — feature is foundation-only (highest-priority cluster)
The `agent_status_card` tool + signed token shipped in v1.12.0; the link is not
yet usable end-to-end.

- [x] **HIGH — public `GET /status/{token}` route.** Verify the token →
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
- [ ] **LOW — inline + `run_in_background`.** `subagent.spawn_inline` rejects
  background with `run_in_background_not_implemented`; wire the async store path.
- [ ] **LOW — per-session `child_seq` counter.** Single spawns pass `child_seq=0`
  so sequential inline/named spawns in one turn can share a mangled child id
  (observability only).
- [ ] **LOW — eager model-override validation.** An invalid `model` alias only
  surfaces at child dispatch (`FinishReason.ERROR`); validate up front.

## Persona life — follow-up
- [ ] **MED — `{{persona.life_*}}` placeholder surfacing depends on
  `agent_id == persona_id`.** Life-state is keyed by `persona_id`, but the
  `PersonaResolver` resolves by `ctx.metadata["agent_id"]`, which
  `_context_metadata` does not stamp today. Wire `agent_id` into the placeholder
  ctx (or document the single-persona convention).

## Release / ops
- [ ] **HIGH — publish v1.11.0 + v1.12.0 to GitHub.** Both are committed +
  tagged locally and v1.11.0 runs on the prod VPS, but neither is pushed (the
  dev harness has no outbound network to github.com). Push `main` (clean
  fast-forward) + both tags, then create the two GitHub releases.
