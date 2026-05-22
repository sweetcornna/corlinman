# PLAN — Agent Parity Implementation (Tier 1 / 2 / 3)

Status: **spec — not started** · Created 2026-05-23 · Owner: agent

The "why" and the full advantage catalog live in
`docs/RESEARCH_AGENT_PARITY.md`. This document is the "how": concrete
designs, files, signatures, contracts, effort, tests, and sequencing for
every borrow item across all three tiers.

Conventions used below:
- `agent/` = `python/packages/corlinman-agent/src/corlinman_agent/`
- `server/` = `python/packages/corlinman-server/src/corlinman_server/`
- `providers/` = `python/packages/corlinman-providers/src/corlinman_providers/`
- Every new tool follows the existing `coding/` contract: a
  `*_tool_schema()` + a `dispatch_*()` returning a JSON envelope string.
- Every change ships with unit tests and is independently deployable.

---

# TIER 1 — high value, bounded effort (~3 days)

## T1.1 — Tool-result truncation + freeze

**Problem.** `ReasoningLoop` keeps every tool result verbatim for all 60
rounds. A few `run_shell` / `read_file` results blow the context.

**Design.**
- New constant in `agent/reasoning_loop.py`: `_TOOL_RESULT_CAP = 8_000`
  (chars). Env override `CORLINMAN_TOOL_RESULT_CAP`.
- New helper `_truncate_tool_result(content: str) -> str`: if over the
  cap, keep the first 2k + last 5k chars joined by a
  `\n…[N chars elided]…\n` notice. Errors live at the tail, so the tail
  is weighted heavier.
- `_extend_with_tool_round()` (reasoning_loop.py ~L408) applies
  `_truncate_tool_result` to every `r.content` before building the
  `role="tool"` message. The truncation is permanent in the message
  history — once truncated, a result is never re-expanded ("freeze").
- `run_shell` (`coding/shell.py`): change the existing head-truncation
  to **tail-biased** and lower `_MAX_OUTPUT_CHARS` 30_000 → 16_000;
  when the full output is larger, also write it to
  `<workspace>/.corlinman/run_shell_<ts>.log` and return that path in
  the envelope so the model can `read_file` it if needed.

**Files.** `agent/reasoning_loop.py`, `agent/coding/shell.py`.
**Tests.** truncation keeps head+tail+notice; a 50KB shell output is
capped and the spill file exists and holds the full output.
**Effort.** ~½ day.

## T1.2 — Provider retry / backoff

**Problem.** The Codex backend rate-limits (429) and has transient 5xx.
The Python layer has no retry — a single blip fails the whole turn.

**Design.**
- New `providers/_retry.py`:
  ```python
  async def with_retry(make_attempt, *, max_attempts=5,
                       base_delay=0.5, max_delay=16.0,
                       retryable, on_retry=None): ...
  ```
  `make_attempt` is an async callable; `retryable(exc) -> float | None`
  returns a delay (honoring a `Retry-After` header parsed off the
  exception) or `None` to not retry. Exponential backoff with full
  jitter, capped at `max_delay`.
- `CodexProvider.chat_stream` (`providers/codex_provider.py`): wrap the
  **connection + first-event** phase in `with_retry`. Retry only while
  no `token`/`tool_call_*` chunk has been emitted yet — once output has
  started a retry would duplicate text, so mid-stream failures
  propagate as today.
- `retryable`: 429, 500, 502, 503, 504, and connection errors →
  retry; 400/401/403/404 and `insufficient_quota` → no retry.
- A `background: bool` flag on `chat_stream` (default `False`): when
  `True`, 529/overload bails immediately (Claude Code's
  foreground/background gate). The agent servicer passes `True` only
  for non-user-facing calls (none today — wired for future use).

**Files.** new `providers/_retry.py`, `providers/codex_provider.py`.
**Tests.** a stream that 429s twice then succeeds yields the final
text; a 401 is not retried; `Retry-After: 2` is honored.
**Effort.** ~1 day.

## T1.3 — Enriched system prompt + dynamic environment block

**Problem.** `_CODING_SYSTEM_PROMPT` (~30 lines) covers about half the
behavioral rules; there is no environment block.

**Design.**
- Rewrite `_CODING_SYSTEM_PROMPT` in `server/agent_servicer.py` to
  ~70 lines covering research items C1–C12: truthful reporting,
  verify-before-done, todo discipline, no speculative code, read-
  before-edit, tool hierarchy, destructive-action calibration, respect
  user changes, security default, conciseness, ask-only-when-blocked,
  minimal comments. (Draft text is in `RESEARCH_AGENT_PARITY.md` §C.)
- New `_build_env_block() -> str`: a `# Environment` section with the
  workspace path, platform, shell, OS, date, and the resolved model id.
  Computed once per turn.
- `_ensure_system_prompt()` appends the env block to the injected
  system message; when an agent card already supplied a system
  message, the env block is still appended (it is fact, not behavior).

