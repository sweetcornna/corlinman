# PR Standards

This document defines how pull requests are opened, reviewed, gated, and routed to owners in **corlinman**. It complements [CONTRIBUTING.md](../CONTRIBUTING.md) (developer setup and workflow), the [pull request template](../.github/PULL_REQUEST_TEMPLATE.md), and the [Codex review flow](../.github/CODEX_REVIEW.md).

The repository is **all-Python** (a `uv`-managed workspace of 25 `python/packages/corlinman-*` packages, with `corlinman-server` the largest at ~85K LOC) plus a Node 20 / pnpm frontend in `ui/`. There is no other language plane.

## 1. PR Hygiene

- **One concern per PR.** Split large refactors into a structural PR followed by behavior PRs.
- **Conventional Commits title:** `type(scope): concise change`.
  - `type` ∈ `feat` | `fix` | `chore` | `docs`.
  - `scope` = the affected package/area, e.g. `channels`, `gateway`, `providers`, `ui`, `marketplace`, `proto`, `docs` — or finer, e.g. `gateway/auth`, `admin/config`, `persona/ui`, `telegram`. Run `git log --oneline -30` to confirm the in-repo style.
  - Subject is imperative present tense: `add X`, `fix Y`.
- **Behavior proof is required** for user-visible or UI-facing changes: tests, screenshots, video, logs, curl output, or before/after notes.
- **No drive-by formatting.** Touch only the code you are changing.

## 2. The Merge Gate

Branch protection requires a single aggregated check, `gate (all required checks)`, which fans in the following jobs from `.github/workflows/ci.yml`. **All must be green.**

| Job | Command |
| --- | --- |
| `py-ruff` | `uv run ruff check .` |
| `py-mypy` | `uv run mypy python/packages/` |
| `py-test` | `uv run pytest -m "not live_llm and not live_transport"` |
| `ui-typecheck` | `pnpm -C ui typecheck` |
| `ui-lint` | eslint over `ui/` |
| `ui-test` | vitest over `ui/` |
| `boundary-check` | `uv run lint-imports` (import-linter, config in `.importlinter`) |
| `proto-sync` | `bash scripts/gen-proto.sh`, then verify the generated stubs under `python/packages/corlinman-grpc/src/corlinman_grpc/_generated/` are committed with no drift |

Reproduce the gate locally before pushing:

```bash
uv sync --all-packages --dev
uv run ruff check .
uv run mypy python/packages/
uv run lint-imports
pnpm install --frozen-lockfile
pnpm -C ui typecheck
pnpm -C ui lint
pnpm -C ui test
```

For tests, prefer targeted runs (`uv run pytest <path>`) — see the caveat below.

## 3. Known Caveat: `py-test` Hangs to the 6h CI Cap

The `py-test` job **intermittently hangs and runs all the way to the 6-hour CI cap**. This is a **known flaky infrastructure issue** — it also affects `main`, and the same tests **pass locally on Python 3.12/3.13**.

Guidance for contributors and reviewers:

- **It is not your failure.** Do not start debugging your diff when only `py-test` hangs.
- **Rerun the job.** A green gate may simply need a lucky rerun.
- **Admin merge is acceptable** when reruns keep hanging and every other job is green — ping a maintainer.
- **Locally, run targeted tests** with `uv run pytest <path>` rather than the whole suite, so you get fast feedback without reproducing the hang.

## 4. Module Boundaries (`boundary-check`)

The Python core gRPC plane is layered (config in `.importlinter`), top-to-bottom:

```
corlinman_server   (top — gRPC entrypoint)
  └── corlinman_agent       (reasoning loop)
        └── corlinman_providers   (provider adapters)
              └── corlinman_grpc  (bottom — generated stubs + client base)
```

Higher layers may import lower layers; the reverse is forbidden, and same-layer peer packages must not import each other. Verify locally with `uv run lint-imports`. A small set of grandfathered upward imports is tracked in `.importlinter`'s `ignore_imports` — do not add new ones.

## 5. Proto Changes (`proto-sync`)

Generated gRPC stubs are committed under `python/packages/corlinman-grpc/src/corlinman_grpc/_generated/`. After editing `proto/corlinman/v1/*.proto`:

