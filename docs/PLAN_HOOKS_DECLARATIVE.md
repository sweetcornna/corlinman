# PLAN — Dim 9: Declarative hooks settings + `/hooks` (claude-code parity, Wave 3)

Date: 2026-07-03. Branch: `feat/hooks-declarative` off `main@6cee0f0c`.
Decision source: `audit/ABSORB_MATRIX_2026-07-02.md:135-140` (ADAPT-ADOPT) +
`docs/parity-matrix-2026-06-11.json:171-187` (hooks cluster). Research basis:
three-agent sweep 2026-07-03 (substrate map / command layer / baseline
contract) — facts below are verified against `main@6cee0f0c`, not docs.

## 0. Verified current state (what we layer on)

- Two disjoint systems: typed async `HookBus` (31 `HookEvent` variants,
  `emit_collect` decision path **unused in prod**) and imperative
  `HookRunner` (config `[hooks]` flat `event_key → shell cmd` + discovered
  `HOOK.yaml`/`handler.py`).
- The **only live blocking path**: `run_pre_tool_async` consulted in
  `_dispatch_builtin` (`agent_servicer.py:2695-2771`) — deny short-circuits
  with `hook_blocked`, `mutated_args` re-encoded onto `event.args_json`.
- Dead-but-present surface: `run_post_tool` (no callers), `run_stop`
  (supported by `ReasoningLoop._maybe_run_stop_hook` `reasoning_loop.py:2495`
  but servicer never passes `hook_runner` at `agent_servicer.py:1644`),
  `run_notification` (no callers). Bus variants with no emit site: `Stop`,
  `PreToolDecision`, `SessionStart/End/Reset`, `Pre/PostCompact`, etc.
- Config: no pydantic model for `[hooks]`; dict flows raw into
  `HookRunner.__init__` (`runner.py:179-183`). Runner built once at boot in
  two sites (`main.py:70-115`, `c2_wiring.py:328-360`) + console
  (`embedded.py:269-271`); **no rebuild on config change** even though
  ConfigWatcher diffs the `hooks` section (`config_watcher.py:487`).
- Shell contract today: stdin JSON; **exit 0 = allow, non-zero = deny**
  (pre_tool) / veto+inject (stop); timeout 5s → fail-open. No env injection.
- `/admin/hooks` GET read-only (`routes_admin_b/infra/hooks.py:61-106`);
  shows shell hooks only; hardcoded 3-event fallback when runner absent.
- `/hooks` slash name unclaimed. Console `_REGISTRY` row = autocomplete for
  free (`console/app.py:57-81`). `/permissions` is the shape template
  (`console/commands.py:280-313`).

## 1. Deliverable (the 4 recorded gaps, nothing more)

1. **Declarative settings shape**: event → matcher-groups → hook defs of
   kind `command | prompt | agent | http`.
2. **`if` matcher** reusing the permission-rule grammar (`Bash(git *)`).
3. **`/hooks` console command** (view / test / reload).
4. Wire the cheap dead surfaces so declarative hooks actually fire:
   post-tool call site, Stop via `hook_runner` pass-through,
   UserPromptSubmit, Session/Compact emit sites where the paths already
   exist.

Out of scope (recorded follow-ups, do NOT build now): `Notification` /
`Setup` / `FileChanged` new event variants; async exit-2 rewake (maps onto
the existing mid-turn `UserSupplemented` injection — own slice);
statusMessage spinner rendering; interactive `/hooks` editor UI (config UI
already edits TOML); stream-json hook-event lines (`ABSORB_MATRIX:104`).

## 2. Config shape (TOML-native, backwards compatible)

`[hooks]` keeps its legacy flat keys untouched (documented contract).
New sub-tables, one per event, arrays of matcher groups:

```toml
[hooks]
enabled = true               # existing
pre_tool = "legacy.sh"       # legacy flat keys still work, run FIRST

[[hooks.declarative.PreToolUse]]
matcher = "Bash"                       # tool-name pattern: exact | A|B | *
if = "Bash(git push*)"                 # optional permission-rule refinement
hooks = [
  { kind = "command", command = "./guard.sh", timeout = 10 },
  { kind = "http", url = "http://127.0.0.1:9911/hook" },
]

[[hooks.declarative.Stop]]
hooks = [ { kind = "prompt", prompt = "Did the agent finish the task? Reply JSON {\"ok\":bool,\"reason\":str}" } ]
```