**Files.** `server/agent_servicer.py`.
**Tests.** the injected system message contains the env block; an
agent-card system message is kept and gets the env block appended.
**Effort.** ~½ day.

## T1.4 — Cost / token tracking

**Problem.** Provider `usage` is discarded; there is zero cost
visibility.

**Design.**
- `ProviderChunk` (`providers/base.py`): add an optional
  `usage: dict[str, int] | None = None` field, populated only on the
  `done` chunk.
- `CodexProvider.chat_stream`: on the Responses API
  `response.completed` event, read `event.response.usage`
  (`input_tokens`, `output_tokens`, plus cached-token fields when
  present) and attach it to the terminal `done` chunk.
- `reasoning_loop.py`: surface usage on `DoneEvent` (add a `usage`
  field) by forwarding the provider's `done.usage`.
- `server/agent_servicer.py`: a `_CostMeter` accumulates per
  `session_key` — total input/output/cached tokens and a running
  request count — and logs `agent.cost.turn` after each turn and
  `agent.cost.session` totals. No pricing math (model prices drift);
  tokens are the durable unit.

**Files.** `providers/base.py`, `providers/codex_provider.py`,
`agent/reasoning_loop.py`, `server/agent_servicer.py`.
**Tests.** a stream whose `response.completed` carries usage produces a
`DoneEvent.usage`; the cost meter sums across two turns of a session.
**Effort.** ~½ day.

## T1.5 — grep polish

**Problem.** `search_files` content mode returns scan order, caps at
200 with no paging.

**Design.** In `agent/coding/search.py`:
- content mode: collect `(path, line, text)`, then sort the *files* by
  `st_mtime` descending so recently-edited files surface first; within
  a file keep line order.
- add an `offset: int` parameter (default 0) — results `[offset :
  offset+limit]`; report `next_offset` when truncated.
- confirm `_SKIP_DIRS` covers `.git/.svn/.hg/.bzr/node_modules/.venv/
  __pycache__/.mypy_cache` and is applied in *both* modes.

**Files.** `agent/coding/search.py`.
**Tests.** results ordered newest-file-first; `offset` pages correctly;
a `.git` dir is never searched.
**Effort.** ~½ day.

**Tier 1 sequencing.** All five are independent. Suggested order:
T1.3 (prompt) → T1.1 (truncation) → T1.5 (grep) → T1.4 (cost) →
T1.2 (retry). Ship + deploy + verify after each.

---

# TIER 2 — medium value / effort (~4–5 days)

## T2.1 — File-read cache + read tracker

**Design.** New `agent/coding/_filestate.py` with a `FileState` object
held by the agent servicer for the lifetime of a chat turn:
- `record_read(path, mtime, content_hash)` — called by
  `dispatch_read_file`.
- `cached_read(path) -> str | None` — returns content if the file's
  `mtime` is unchanged since the recorded read (skip the disk hit).
- `is_stale(path) -> bool` — used by T2.2's edit staleness guard.

The servicer creates one `FileState` per `Chat` RPC and threads it into
the coding dispatch calls. `dispatch_read_file` / `dispatch_edit_file`
gain an optional `state: FileState | None` parameter (default `None`
keeps them standalone-testable).

**Files.** new `agent/coding/_filestate.py`, `agent/coding/files.py`,
`server/agent_servicer.py`.
**Effort.** ~1 day.

## T2.2 — Fuzzy edit matcher + staleness guard