1. Run `bash scripts/gen-proto.sh` (requires `protobuf-compiler`; pinned `grpcio-tools` / `protobuf` versions in `pyproject.toml` keep output byte-identical across machines).
2. Commit the regenerated stubs **in the same PR**.

`proto-sync` fails on any drift between the committed stubs and a fresh generation. Backward-incompatible proto edits (changing a field type, deleting a field, renumbering) require an issue discussion first; additive fields are fine.

## 6. Codex Review Flow

Automatic Codex review runs on PR creation and again after each push. Status is reflected by labels applied automatically by the `PR status labels` workflow (`.github/workflows/pr-status-labels.yml`). Full procedure: [.github/CODEX_REVIEW.md](../.github/CODEX_REVIEW.md).

1. Open a focused PR with a clear `type(scope): concise change` title.
2. Attach behavior proof for user-visible changes.
3. Let automatic Codex review run.
4. If a review looks stale, comment `@codex review`.
5. Treat the Codex `eyes` reaction as acknowledgement; wait for the actual review comment or thumbs-up.
6. Classify PR status from the newest bot comments and evidence — never trust stale labels alone.

### Label vocabulary (auto-applied)

| Family | Labels |
| --- | --- |
| Codex | `codex:needs-review`, `codex:review-requested`, `codex:reviewed`, `codex:needs-rerun`, `codex:setup-issue` |
| Status | `status: 🔁 re-review loop`, `status: 🛠️ actively grinding`, `status: 📣 needs proof`, `status: 👀 ready for maintainer look`, `status: ⏳ waiting on author`, `status: ✅ merge-ready`, `status: 🚧 blocked` |
| Proof | `proof: missing`, `proof: supplied`, `proof: sufficient`, `proof: 📸 screenshot`, `proof: 🎥 video` |
| Risk | `merge-risk: 🚨 automation`, `merge-risk: 🚨 compatibility`, `merge-risk: 🚨 data-loss`, `merge-risk: 🚨 security-boundary`, `merge-risk: 🚨 other` |

Notable automation behavior (from `pr-status-labels.yml`):

- On `synchronize` (new push) the workflow adds `codex:needs-rerun` + `status: 🔁 re-review loop` and clears prior `codex:reviewed` / maintainer-look / merge-ready status.
- A `@codex review` comment re-requests review and re-enters the re-review loop.
- A Codex result that looks like a setup problem applies `codex:setup-issue` + `status: 🚧 blocked`; a normal result applies `codex:reviewed` + `status: 👀 ready for maintainer look`.
- Converting to draft applies `status: 🛠️ actively grinding`; merging applies `status: ✅ merge-ready`.

The workflow only calls GitHub's label API — it does not check out or execute PR code.

## 7. Ownership & CODEOWNERS Routing

Touching another team's area requires that team's review. Owner-areas map to real `python/packages/...` and `ui/` paths (never to any other language tree). The canonical mapping below mirrors the architecture module map ([docs/architecture-modules.md](architecture-modules.md)).

