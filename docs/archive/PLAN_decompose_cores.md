# Decomposition plan — remaining risky cores (`entrypoint.py` / `auth.py`)

> Status: **✅ EXECUTED — shipped in v1.17.0.** Produced 2026-06-03 by a deep
> multi-agent read-only analysis (6 analysis agents + 2 synthesis agents), then
> executed 2026-06-03/04 across 6 reviewed phases. Outcome: `auth.py` 931 → 773
> (mechanical layer → `_auth_lib`, all security logic kept); `entrypoint.py`
> 3680 → 1769 via `cli_helpers` / `bootstrap_constants` / `config_loading` /
> `app_factory` / `c2_wiring` (the `lifespan` closure is the irreducible
> residual). Including the in-`build_app` middleware/UI-static extraction whose
> registration order was proven byte-identical via `app.user_middleware`. The
> per-phase plan below is retained as the executed record. Every phase used the
> proven *extract-and-reimport* pattern (move a cohesive group verbatim into a
> sibling module, re-import the names so the public surface + `__all__` + all
> external importers stay byte-for-byte; the sibling never imports its source
> module → no cycle).

These two files were deliberately excluded from the 6 parallel split waves
(20 god-files, merged through `8ccc6fa`) because they are the boot core and
the security core. This plan makes their decomposition concrete and phased.

---

## A. `routes_admin_a/auth.py` (931 LOC) — **GO (conservative)**

**Decision: GO.** Move only the lowest-risk mechanical layer (wire-models +
pure constants + the stateless rate-limiter + pure format/error helpers) into a
sibling `_auth_lib.py`. **All security logic stays in `auth.py`.** Precedent:
`routes_admin_a/_sessions_lib.py` already uses this exact pattern.

### STAYS in `auth.py` (security core — never move)
- Password crypto: `_HASHER`, `_DUMMY_PASSWORD_HASH` (timing-oracle defense), `hash_password`, `argon2_verify`
- Session/cookie/TLS: `_ensure_session_store`, `_read_session_cookie`, `_set_cookie_header`, `_clear_cookie_header`, `_session_cookie_secure`, `_request_is_https`, `_trusted_forwarded_proto_cidrs`, `_request_from_trusted_forwarded_proto_proxy`, `_remote_ip`
- Throttle: `_login_failure_store` (accessor)
- Persistence/locks: `_atomic_write`, `_persist_admin_credentials`, `_toml_escape`*, `_rename_active_session`, `_FALLBACK_ADMIN_WRITE_LOCK`, `_LockAsyncCM`, `_lock_async`
- `router()` + all 6 handlers (`login`/`logout`/`me`/`onboard`/`change_password`/`change_username`) — byte-for-byte

