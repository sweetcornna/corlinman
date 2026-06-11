# NapCat Managed Adapter Design

## Goal

Make NapCat a first-class corlinman managed subsystem while keeping user-provided NapCat deployments supported. Operators should no longer see vague failures such as "cannot connect" or "QR code cannot refresh"; the gateway should either repair the problem automatically or report the exact failed layer.

## Scope

This design covers the QQ/NapCat admin runtime path:

- NapCat WebUI/API URL and token resolution.
- QR login and QR refresh reliability.
- WebUI credential injection compatibility.
- Gateway-owned `/webui` and narrow NapCat WebUI API proxying.
- OneBot v11 websocket server repair.
- Admin diagnostics for managed and external NapCat deployments.
- QQ channel config editing for NapCat endpoint fields.

This design does not vendor NapCat source code into this repository. corlinman manages NapCat as a supervised runtime dependency in Docker/native installs and adapts to external NapCat through the same gateway adapter.

## Architecture

Introduce a gateway-owned NapCat adapter boundary in `routes_admin_b/_napcat_lib.py`. Routes call the adapter for QR, login status, credential, OneBot repair, and diagnostics rather than encoding assumptions in UI or deployment docs.

NapCat runs in one of two modes:

- `managed`: corlinman default Docker/native NapCat, resolved from environment or the loopback default.
- `external`: user-provided NapCat, resolved from `[channels.qq].napcat_url`.

Both modes use the same HTTP/WebUI API contract. The difference is how diagnostics phrase corrective actions: managed mode can tell the operator to restart/redeploy corlinman-managed NapCat, while external mode tells them which externally owned URL/token/proxy layer is failing.

## Data Flow

1. Admin opens QQ page.
2. QQ status returns normal channel state plus non-secret config keys including `ws_url` and `napcat_url`.
3. Scan-login dialog fetches `/admin/channels/qq/napcat/diagnostics`.
4. Gateway probes:
   - resolved URL and source,
   - WebUI token presence and credential exchange,
   - QR API reachability,
   - OneBot config API reachability.
5. Gateway returns structured `issues[]` and `actions[]`.
6. The iframe loads gateway-owned `/webui`, which proxies to the resolved NapCat WebUI.
7. NapCat WebUI calls to `/api/QQLogin/*`, `/api/OB11Config/*`, and `/api/auth/*` stay in the gateway's narrow NapCat proxy. `RefreshQRcode` keeps the robust gateway refresh path.
8. Operators can see whether failure is URL, auth, API mismatch, login state, or OneBot config drift.

## Error Handling

The adapter keeps existing typed `NapcatError` behavior for QR/login routes. Diagnostics never raise on probe failures; they return status strings and actions so the UI remains usable during outages.

QR refresh keeps the current robust path:

- capture old QR,
- call NapCat refresh,
- poll until QR changes,
- restart NapCat through NapCat API when refresh is a no-op,
- clear cached credential and poll for a new QR.

## Config

QQ config editing must expose:

- secret fields: `access_token`, `napcat_access_token`
- URL fields: `ws_url`, `napcat_url`
- ID fields: `self_ids`

Secrets are never echoed. Non-secret `ws_url`, `napcat_url`, and `self_ids` are returned through status/config payloads so the UI can seed editors.

## Testing

Backend tests cover:

- default managed URL resolution,
- explicit external URL classification,
- diagnostics for reachable/authenticated NapCat,
- diagnostics for unreachable NapCat,
- diagnostics for missing WebUI token,
- QQ config write accepting `napcat_url` and `napcat_access_token`.

Frontend tests cover:

- QQ channel config spec includes NapCat URL/token fields,
- scan-login dialog fetches and renders diagnostics when opened,
- existing iframe behavior remains intact.

Gateway proxy tests cover:

- `/webui` is handled by the NapCat router instead of falling through to static UI.
- NapCat WebUI API prefixes proxy to NapCat without adding a catch-all `/api/*` route that would steal corlinman's own API paths.

## Verification

Run targeted Python tests for NapCat/channel admin and targeted Vitest tests for QQ/channel config. Then run a broader status check over changed files with `git diff --check`.