```text
# Core Gateway Orchestration
python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/           @corlinman/gateway-lead
python/packages/corlinman-server/src/corlinman_server/gateway/core/                @corlinman/gateway-lead
python/packages/corlinman-server/src/corlinman_server/gateway/middleware/          @corlinman/gateway-lead

# Voice & Chat Platform
python/packages/corlinman-server/src/corlinman_server/gateway/routes/              @corlinman/voice-chat-platform-team
python/packages/corlinman-server/src/corlinman_server/gateway/routes_voice/        @corlinman/voice-chat-platform-team
python/packages/corlinman-server/src/corlinman_server/gateway/oauth/               @corlinman/voice-chat-platform-team

# Admin Control-Plane
python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/      @corlinman/admin-control-plane-team

# Admin Backend
python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/      @corlinman/admin-backend-team

# Runtime Orchestration
python/packages/corlinman-server/src/corlinman_server/gateway/services/            @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/evolution/           @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/channels_runtime/    @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/grpc/                @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/providers/           @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/placeholder/         @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/mcp/                 @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway_api/                 @corlinman/runtime-orchestration-team
python/packages/corlinman-server/src/corlinman_server/gateway/observability/       @corlinman/runtime-orchestration-team

# Reasoning & Agent Tools
python/packages/corlinman-agent/                                                   @corlinman/reasoning-agent-team
python/packages/corlinman-agent-brain/                                             @corlinman/reasoning-agent-team

# Message Transports & Integrations
python/packages/corlinman-channels/                                                @corlinman/channels-gateway-team

# Provider Abstraction & Plugin Platform
python/packages/corlinman-providers/src/corlinman_providers/                       @corlinman/provider-adapters-team
python/packages/corlinman-providers/src/corlinman_providers/plugins/               @corlinman/plugin-platform-team

# Data & Knowledge Layer
python/packages/corlinman-memory-host/                                             @corlinman/memory-backend-team
python/packages/corlinman-episodes/                                                @corlinman/memory-backend-team
python/packages/corlinman-tagmemo/                                                 @corlinman/memory-backend-team
python/packages/corlinman-user-model/                                              @corlinman/memory-backend-team
python/packages/corlinman-replay/                                                  @corlinman/memory-backend-team
python/packages/corlinman-evolution-engine/                                        @corlinman/evolution-engine-team
python/packages/corlinman-evolution-store/                                         @corlinman/evolution-engine-team
python/packages/corlinman-shadow-tester/                                           @corlinman/evolution-engine-team
python/packages/corlinman-auto-rollback/                                           @corlinman/evolution-engine-team
python/packages/corlinman-goals/                                                   @corlinman/goals-intelligence-team

# Platform Services
python/packages/corlinman-server/src/corlinman_server/scheduler/                   @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/tenancy/                     @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/persona/                     @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/profiles/                    @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/cli/                         @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/tools/                       @corlinman/platform-services-team
python/packages/corlinman-server/src/corlinman_server/bundled_skills/              @corlinman/platform-services-team

# Foundation Infrastructure
python/packages/corlinman-persona/                                                 @corlinman/foundation-infrastructure-team
python/packages/corlinman-identity/                                                @corlinman/foundation-infrastructure-team
python/packages/corlinman-grpc/                                                    @corlinman/foundation-infrastructure-team
python/packages/corlinman-wstool/                                                  @corlinman/foundation-infrastructure-team
python/packages/corlinman-nodebridge/                                              @corlinman/foundation-infrastructure-team
python/packages/corlinman-skills-registry/                                         @corlinman/foundation-infrastructure-team
python/packages/corlinman-subagent/                                                @corlinman/foundation-infrastructure-team
python/packages/corlinman-hooks/                                                   @corlinman/foundation-infrastructure-team
python/packages/corlinman-mcp-server/                                              @corlinman/foundation-infrastructure-team
python/packages/corlinman-canvas/                                                  @corlinman/foundation-infrastructure-team

# System Integration
python/packages/corlinman-server/src/corlinman_server/system/                      @corlinman/system-integration-team

# Frontend
ui/                                                                                @corlinman/voice-chat-platform-team
```

> This block is a **proposal** mirroring the module map. To activate it, copy it into a real `.github/CODEOWNERS` file and replace the placeholder `@corlinman/<area>` handles with actual GitHub teams.

### Cross-team seams (need approval from both owner-areas)

- **AdminState contract** — `@corlinman/gateway-lead` owns the AdminState dataclass; admin-control-plane and admin-backend negotiate new fields via gateway-lead review.
- **Provider registry** — runtime-orchestration owns provider→AppState attachment; provider-adapters and voice-chat-platform sign off on `ProviderRegistry` interface changes.
- **Memory & evolution** — memory-backend owns the `MemoryHost` interface; reasoning-agent (memory tools) and evolution-engine (memory_op handler) both depend on it.
- **Scheduler builtins** — platform-services owns scheduler lifecycle; admin-backend passes job specs via explicit AdminState wiring.

## 8. Merge Checklist

- [ ] All 8 gate jobs green (`py-test` flakiness handled per §3).
- [ ] Conventional Commits title.
- [ ] Tests added/updated; behavior proof attached for user-visible changes.
- [ ] `uv run lint-imports` passes — no new reverse imports.
- [ ] Proto stubs regenerated and committed if `*.proto` changed.
- [ ] CODEOWNERS for every touched owner-area have approved; cross-team seams signed off by both areas.
- [ ] Codex review passed or freshly requested; PR status labels reflect the current head.
