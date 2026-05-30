#!/usr/bin/env bash
# Publish v1.11.0 + v1.12.0 to GitHub and file the v1.12.0 follow-up issues.
#
# Run this from a machine where `git push` to origin works AND `gh` is
# authenticated (the dev harness that built these releases has no outbound
# network to github.com). Idempotent: re-running skips releases/issues that
# already exist.
#
#   bash scripts/gh_publish_v1.12.0.sh
set -uo pipefail
cd "$(dirname "$0")/.."

echo "== 1/3  push main + tags (clean fast-forward through v1.11.0, v1.12.0) =="
git push origin main
git push origin v1.11.0 v1.12.0

# --- release notes = the matching CHANGELOG section ------------------------
notes() { awk -v v="## [$1]" '
  $0 ~ /^## \[/ { if (started) exit; if (index($0, v)==1) { started=1; next } }
  started { print }' CHANGELOG.md; }

mkrelease() { # tag  title  version
  if gh release view "$1" >/dev/null 2>&1; then echo "  skip (exists): $1";
  else gh release create "$1" --title "$2" --notes "$(notes "$3")" && echo "  created: $1"; fi
}

echo "== 2/3  GitHub releases =="
mkrelease v1.11.0 "v1.11.0 — Persona life system + QZone comments" 1.11.0
mkrelease v1.12.0 "v1.12.0 — Dynamic subagents + status-card foundation" 1.12.0

# --- follow-up issues (see docs/TODO_FOLLOWUPS.md) --------------------------
mkissue() { # title  body
  if gh issue list --state open --search "$1 in:title" --json title --jq '.[].title' 2>/dev/null | grep -qxF "$1"; then
    echo "  skip (exists): $1"
  else
    gh issue create --title "$1" --body "$2" >/dev/null && echo "  filed: $1"
  fi
}

echo "== 3/3  follow-up issues =="
mkissue "[status-card] public GET /status/{token} route (HIGH)" \
"Verify the signed token (gateway/status_token.verify_status_token) -> read the per-turn journal (list_session_turns / get_session_turn_ids) -> return {session_key, status, turns[], current_step} JSON. Mounts at root via routes/register.build_app_router (auth gates only /v1/ + /admin/*, so /status is public). Without it the agent_status_card link 404s. See docs/PLAN_AGENT_STATUS_CARD.md."

mkissue "[status-card] public status UI page ui/app/status/[token]/page.tsx (HIGH)" \
"Standalone public page (outside (admin)). Render current status + work trajectory reusing EventTimeline / ToolCallCard / SubagentCard read-only (strip kill buttons), driven by the token endpoints. Needs a live app run to verify. See docs/PLAN_AGENT_STATUS_CARD.md."

mkissue "[status-card] trajectory privacy / redaction for the public card (MED)" \
"Tool-call args/results on the public status card may carry sensitive content. Add a redaction pass or a per-deployment 'what the public card shows' toggle before wide use."

mkissue "[status-card] live SSE on the public status page (MED)" \
"Add GET /status/{token}/events/live reusing JournalBackedEmitter.subscribe so the public card updates live while the turn is in progress. MVP is poll-refresh."

mkissue "[status-card] /status channel command (LOW)" \
"Let the user pull the status link without prompting the agent: a /status channel command (like /help, /whoami) whose handler replies with the link."

mkissue "[status-card] CORLINMAN_PUBLIC_URL in the TOML config schema (LOW)" \
"Currently env-only. Add gateway.public_url to the TOML config schema + surface it on AppState so it's configurable alongside the rest of the gateway config."

mkissue "[status-card] token revocation (LOW)" \
"A leaked status link is valid until its TTL expires with no way to revoke. Consider a per-session epoch folded into the signature so an operator can invalidate outstanding links."

mkissue "[subagent] make supervisor policy configurable via [subagent] config (MED)" \
"Supervisor caps are hardcoded defaults (depth 2, 3/parent, 15/tenant) in agent_servicer._get_subagent_caps. Read them from a [subagent] config block."

mkissue "[subagent] inline + run_in_background (LOW)" \
"subagent.spawn_inline rejects background with run_in_background_not_implemented. Wire the same async store path the named background spawn uses."

mkissue "[subagent] per-session child_seq counter (LOW)" \
"Single spawns pass child_seq=0, so sequential inline/named spawns in one turn can share a mangled child id (observability only). Thread a per-session counter."

mkissue "[subagent] eager model-override validation (LOW)" \
"An invalid 'model' alias only surfaces at child dispatch (FinishReason.ERROR). Validate the alias against the gateway config up front and reject with a clear error."

mkissue "[persona-life] {{persona.life_*}} surfacing depends on agent_id==persona_id (MED)" \
"Life-state is keyed by persona_id but PersonaResolver resolves by ctx.metadata['agent_id'], which _context_metadata does not stamp today. Wire agent_id into the placeholder ctx (or document the single-persona convention). See project_grantley_life_qzone_migration."

echo "== done =="
