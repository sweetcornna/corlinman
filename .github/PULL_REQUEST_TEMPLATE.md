## Summary

<!-- What does this PR do, and why? Keep it to one or two sentences. -->

## Type

- [ ] feat: new user-facing behavior
- [ ] fix: bug fix or regression repair
- [ ] refactor: internal change with no intended behavior difference
- [ ] test: tests only
- [ ] perf: performance optimization
- [ ] docs: documentation only
- [ ] chore: tooling, dependencies, or maintenance
- [ ] ci: CI / workflow change

<!-- PR title must follow Conventional Commits: type(scope): concise change.
     scope = affected package/area, e.g. channels, gateway, providers, ui,
     marketplace, proto, docs (or finer: gateway/auth, admin/config, persona/ui). -->

## Scope / Packages Touched

<!-- Which package(s) or area(s)? e.g. corlinman-server/gateway, corlinman-channels, ui. -->

## Behavior Proof

<!-- Required for behavior changes. Attach the smallest convincing proof. -->

- [ ] I added/updated automated tests, or explained why tests are not practical.
- [ ] I included real behavior proof when relevant: screenshot, video, logs, curl output, or before/after notes.
- [ ] I labeled proof status when useful: `proof: supplied`, `proof: sufficient`, `proof: 📸 screenshot`, or `proof: 🎥 video`.

## CI Gate

The 7 jobs aggregated by `gate (all required checks)` must be green before merge:

- [ ] `py-ruff` — `uv run ruff check .`
- [ ] `py-mypy` — `uv run mypy python/packages/`
- [ ] `py-test` — `uv run pytest -m "not live_llm and not live_transport"`
- [ ] `ui-typecheck` — `pnpm -C ui typecheck`
- [ ] `ui-lint` — eslint over `ui/`
- [ ] `ui-test` — vitest over `ui/`
- [ ] `boundary-check` — `uv run lint-imports` (import-linter / `.importlinter`)

Separate checks (not in the `gate` aggregate — confirm independently):

- [ ] `proto-sync` — `bash scripts/gen-proto.sh`, regenerated stubs committed with no drift
- [ ] `swift-mac` — green if this PR touched `apps/swift-mac/**` or `.github/workflows/swift-mac.yml` (else N/A)

> ⚠️ **Known flaky:** `py-test` intermittently **hangs to the 6h CI cap**. This is a known infra issue that also affects `main`; the same tests pass locally on Python 3.12/3.13. It is **not your failure** — just **rerun the job**. A green gate may need a lucky rerun or an admin merge. Locally, run targeted tests with `uv run pytest <path>` instead of the whole suite.

## Module Boundaries / Proto

- [ ] No new reverse imports across the Python layering contract (`uv run lint-imports` passes locally).
- [ ] If I touched `proto/corlinman/v1/*.proto`, I ran `bash scripts/gen-proto.sh` and committed the regenerated `_generated/` stubs.

## Risk & Rollback

<!-- Name meaningful risk and how to roll back if the change breaks. -->

Risk labels to apply when relevant:
`merge-risk: 🚨 automation`, `merge-risk: 🚨 compatibility`, `merge-risk: 🚨 data-loss`, `merge-risk: 🚨 security-boundary`, `merge-risk: 🚨 other`.

Rollback plan:

## Ownership

<!-- There is no .github/CODEOWNERS file yet, so reviewers are NOT auto-requested.
     If this PR crosses an owner-area, manually request the relevant team using the
     area → owner map in docs/pr-standards.md §7. -->

## Codex Review

This repo runs an automatic Codex review on PR creation and after each push; status labels are applied automatically (see `.github/CODEX_REVIEW.md`).

- [ ] Ready for automatic Codex review.
- [ ] After follow-up commits, I will re-request with `@codex review` if the run is stale.
- [ ] I checked the newest Codex/bot comments before deciding PR status.

## Linked Issues

<!-- Example: Closes #123 -->

## Checklist

- [ ] PR title follows Conventional Commits.
- [ ] Tests added/updated; behavior proof attached for user-visible changes.
- [ ] Reviewers for every touched owner-area manually requested per `docs/pr-standards.md` §7 (no `.github/CODEOWNERS` exists yet).
- [ ] No `--no-verify` or hook-skipping used.
