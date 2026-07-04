# Evolution loop — scheduled engine runs

Phase 2 wave 2-B closes the auto-evolution loop by wiring the Python
`corlinman-evolution-engine` CLI as a scheduled subprocess job:

```
┌────────────────────┐  daily 03:00 UTC   ┌──────────────────────────┐
│ corlinman-scheduler│ ─────────────────▶ │ corlinman-evolution-engine │
│(Python, in-process)│                    │ (Python, subprocess)        │
└──────────┬─────────┘                    └──────────────┬──────────────┘
           │ exit code + duration                        │ writes proposals
           ▼                                              ▼
┌────────────────────┐    HookBus     ┌────────────────────────────────┐
│ EngineRun{Completed│ ─────────────▶ │ EvolutionObserver               │
│        ,Failed}    │                │  → evolution_signals (sqlite)   │
└────────────────────┘                └────────────────────────────────┘
```

Each run's outcome is itself a signal the *next* run sees. Two consecutive
`engine.run.failed` rows are now visible at `/admin/evolution/signals`.

## Default schedule

```toml
[[scheduler.jobs]]
name = "evolution_engine"
cron = "0 0 3 * * * *"   # 03:00:00 UTC daily, 7-field cron
action = { type = "subprocess", command = "corlinman-evolution-engine",
           args = ["run-once"], timeout_secs = 600, working_dir = "/data",
           env = { CORLINMAN_EVOLUTION_DB = "/data/evolution.sqlite",
                   CORLINMAN_KB_DB = "/data/kb.sqlite" } }
```

To shift the schedule, edit the `cron` field in your `corlinman.toml`. The
expression uses the 7-field grammar (`sec min hour day month weekday year`)
that `corlinman doctor`'s `scheduler` check already validates.

To pin different DB paths or pass extra environment variables to the
engine, add them under `env`. The map merges over the gateway's inherited
environment.

## Diagnostics

Two surfaces report engine-run health:

1. **Logs** — `/admin/logs?source=scheduler` shows the per-line stdout
   (info) and stderr (warn) forwarded from the child, tagged with the
   `job` and `run_id` fields. Spawn / timeout / non-zero exits log as
   `error` records.
2. **Signals** — `/admin/evolution/signals?event_kind=engine.run.failed`
   filters down to failed runs. Each row carries the `error_kind` (one of
   `exit_code`, `timeout`, `spawn_failed`, `unsupported_action`) and the
   `exit_code` (when applicable) in its JSON payload.

A failure path checklist:

- `error_kind = spawn_failed` — `corlinman-evolution-engine` is not on
  `$PATH`. In production this is the venv at `/opt/venv/bin/`; in dev
  invoke via `uv run` (see below).
- `error_kind = exit_code` — the engine itself raised. The stderr lines
  in `/admin/logs` show the Python traceback.
- `error_kind = timeout` — the engine ran past `timeout_secs` and was
  hard-killed. Either bump the timeout or look for an upstream RPC hang.

## Dev quickstart

In dev environments where the Python package is not installed into a
venv on `$PATH`, point the job's `command` at `uv` and use the
[`run`](https://docs.astral.sh/uv/reference/cli/#uv-run) subcommand:

```toml
action = { type = "subprocess", command = "uv",
           args = ["run", "--package", "corlinman-evolution-engine",
                   "corlinman-evolution-engine", "run-once"],
           timeout_secs = 600, working_dir = "<repo root>" }
```

In production the Docker image bakes `corlinman-evolution-engine` into
`/opt/venv/bin/`, which is already on `$PATH` for the gateway entrypoint;
the default config above works as-is.

## Shadow gating (W1-A)

> **Reality check (Python port).** Shadow gating is **NOT wired** in the
> current gateway. The `corlinman-shadow-tester` package exists and
> exposes `ShadowRunner` (`runner.py:87`), but it has **no console-script
> entrypoint, no CLI `main`, and is not registered as a scheduler job**
> by `corlinman-server`; the `corlinman-evolution-engine` never imports or
> calls `ShadowRunner` either (its handler docstrings claiming the
> ShadowTester "routes high-risk kinds through the shadow stage" overstate
> — the engine only emits proposals). No proposal is ever flipped to
> `shadow_done` at runtime. The section below describes the *intended*
> design; see ARCH_DEBT (evolution self-evolution spec) for the missing
> wiring. The cron block is a template, not an active job.

Phase 3 wave 1-A *was designed to* add a second scheduled subprocess — the
`corlinman-shadow-tester` runner — that runs 30 minutes after the
engine. The intent: anything the engine files as `pending` with
`risk = medium | high` is claimed, shadow-tested against an in-process
eval set, and flipped to `shadow_done` (with `shadow_metrics`,
`baseline_metrics_json`, and `eval_run_id` populated) before the
operator approval UI surfaces it; low-risk proposals bypass shadow
entirely and stay on the original `pending → approved` path. None of
this runs today (see the reality-check banner above).

```toml
[[scheduler.jobs]]
name = "shadow_tester"
cron = "0 30 3 * * * *"   # 03:30 UTC — 30 min after evolution_engine
action = { type = "subprocess", command = "corlinman-shadow-tester",
           args = ["run-once", "--config", "/data/config.toml"],
           timeout_secs = 600, working_dir = "/data",
           env = { CORLINMAN_DATA_DIR = "/data" } }
```

The binary reads the same `[evolution.observer].db_path` as the
gateway's `EvolutionObserver` (single source of truth for
`evolution.sqlite`) and resolves `kb.sqlite` as `<data_dir>/kb.sqlite`,
honouring `$CORLINMAN_DATA_DIR` first then falling back to
`[server].data_dir` — same precedence as the gateway.

**Master switch.** Shadow gating is off by default
(`[evolution.shadow].enabled = false`). Wiring the cron job alone does
nothing — the binary checks the flag at startup and exits non-zero with
a clear log line if it's still false. Flip the switch only once the
eval set under `[evolution.shadow].eval_set_dir` is authored (or you've
accepted the bundled `memory_op` cases). To stop shadow runs entirely,
either flip `enabled` back to false or drop the
`[scheduler.jobs.shadow_tester]` block; both are reversible without a
schema migration.

**Sandbox.** Phase 3 ships `sandbox_kind = "in_process"` only. `docker`
is reserved for Phase 4 (prompt / tool-policy kinds need stronger
isolation than in-process gives) and is rejected at startup until the
runner supports it.

## Shadow diagnostics

Verifying a run actually fired:

```sql
SELECT id, eval_run_id, status, shadow_metrics
  FROM evolution_proposals
 WHERE status = 'shadow_done'
 ORDER BY decided_at DESC
 LIMIT 10;
```

A row with a populated `eval_run_id` (format `eval-YYYY-MM-DD-<short>`)
and a non-null `shadow_metrics` blob means the binary claimed and
completed it. If a proposal sits in `shadow_running` for hours, the
runner crashed mid-case — check `/admin/logs?source=scheduler` for the
subprocess's tracing output.

`eval_run_id = no-eval-set` is the runner's "untested" sentinel: the
proposal was claimed and finished, but the per-kind subdir under
`eval_set_dir` was missing or empty. The operator UI renders that as a
distinct state so you don't confuse "shadow passed" with "shadow had
nothing to test against".

See [`docs/design/shadow-tester.md`](design/shadow-tester.md) for the
full design — eval-case schema, simulator trait contract, aggregation
shape, and the list of kinds queued for future waves.
