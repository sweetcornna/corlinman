# NapCat Managed Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make NapCat a managed corlinman subsystem with diagnostics and config controls that also support user-provided NapCat deployments.

**Architecture:** Add structured diagnostics and mode detection to the existing `_NapcatClient` adapter layer. Surface NapCat endpoint fields through the existing channel config API, and show diagnostics in the QQ scan-login dialog without replacing the existing WebUI iframe.

**Tech Stack:** FastAPI, Pydantic, pytest, Next.js/React, Vitest, React Testing Library.

---

## File Structure

- Modify `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/_napcat_lib.py`: adapter models, endpoint resolution metadata, diagnostics probe.
- Modify `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/napcat.py`: diagnostics route.
- Modify `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/napcat.py`: gateway-owned `/webui` and narrow NapCat API proxy routes.
- Modify `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/_channels_lib.py`: QQ config edit spec and non-secret config projection.
- Modify `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/channels.py`: include QQ `config_keys` in status.
- Modify backend tests under `python/packages/corlinman-server/tests/gateway/routes_admin_b/` and `routes_admin_a/`.
- Modify `ui/lib/api.ts`: NapCat diagnostics client types and fetcher.
- Modify `ui/lib/api/channel-config.ts`: QQ editable fields.
- Modify `ui/app/(admin)/channels/qq/ScanLoginDialog.tsx`: fetch/display diagnostics.
- Modify `ui/app/(admin)/channels/qq/page.tsx`: mount `ChannelConfigEditor` for QQ.
- Modify Vitest tests for QQ dialog and channel config.
- Modify `docs/runbook.md`: managed/external NapCat diagnostics guidance.

## Task 1: Backend Diagnostics Contract

**Files:**
- Test: `python/packages/corlinman-server/tests/gateway/routes_admin_b/test_napcat_diagnostics.py`
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/_napcat_lib.py`
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/napcat.py`

- [x] **Step 1: Write failing diagnostics tests**

Add tests that instantiate fake NapCat clients and assert:

```python
assert out.mode == "managed"
assert out.url_source == "default"
assert out.credential == "missing_token"
assert "napcat_webui_token_missing" in out.issues
```

and:

```python
assert out.mode == "external"
assert out.url == "http://user-napcat:6099"
assert out.credential == "ok"
assert out.qrcode_api == "ok"
assert out.onebot_config_api == "ok"
assert out.issues == []
```

- [x] **Step 2: Run backend diagnostics tests and verify RED**

Run:

```bash
uv run pytest python/packages/corlinman-server/tests/gateway/routes_admin_b/test_napcat_diagnostics.py -q
```

Expected: fail because diagnostics models/functions/routes do not exist.

- [x] **Step 3: Implement diagnostics models and probe**

Add:

```python
class NapcatDiagnosticsOut(BaseModel):
    mode: str
    url: str | None
    url_source: str
    managed: bool
    auth_configured: bool
    credential: str
    qrcode_api: str
    onebot_config_api: str
    issues: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
```

Add endpoint metadata resolution and an async probe that returns this model without raising.

- [x] **Step 4: Add route**

Add `GET /admin/channels/qq/napcat/diagnostics` in `napcat.py`, protected by existing admin dependency.

- [x] **Step 5: Run backend diagnostics tests and verify GREEN**

Run the same uv run pytest command. Expected: pass.

## Task 2: QQ Config Editable NapCat Fields

**Files:**
- Test: `python/packages/corlinman-server/tests/gateway/routes_admin_a/test_qq_napcat_config_edit.py`
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/_channels_lib.py`
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_a/channels.py`

- [x] **Step 1: Write failing config tests**

Assert `PUT /admin/channels/qq/config` accepts:

```json
{
  "secrets": {"napcat_access_token": "webui-token"},
  "urls": {"napcat_url": "http://user-napcat:6099"}
}
```

and returns non-secret `config_keys.napcat_url`.

- [x] **Step 2: Run config tests and verify RED**

Run:

```bash
uv run pytest python/packages/corlinman-server/tests/gateway/routes_admin_a/test_qq_napcat_config_edit.py -q
```