**Design.** `dispatch_edit_file` (`agent/coding/files.py`):
- Replace the single exact `str.count`/`replace` with a tiered locator
  (reuse + extend `apply_patch._locate`'s multi-pass idea):
  1. exact;
  2. per-line `rstrip`;
  3. per-line `strip` (indentation-flexible);
  4. block-anchor: match the first and last lines exactly, allow the
     interior to differ — used only when tiers 1–3 give exactly one
     candidate span.
  The uniqueness rule still holds: if a tier yields >1 span and
  `replace_all` is false, error.
- Staleness guard: when a `FileState` is supplied and the target file
  `is_stale`, return `error: file_changed_since_read` instead of
  editing blind.

**Files.** `agent/coding/files.py` (+ a shared `_locate` moved to
`agent/coding/_common.py`).
**Effort.** ~1.5 days.

## T2.3 — Token-aware context compaction

**Design.** In `agent/reasoning_loop.py`:
- `_estimate_tokens(messages) -> int` — `sum(len(text)) // 4` over all
  message/tool content (cheap heuristic; good enough for a budget).
- After each round, if the estimate exceeds
  `CORLINMAN_CONTEXT_BUDGET` (default 120_000): **elide** — walk the
  oldest tool rounds and replace each already-truncated
  `role="tool"` content with `"[older tool output elided]"`, keeping
  the assistant `tool_calls` shells intact so the transcript stays
  valid. Always keep: the system message, the original user turn, and
  the most recent 3 rounds verbatim.
- v1 is elision only. LLM-summarization compaction (Claude Code's
  approach) is noted as a future upgrade — it needs an extra provider
  round and is deferred.

**Files.** `agent/reasoning_loop.py`.
**Effort.** ~1 day.

## T2.4 — File-change snapshot + `revert_changes` tool

**Design.** The agent workspace becomes a git repo used purely as a
snapshot store:
- New `agent/coding/_snapshot.py`: `ensure_repo(workspace)` runs
  `git init` + an initial commit on first use; `snapshot(workspace,
  label)` does `git add -A && git commit` (allowing empty);
  `revert_last(workspace)` does `git reset --hard HEAD~1`;
  `list_snapshots(workspace)` returns recent commits.
- The agent servicer calls `snapshot()` once at the start of a turn
  (label = the user message head) so a turn's edits are one revertible
  unit.
- New builtin tool `revert_changes` — schema + `dispatch_revert_changes`
  — lets the model (or the user, via a chat command) undo the last
  snapshot. Workspace-confined; never touches anything outside.

**Files.** new `agent/coding/_snapshot.py`, new tool in
`agent/coding/`, wired into `agent_servicer.py` BUILTIN_TOOLS.
**Effort.** ~1.5 days.
**Risk.** git must be present on the host (it is, on the VPS). Degrade
gracefully — if `git` is missing, snapshotting is a logged no-op.

**Tier 2 sequencing.** T2.1 → T2.2 (T2.2 uses T2.1's `FileState`);
T2.3 and T2.4 are independent and can land in any order.

---

# TIER 3 — large / lower value for a chat bot

Documented as design sketches; implement only on a concrete need.
None of these are required for coding-agent parity — they are platform
features.

## T3.1 — Permission ruleset + tool gate

A declarative `(tool, pattern) → allow|deny|ask` rule list, evaluated
last-match-wins, default `allow` for read-only tools / `ask` for
mutating tools. For a QQ bot "ask" has no natural UI — it would map to
the bot replying with a confirmation prompt and waiting for the next
message, which needs a per-session pending-approval state machine.
**Design sketch:** `server/gateway` config block `[agent.permissions]`;
a `PermissionGate` consulted in `_dispatch_builtin` before mutating
tools; an `ask` verdict suspends the turn and emits a confirm prompt.
**Effort.** ~3–4 days. **Recommendation:** defer — the workspace
confinement + shell denylist already cover the realistic risk.

## T3.2 — Hook system

Pre/post-tool hook points loaded from a config file; a hook can block
or mutate a tool call. **Design sketch:** a `HookBus` already exists in
`corlinman-hooks`; extend it with `PreToolUse`/`PostToolUse` events
emitted around `_dispatch_builtin`; hooks are async callables resolved
from config. **Effort.** ~2 days. **Recommendation:** defer until a
concrete hook use-case exists.

## T3.3 — Coordinator mode for subagents

A parent agent orchestrating worker subagents with isolated tool sets
and a shared scratch dir. corlinman has `subagent_spawn` + `blackboard`
already — coordinator mode is the orchestration layer on top.
**Design sketch:** a `coordinator` agent card + a scratch dir per
parent session; workers get a restricted `start.tools`. **Effort.**
~5 days. **Recommendation:** defer — single-agent + `subagent_spawn`
covers current needs.

## T3.4 — Plugin lifecycle

Plugins contributing skills + hooks + MCP servers, enable/disable
persisted. corlinman has bundled skills + an MCP channel; a full plugin
lifecycle is a marketplace feature. **Effort.** ~4 days.
**Recommendation:** defer.

## T3.5 — Prompt caching

Mark the system prompt with a cache breakpoint so repeated turns hit
the provider cache. The Codex/Responses API path would need explicit
cache-control support; payoff is mostly latency/cost on long multi-turn
sessions. **Effort.** ~1 day once the API supports it. **Recommendation:**
revisit when measuring cost.

## T3.6 — LSP code intelligence

Language-server-backed go-to-def / find-refs / hover. Large subsystem;
`search_files` + `run_shell` cover the practical need for a chat bot.
**Recommendation:** out of scope.

---

# Overall sequencing & effort

| Batch | Items | Effort | Ships as |
|-------|-------|--------|----------|
| Tier 1 | T1.1–T1.5 | ~3 days | 5 independent commits, deploy+verify each |
| Tier 2 | T2.1–T2.4 | ~4–5 days | 4 commits; T2.2 depends on T2.1 |
| Tier 3 | T3.1–T3.6 | ~15+ days | design sketches only; build on demand |

Recommended execution: Tier 1 in full → deploy + end-to-end verify on
the VPS → Tier 2 in full → deploy + verify → reassess Tier 3 against
real usage before committing to any of it.

Each item's "Tests" note is the acceptance bar; nothing is marked done
until its tests pass and (for Tier 1/2) the change is verified live on
the VPS agent.