- Event names = claude-code names, mapped internally:
  `PreToolUse→pre_tool`, `PostToolUse→post_tool`, `Stop→stop`,
  `UserPromptSubmit→user_prompt_submit`, `SessionStart/End/Reset`,
  `PreCompact/PostCompact`, `Notification→notification` (accepted in config,
  fires only if a call site exists; unknown event names → boot warning, not
  crash).
- `matcher` omitted or `"*"` = match all. `matcher` grammar: exact tool
  name, `|` alternation, `*` suffix glob. Case-sensitive.
- `if` value parsed by the **injected** rule matcher (see §3 dependency
  rule); on parse failure: log once + treat group as non-matching
  (fail-closed for the group, fail-open for the tool call).
- Per-hook `timeout` (seconds, default 5.0 = existing `_HOOK_TIMEOUT`),
  `async` (bool, default: false for Pre*/Stop events, true for Post*/
  Session*/UserPromptSubmit — post-events cannot block by construction).

## 3. New module: `corlinman_hooks/declarative.py`

- `parse_declarative(section: dict) -> DeclarativeConfig` — pure, defensive,
  returns typed dataclasses (`MatcherGroup`, `HookDef`); collects
  `warnings: list[str]` instead of raising (surfaced by `/hooks` + boot log).
- `DeclarativeEngine.run(event_key, tool, payload, ctx) -> HookDecision` —
  evaluates matcher groups in file order; within a group, hooks in listed
  order; **first explicit deny short-circuits** (same fold as
  `_run_handlers` `runner.py:382-416`); allow-path merges
  `mutated_args`/`inject_message` forward.
- **Dependency rule**: `corlinman-hooks` must not import `corlinman-agent`
  (agent already depends on hooks). The `if` grammar is therefore injected:
  `HookRunner(..., rule_matcher: Callable[[str, str, dict], bool] | None)`.
  Wiring sites pass a closure over `corlinman_agent.permission` (grammar
  designed once, per `parity-matrix:38`). Unset → `if` clauses log-and-skip.

### Executor kinds and contracts

| kind | transport | payload | verdict |
|---|---|---|---|
| `command` | subprocess shell, stdin JSON | `{event, tool_name, tool_input, session_key, tenant_id, user_id}` | exit-code table below |
| `http` | POST JSON, `Content-Type: application/json` | same JSON body | 2xx + body `{decision:"allow"\|"block", reason?, mutated_args?, inject_message?}`; non-2xx / bad JSON / network error → fail-open + log |
| `prompt` | injected `prompt_evaluator(prompt, payload) -> dict` async callable | prompt + payload | returned `{ok: bool, reason?}`; unwired → log unavailable, fail-open |
| `agent` | injected `agent_evaluator(instructions, payload) -> dict` async callable | instructions + payload | same verdict shape; unwired → fail-open |

**Unified exit-code table for declarative `command` hooks** (resolves the
recorded conflict between claude-code "exit 2" and legacy "non-zero = deny";
legacy flat keys keep their old table —两套并存, documented):

- `0` — allow; if stdout is JSON, parse optional
  `{decision, reason, mutated_args, inject_message}` (decision `"block"`
  wins over exit 0).
- `2` — **block**; stderr (fallback stdout, trunc 500) = reason fed back to
  the model (claude-code semantic).
- other — non-blocking error: log + fail-open (do NOT deny — matches
  claude-code "other codes show error, don't block").
- timeout — kill, fail-open, log (unchanged posture).