Expected: fail with `unknown_field` or missing `config_keys`.

- [x] **Step 3: Implement config spec changes**

Update QQ editable spec:

```python
"secret_keys": ["access_token", "napcat_access_token"],
"url_keys": ["ws_url", "napcat_url"],
"int_list_keys": ["self_ids"],
```

Return QQ `config_keys` from status.

- [x] **Step 4: Run config tests and verify GREEN**

Run the same uv run pytest command. Expected: pass.

## Task 3: Gateway-Owned NapCat WebUI Proxy

**Files:**
- Test: `python/packages/corlinman-server/tests/gateway/routes_admin_b/test_napcat_webui_proxy.py`
- Modify: `python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/napcat.py`

- [x] **Step 1: Write failing WebUI proxy tests**

Assert `/webui` proxies to upstream `/`, and `/api/QQLogin/GetQQLoginQrcode` proxies to the exact NapCat path without adding a generic `/api/*` catch-all.

- [x] **Step 2: Run proxy tests and verify RED**

Run:

```bash
uv run pytest python/packages/corlinman-server/tests/gateway/routes_admin_b/test_napcat_webui_proxy.py -q
```

Expected: fail because `_proxy_napcat_request` and proxy routes do not exist.

- [x] **Step 3: Implement narrow proxy routes**

Add admin-gated proxy routes for `/webui`, `/webui/{path:path}`, `/api/QQLogin/{path:path}`, `/api/OB11Config/{path:path}`, and `/api/auth/{path:path}`. Keep the exact robust `/api/QQLogin/RefreshQRcode` route before the generic QQLogin proxy.

- [x] **Step 4: Run proxy tests and verify GREEN**

Run the same uv run pytest command. Expected: pass.

## Task 4: Frontend Diagnostics and Config Controls

**Files:**
- Test: `ui/lib/api/channel-config.test.ts`
- Test: `ui/app/(admin)/channels/qq/ScanLoginDialog.test.tsx`
- Modify: `ui/lib/api.ts`
- Modify: `ui/lib/api/channel-config.ts`
- Modify: `ui/app/(admin)/channels/qq/ScanLoginDialog.tsx`
- Modify: `ui/app/(admin)/channels/qq/page.tsx`

- [x] **Step 1: Write failing frontend tests**

Assert the QQ config spec includes `napcat_access_token` and `napcat_url`. Assert opening the scan-login dialog fetches `/admin/channels/qq/napcat/diagnostics` and renders the returned mode/status.

- [x] **Step 2: Run frontend tests and verify RED**

Run:

```bash
cd ui && pnpm vitest run lib/api/channel-config.test.ts app/'(admin)'/channels/qq/ScanLoginDialog.test.tsx
```

Expected: fail because fields/client/rendering do not exist.

- [x] **Step 3: Implement frontend client and UI**

Add diagnostics types/fetcher, update QQ config spec, show diagnostics above the iframe, and mount `ChannelConfigEditor` on the QQ page using `status.data.config_keys`.

- [x] **Step 4: Run frontend tests and verify GREEN**

Run the same Vitest command. Expected: pass.

## Task 5: Documentation and Verification

**Files:**
- Modify: `docs/runbook.md`

- [x] **Step 1: Update runbook**

Document managed vs external NapCat, diagnostics endpoint, and the meaning of URL/auth/QR/OneBot failures.

- [x] **Step 2: Run final targeted verification**

Run:

```bash
uv run pytest python/packages/corlinman-server/tests/gateway/routes_admin_b/test_napcat_diagnostics.py python/packages/corlinman-server/tests/gateway/routes_admin_b/test_napcat_webui_proxy.py python/packages/corlinman-server/tests/gateway/routes_admin_b/test_napcat_qrcode_refresh.py python/packages/corlinman-server/tests/gateway/routes_admin_a/test_qq_napcat_config_edit.py python/packages/corlinman-server/tests/gateway/routes_admin_a/test_qq_napcat_onebot_ensure.py -q
cd ui && pnpm vitest run lib/api/channel-config.test.ts app/'(admin)'/channels/qq/ScanLoginDialog.test.tsx
git diff --check
```

Expected: all commands exit 0.