### MOVE to `_auth_lib.py` (~19 symbols, ~290 LOC)
- Wire-models (6): `LoginRequest`, `LoginResponse`, `MeResponse`, `OnboardRequest`, `ChangePasswordRequest`, `ChangeUsernameRequest`
- Constants (6): `MIN_PASSWORD_LEN`, `USERNAME_MAX_LEN`, `_USERNAME_RE`, `DEFAULT_SESSION_TTL_SECS`, `LOGIN_FAILURE_LIMIT`, `LOGIN_FAILURE_WINDOW_SECONDS`
- Stateless rate-limiter: `AdminLoginFailureStore`
- Pure helpers (6): `_iso`, `_client_ip`, `_too_many_login_attempts`, `_service_unavailable`, `_unauthorized`, and `_toml_escape` *(borderline — only if grep confirms it's a pure string escaper with no external patch; otherwise leave it with `_persist_admin_credentials`)*

### Re-export & tests
- Re-import all moved names into `auth.py`; keep `auth.__all__` **identical** (12 names).
- External importers (`cli/init`, `admin_seed`, `onboard`, `_auth_shim`, `password_reset`, `__init__`, ~15 test files) all import `hash_password`/`router`/wire-models — **all stay importable; zero external edits.**
- **Monkeypatch repoints: none.** Tests patch `argon2_verify` / `hmac.compare_digest` — both stay in `auth.py`; the handlers that read them stay too.

### Risk: **LOW (★★☆☆☆).** Post-split `auth.py` ≈ 709 LOC, `_auth_lib.py` ≈ 290.
### Validation
```
uv run ruff check <auth.py> <_auth_lib.py>
uv run mypy python/packages/corlinman-server/src
uv run python -c "import corlinman_server.gateway.routes_admin_a.auth as a; \
  from corlinman_server.gateway.routes_admin_a import _auth_lib; \
  assert set(a.__all__) >= {'hash_password','router','LoginRequest'}; print('ok')"
uv run lint-imports
uv run pytest python/packages/corlinman-server/tests/gateway/routes_admin_a \
  python/packages/corlinman-server/tests/gateway/lifecycle/test_admin_seed.py -q
# boot smoke: import entrypoint + build_router(admin_a/b)
```

---

## B. `lifecycle/entrypoint.py` (3679 LOC) — phased, mostly safe

**Key insight:** ~1600 LOC of `entrypoint.py` are **module-level helper
functions defined OUTSIDE `build_app`** (lines 119–1718). Moving those is the
exact proven pattern (low risk). `build_app` itself (1720–3495, 1776 LOC) is
the core; only the *in-function block* extractions (CORS / middleware / UI
mount, ~285 LOC) touch it and are the riskier subset.

| Phase | New sibling | Moves (module-level unless noted) | ~LOC | Risk | Self-merge |
|---|---|---|---|---|---|
| 1 | `cli_helpers.py` | `_lazy_import`, `_resolve_config_path`, `_resolve_data_dir`, `_should_run_legacy_migration`, `_tenant_scope_params`, `_build_parser`, `_resolve_bind` | 214 | LOW | yes |
| 2 | `bootstrap_constants.py` | `DEFAULT_HOST/PORT`, `SIGTERM_EXIT_CODE`, `RESTART_REQUIRED_SECTIONS_LOCAL`, `_emit_py_config_drop`, `list_default_scheduler_jobs`, `_IDENTITY_SWEEP_INTERVAL_SECS`, `_identity_sweep_loop` | 105 | LOW | yes |
| 3 | `config_loading.py` | `_load_config`, `_wire_status_links`, `_config_hot_reload_enabled`, `_reapply_hot_reloadable`, `_start_config_watcher` (+ CORS inline → `_install_cors_middleware`) | 397 | MED | yes (ruff/tests green) |
| 4 | `app_factory.py` | `_build_state`, `_repo_agents_dir`, `_make_channels_writer`, `_make_config_swap_fn`, `_build_agent_registry_stack`, `_DegradedAppState`, `_mount_routes` (+ 3 middleware inline blocks → helpers) | ~640 | MED-HIGH | yes + review middleware blocks |
| 5 | `c2_wiring.py` | `_wire_c2_handles`, `_wire_plugin_hotload` (+ UI-static inline → `_mount_ui_static`) | ~275 | HIGH | **human review** |
| — | (reserved) | the `lifespan` closure (~1376 LOC) captures 12+ shared locals, 25+ best-effort try/except, ordered teardown | — | IRREDUCIBLE | do not extract |

**Recommended order of safety:** Phases 1 → 2 are pure module-level moves
(safe self-merge, same as the prior 20 splits). Phase 3 is mostly module-level
+ one in-function CORS block. Phase 4 is module-level builders + 3 in-function
middleware blocks (do the module-level part first; the inline-block extraction
is a separate, reviewed step). **Phase 5 needs human review** (async store
init, scheduler `app.state` threading, lifespan teardown order).

**Floor:** after phases 1–4 (module-level portions), `entrypoint.py` ≈ 2200–2400
LOC with `build_app` + `lifespan` intact; pushing to ~1500 requires the
in-function block extractions (riskier) and is optional.

### Per-phase validation (each phase, before merge)
```
uv run ruff check . && uv run mypy python/packages/corlinman-server/src && uv run lint-imports
# boot smoke + build_app construction:
uv run python -c "from corlinman_server.gateway.lifecycle.entrypoint import build_app; \
  import tempfile; from pathlib import Path; \
  app = build_app(config_path=None, data_dir=Path(tempfile.mkdtemp())); \
  from fastapi.testclient import TestClient; assert TestClient(app).get('/health').status_code==200; print('boot ok')"
uv run pytest python/packages/corlinman-server/tests/gateway/lifecycle -q
```

---

## Recommended execution sequence

1. **auth.py split** (one PR, LOW risk) — self-contained, highest value/risk ratio.
2. **entrypoint Phase 1 + 2** (one PR each, or stacked) — pure module-level moves, LOW risk.
3. **entrypoint Phase 3** — module-level + CORS; verify config-hot-reload tests.
4. **entrypoint Phase 4 (module-level builders only)** — defer the 3 in-function middleware blocks to a separate reviewed step.
5. **STOP for review** before Phase 4 inline-blocks / Phase 5 (boot-order-critical).

Each step: branch off `main` → extract-and-reimport → validate (above) → FF-merge → next. Run a dedicated adversarial read-only review on the auth split and on any in-function (build_app) extraction.