`prompt`/`agent` evaluator wiring: servicer passes closures using the
existing small-fast-model routing (console PR #88 substrate) and
`subagent_spawn` inline path. Console `embedded.py` wires the same. Gateway
`c2_wiring.py` wires `prompt_evaluator` only if a provider registry is
available at that point; else leaves unwired (fail-open, logged).

## 4. Wiring changes (each small, each tested)

1. `HookRunner` gains `declarative: DeclarativeEngine` (parsed in
   `__init__` from `config["hooks"]["declarative"]`). `run_pre_tool_async`
   order: legacy shell → discovered handlers → declarative (first deny
   wins; mutations merge in that order).
2. **Post-tool call site**: `_dispatch_builtin` exit paths already fire
   `_emit_tool_called` (`agent_servicer.py:2354-2378`); add fire-and-forget
   `run_post_tool_async` (new async twin of `:542`) alongside — result
   included, deny impossible by contract.
3. **Stop**: pass `hook_runner=self._resolve_hook_runner()` into
   `ReasoningLoop(...)` at `agent_servicer.py:1644` — activates the existing
   `_maybe_run_stop_hook` (`reasoning_loop.py:2495`) veto+inject path for
   legacy AND declarative.
4. **UserPromptSubmit**: next to the existing bus emit
   (`agent_servicer.py:1389`), add declarative run (async groups only —
   cannot block; a `"block"` verdict here maps to inject_message prepended
   as system note, not turn abort — v1 documented behavior).
5. **Session/Compact events**: emit sites where paths exist —
   `SessionReset` in the `/new`-epoch path, `Pre/PostCompact` around the
   compaction call in `reasoning_loop.py` (bus emit + declarative async
   run). `SessionStart/End` only if a ≤10-line seam exists at session
   create/teardown; otherwise leave documented as unwired (config accepted,
   noted in `/hooks` output as "no live emitter").
6. **Reload**: `HookRunner.reload(config, hooks_dir)` re-parses shell keys +
   declarative + re-discovers; called from (a) `/hooks reload`, (b) a
   ConfigWatcher callback on `hooks`-section diff (watcher already opt-in;
   callback registered in `c2_wiring.py`).

## 5. `/hooks` console command

`SlashCommand` row in `_REGISTRY` (autocomplete free). Console-only v1
(like `/permissions`); channel exposure deferred.

- `/hooks` — table: legacy keys, discovered handlers, declarative groups
  (event / matcher / kinds / async), per-event live-emitter status, parse
  warnings, runner source (config path + hooks_dir).
- `/hooks test <event> [tool] [json-args]` — dry-run through the full fold
  with a synthetic payload; prints each hook's verdict + timing; never
  mutates state.
- `/hooks reload` — calls `HookRunner.reload`; prints diff summary
  (added/removed/changed counts).

`/admin/hooks` GET extended (same read-only posture): add `declarative`
(groups), `discovered` (names+events), `warnings`, `live_events`. Remove
the stale hardcoded 3-event fallback (return `supported_events: []` when
runner absent).

## 6. Test plan (TDD; new tests first)

- `corlinman-hooks/tests/test_declarative.py`: parse (good/bad/unknown
  event/warning collection), matcher grammar (exact/`|`/`*`/case), `if`
  injection + unset behavior, exit-code table (0 / 0+JSON-block / 2 / 7 /
  timeout), http verdict shapes + network fail-open, prompt/agent
  wired+unwired, deny short-circuit + mutation merge order, async groups
  never block, legacy-before-declarative order, reload diff.
- `corlinman-server/tests/`: pre-tool gate end-to-end with declarative deny
  (`hook_blocked` envelope), post-tool fire-and-forget (result present, no
  block possible), Stop veto now live through servicer-constructed loop,
  UserPromptSubmit inject, reload via watcher callback, `/hooks` command
  trio (view/test/reload), `/admin/hooks` new fields.
- Existing suites must stay green untouched — legacy `[hooks]` contract
  tests (`test_hook_runner.py`, `test_gf_hooks_runner.py`) unchanged =
  backwards-compat proof.

## 7. Commit sequence

1. `feat(hooks): declarative settings parse + matcher engine + command kind`
2. `feat(hooks): http/prompt/agent executor kinds via injected evaluators`
3. `feat(agent): wire declarative hooks — post-tool, Stop, UserPromptSubmit, compact/session emits, reload`
4. `feat(console): /hooks view/test/reload + /admin/hooks declarative fields`
5. `docs + config.example.toml + CHANGELOG + version bump`

Each commit: full `make ci` green locally. Then PR → Codex loop (poll twice,
10-25 min per push) → merge. Risks: (a) pre-tool hot path — declarative
evaluation must be O(groups) dict work when no groups configured (zero-cost
default); (b) `service.py` untouched — no channel contract risk; (c) the
`:compact` suffix + `supports_tools` contracts untouched.
