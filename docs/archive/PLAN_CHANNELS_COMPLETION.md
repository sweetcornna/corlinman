# PLAN — Channels Completion (frontend access, QQ completeness, NapCat embed, surface all channels)

Status: draft for execution. Author: lead engineer synthesis from 5 parallel mapper reports + direct
codebase verification (2026-05-29).

This plan fixes five reported problems:

1. Telegram and QQ channel admin pages "cannot be accessed".
2. QQ channel functionality is incomplete.
3. The installer does not bundle + embed + configure NapCat for QQ.
4. Many already-built channels are not surfaced in the frontend.
5. Make ALL channels functionally complete.

---

## 0. CRITICAL CORRECTION to the R1 root-cause report (read this first)

R1 claimed the Telegram/QQ pages 404 because the `(admin)/layout.tsx` `getSession()` guard
makes the static exporter bake a 404 into `out/channels/{qq,telegram}.html` (the `notFound:[[...]]`
RSC marker).

**This hypothesis is contradicted by direct evidence and must NOT be implemented as written.**
Verification performed against the repo:

- `out/channels/qq.html` and `out/channels/telegram.html` are **structurally identical** to working
  pages like `out/config.html` and `out/models.html`. All of them are `"use client"` pages under the
  same `(admin)/layout.tsx` guard.
- The `notFound` token count is comparable across all pages (qq.txt = 4, config.txt = 3). It is the
  standard not-found RSC segment that Next emits into **every** client-rendered page's payload — it is
  NOT a "this page is a 404" marker.
- `out/channels/qq.txt` references its real module chunk `app/(admin)/channels/...` exactly as
  `out/config.txt` references `app/(admin)/config`. The page module is wired, not replaced by 404.
- `cmp out/channels/qq.html out/404.html` → **DIFFERENT** (diverge at byte 2104). qq.html is not the
  404 page.
- The build is fresh: `out/channels/qq.html` (May 28) is newer than `page.tsx` source (May 26).
- i18n keys exist (`en.ts:382-383`, `zh-CN.ts:367-368`); sidebar group is in `OPERATOR_ITEMS`
  (always-visible, not dev-gated); all QQ child components and `lib/api.ts` exports
  (`fetchQqStatus`, `reconnectQq`, `updateQqKeywords`, `QqStatus`) resolve.

Because the pages are `output: "export"` client shells, the static `.html` is intentionally a skeleton
that hydrates the real page client-side after auth resolves. config.html does the same and works in the
browser, so qq.html will too once served. **The build is not the problem.**

### The actual most-likely root cause: the static file resolver / serving layer

The gateway serves the UI via `_NextStaticFiles` in
`python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/entrypoint.py:2469-2503`.
For a request path `channels/qq` (no trailing slash, leaf `qq` has no dot) it tries
`channels/qq.html` first, then falls back to `404.html`. The flat-file emit
(`out/channels/qq.html`, not `channels/qq/index.html`) matches this resolver.

Genuine failure candidates, in priority order, that produce a real browser 404/"cannot access":

- **(A) Stale `ui-static` on the running deploy.** Native prod (43.133.12.98, systemd) and docker
  both serve from a baked `CORLINMAN_UI_DIR` (`/app/ui-static` docker; `$PREFIX/ui-static` native via
  `install.sh:305-307` rsync). If the deployed bundle predates the channel pages (pages added Apr 22,
  but a deploy may have shipped an older `out/`), `channels/qq.html` is simply absent on disk → resolver
  falls to `404.html`. This is the single most likely cause for "cannot be accessed" in production.
- **(B) The resolver's `response.status_code != 404` guard** (entrypoint.py:2482) — Starlette
  `StaticFiles` returns a 200 `FileResponse` for an existing file, so this is fine for present files;
  but if a reverse proxy/CDN cached a 404 body for `/channels/qq` during a deploy gap (the exact
  scenario `assetPrefix` in `next.config.ts:14-21` was added to defeat), the browser keeps getting 404.
- **(C) Client-side runtime crash post-hydration** — if any page import throws at runtime the
  `PageErrorBoundary` (layout.tsx:156) renders an error, which a user may describe as "can't access".
  Verified imports all resolve, so this is low probability but must be ruled out by a live smoke test.

### Exact fix for problem (1)

Do NOT refactor the admin layout to a server component (R1 Solution A) — it would not change the symptom
and risks breaking every other admin page. Instead:

1. **Rebuild + redeploy the UI bundle** so `ui-static` contains current `channels/{qq,telegram}.html`.
   - Native: `install.sh` step at lines 298-308 (`pnpm build` → rsync `ui/out/` → `$PREFIX/ui-static/`).
   - Docker: rebuild the `ui-builder` stage (Dockerfile:135 copies `ui/out` → `/app/ui-static`).
2. **Add a deterministic E2E/serving assertion** (new) so this regression is caught:
   - New script `ui/scripts/assert-routes-built.mjs` (or a pytest) that, after `pnpm build`, asserts the
     existence of `out/channels/qq.html`, `out/channels/telegram.html`, and every channel page added in
     §4, and asserts each file is NOT byte-identical to `out/404.html`.
   - Wire it into the `ui-build` CI gate.
3. **Add a backend serving test** in
   `python/packages/corlinman-server/tests/` that boots the gateway with `CORLINMAN_UI_DIR` pointed at a
   fixture dir containing `channels/qq.html`, requests `GET /channels/qq`, and asserts 200 + the file
   body (locks the `_NextStaticFiles` nested-route resolution behaviour).
4. **Live smoke test** (manual, post-deploy): `curl -sS https://<host>/channels/qq | head` and confirm
   it returns the qq shell, not `404.html`; then load in a browser logged-in to confirm hydration.

If, after redeploy, the page still 404s in the browser despite `channels/qq.html` being present and
served (verified by curl), THEN and only then investigate the layout/SSR angle — but the evidence says
this will resolve at the deploy/serving layer.

---

## 1. Backend channel admin API — current state (verified)

Routers split across `routes_admin_a` and `routes_admin_b`:

- **QQ/OneBot** (`routes_admin_a/channels.py`): `GET /admin/channels/qq/status`,
  `POST /admin/channels/qq/reconnect` (501 stub), `POST /admin/channels/qq/keywords`.
- **QQ NapCat** (`routes_admin_b/napcat.py`): `POST /admin/channels/qq/qrcode`,
  `GET /admin/channels/qq/qrcode/status`, `GET /admin/channels/qq/accounts`,
  `POST /admin/channels/qq/quick-login`. URL resolution at `napcat.py:112-130`
  (`[channels.qq].napcat_url` → `CORLINMAN_NAPCAT_URL` env → 503 not-configured).
- **Telegram** (`routes_admin_a/channels.py`): `GET /status`, `GET /messages`, `POST /send`.
- **CorlinmanChannel** (`routes_admin_b/corlinman_channel.py`): `/api/channels/corlinman/*`.
- **No admin endpoints at all** for: discord, slack, feishu, wechat_official, qq_official — all are
  bootstrapped at runtime by `channels_runtime/__init__.py:478-732` but have zero HTTP admin surface.

`AdminState` (`routes_admin_a/state.py:106-127`) parks `channels_config` (dict), `channels_writer`
(callback), and `telegram_sender` (live instance for test-sends). The pattern to replicate for new
channels: park a live sender/client + a health snapshot on `AdminState` when the channel boots.

---

## 2. QQ channel completion

Verified nuance vs R3: OneBot's wire layer is MORE capable than R3 implied. `SendGroupMsg`/
`SendPrivateMsg` already carry `message: list[MessageSegment]`, and `_segment_to_wire` (onebot.py:91-101)
already serializes `image` segments. The real gap is that the **handler** `_build_qq_action`
(`service.py:1435-1459`) only ever appends `TextSegment` — it never constructs an `ImageSegment` for
outbound replies. So inline media send is a handler-level change, not a protocol change.

### 2a. Backend — `corlinman-channels` (onebot.py + service.py)

- **Inline image send** (`service.py`, `_build_qq_action` 1435-1459): when the agent reply carries an
  image/emoji attachment, append `ImageSegment(url=...)` (or `file=`) to the segment list alongside the
  text segment, for both group and private paths. The wire serialization already exists.
- **Document/file send**: keep `UploadGroupFile`/`UploadPrivateFile` (onebot.py:436-466) for true file
  shares; route attachment outbound (`_qq_send_attachment` service.py:971-1006) is already wired — verify
  it handles both image (inline) and non-image (upload) by mime.
- **Inbound media coverage** (`segments_to_attachments` onebot.py:343-367): currently extracts image +
  record (audio). Add video + file segment extraction so QQ inbound matches Telegram's photo/voice/doc.
- **Command handlers** (R3): wire the shared `commands.COMMAND_REGISTRY` `handler=` direct-return path
  into `handle_one_qq` (mirror `handle_one_telegram`) so `/help`, `/whoami`, `/status` return without an
  LLM round-trip. Currently QQ only has the `wizard_prelude` (LLM) path.
- **Health/status parity**: `_qq_health_watcher` (service.py:401-528) already tracks NapCat heartbeat +
  account online/offline into `QQ_HEALTH`. Add per-send success/failure counters + send-queue depth so
  the status endpoint can report delivery health (Telegram-like).
- **Reconnect**: replace the `reconnect` 501 stub (`routes_admin_a/channels.py`) with a real action — set
  a reconnect flag the OneBot adapter loop honours, or close+reopen the WS. If genuinely infeasible with
  forward-WS, keep 501 but document why (OneBot adapter dials `QQ_WS_URL`, reconnect = bounce NapCat).
- **Keep** the tool-summary prelude behaviour (CORLINMAN_QQ_TOOL_SUMMARY) — it's the correct fallback
  for a non-editable channel. Document the env var in CLI help + config example.

### 2b. NapCat REST admin (`routes_admin_b/napcat.py`)

- The QR/scan-login/accounts/quick-login flow is implemented. After §3 (embedding), ensure
  `_resolve_napcat_url` (112-130) resolves successfully in BOTH docker (`http://napcat:6099`) and native
  (`http://127.0.0.1:6099`) so the scan-login UI is not 503 in either mode.
- Add a `GET /admin/channels/qq/napcat/health` (or fold into `/status`) returning NapCat webui
  reachability + logged-in account, so the UI can distinguish "NapCat down" from "QQ not logged in".

### 2c. QQ UI

The QQ page (`ui/app/(admin)/channels/qq/page.tsx`) + child components + `ScanLoginDialog` already exist
and are feature-complete (hero, stats, account panel, filters/keywords, messages, scan-login). After 2a:

- Surface the new health counters (send success/fail, queue depth) in `QqStatsRow`.
- Surface a NapCat-down vs not-logged-in distinction in `QqHero` using the new health field.
- Add the missing test file `ui/app/(admin)/channels/qq/page.test.tsx` (R5 gap), mirroring
  `telegram/page.test.tsx` (vi.mock the api module before import; QueryClientProvider wrapper; assert
  `qq-*` test-ids; cover keywords-dirty save, reconnect, scan-login open).

---

## 3. NapCat embedding — docker (default) + native systemd

Current state (verified): NapCat exists ONLY as the docker sidecar `docker-compose.qq.yml`
(`image: mlikiowa/napcat-docker:latest`, `profiles: ["qq"]`, webui 6099, OneBot WS 3001), opt-in via
`--with-qq`. `install.sh:1086-1087` hard-rejects `--with-qq` in native mode. `.env.template` has no
`QQ_WS_URL`/`CORLINMAN_NAPCAT_URL`.

### 3a. Docker — make NapCat default

- `deploy/install.sh`: default `WITH_QQ=1` for docker mode; add `--without-qq` opt-out. Remove the hard
  native rejection at 1086-1087 (replaced by native provisioning below). Keep layering
  `docker-compose.qq.yml` when QQ is on.
- `docker/compose/docker-compose.qq.yml:16`: pin the image —
  `image: mlikiowa/napcat-docker:${NAPCAT_VERSION:-v4.x.x}` (choose a concrete stable tag; see
  open_decision). `latest` is unsafe for prod.
- `.env.template`: add `NAPCAT_VERSION`, `QQ_WS_URL` (`ws://napcat:3001` docker / `ws://127.0.0.1:3001`
  native), `CORLINMAN_NAPCAT_URL` (`http://napcat:6099` / `http://127.0.0.1:6099`) with comments.

### 3b. Native systemd — provision NapCat

New `install.sh` logic (model on `write_gateway_unit`):

- `download_napcat_appimage()`: fetch the NapCat AppImage for the host arch (x86_64/aarch64) from the
  official NapCatQQ GitHub release pinned to `$NAPCAT_VERSION` → `$PREFIX/napcat/`, chmod +x,
  checksum-verify if a sums file is published.
- `write_napcat_unit()`: emit `/etc/systemd/system/corlinman-napcat.service`
  (`User=corlinman`, `WorkingDirectory=$DATA_DIR`, `ExecStart=$PREFIX/napcat/<appimage> ...`,
  `Environment=HOME=$DATA_DIR`, `EnvironmentFile=-$PREFIX/.env`, `Restart=on-failure`,
  state under `$DATA_DIR/.napcat/{app,ntqq}`). Call from `_apply_native_ref` so upgrades regenerate it.
- `write_gateway_unit`: export `QQ_WS_URL=ws://127.0.0.1:3001` and
  `CORLINMAN_NAPCAT_URL=http://127.0.0.1:6099` so native gateway + scan-login resolve NapCat.
- Doctor/preflight: warn if QQ is enabled but the NapCat unit/binary is missing.

### 3c. Embedding decision

Two viable approaches — see open_decisions. Default recommendation: **(a) AppImage + dedicated systemd
unit** for native, **docker sidecar** for docker (least invasive, NapCat upstream ships both). Avoid
embedding NapCat as a gateway subprocess unless durable supervision is wanted (more code, more risk).

---

## 4. Surface the 5 missing channels in the frontend

Backend-verified: discord, slack, feishu, wechat_official, qq_official are runtime-bootstrapped but have
**zero admin endpoints**. So each channel needs BOTH a backend admin surface AND a UI page.

### 4a. Per-channel page classification

| Channel | Page type | Rationale |
|---|---|---|
| discord | **full inbox** | Gateway+REST, mutable edit, inbound messages — Telegram-class. |
| slack | **full inbox** | Socket Mode + Web API, inbound messages. |
| feishu | **full inbox** | Long-connection + REST, p2p+group inbound. |
| wechat_official | **config-only** | Webhook passive-reply, 5s window; no live inbox stream. |
| qq_official | **config-only** | Official Gateway, 5-min reply threading; sender-focused. |

### 4b. Backend admin endpoints to add (standardized)

Add a uniform pattern in `routes_admin_a/channels.py` (or a new `channels_extra.py` mounted by
`routes_admin_a/__init__.py:88-110`). For EVERY new channel:

- `GET /admin/channels/{discord|slack|feishu|wechat_official|qq_official}/status`
  → `{ configured, enabled, online, last_event_at_ms, error_message, config_keys: {...non-secret...} }`.
- `GET /admin/channels/{discord|slack|feishu}/messages` → recent message deque (store
  `*_RECENT_MESSAGES` in `corlinman_channels.service` parallel to `TELEGRAM_RECENT_MESSAGES`).
- `POST /admin/channels/{discord|slack|feishu}/send` (body: target_id, text) via a live sender parked on
  `AdminState` (mirror `telegram_sender`). `channels_runtime.bootstrap` must attach
  `discord_sender`/`slack_sender`/`feishu_sender` when those channels start.
- `GET /admin/channels/{channel}/config` for the config-only channels (non-secret keys).

Add per-channel `*_HEALTH` snapshots (online flag, last heartbeat) populated by each adapter, exposed by
`/status` (R2 + R3 recommend a shared `ConnectivityProbe`/health-watcher abstraction — implement once,
reuse across discord/slack/feishu, modeled on `_qq_health_watcher`).

### 4c. UI files to create

Sidebar (`ui/components/layout/sidebar.tsx`, channels group children 109-120): add 5 `NavItem`s
`{ href: "/channels/{id}", labelKey: "nav.channel{Xxx}", icon }`. Icons: Discord→`MessageCircle`,
Slack→`Hash`/`Slack`, Feishu→`MessageSquareText`, WeChat→`MessageCircle`, QQ Official→`MessageCircle`.

i18n (`ui/lib/locales/en.ts` + `zh-CN.ts`): add `nav.channelDiscord`, `nav.channelSlack`,
`nav.channelFeishu`, `nav.channelWechatOfficial`, `nav.channelQqOfficial` at the nav top level, plus a
`channels.{id}.tp.*` namespace per channel (copy the Telegram/QQ blocks as templates).

API clients (`ui/lib/api/`): new modules `discord.ts`, `slack.ts`, `feishu.ts` (full: status + messages +
send), and `wechat_official.ts`, `qq_official.ts` (status + config only). Or extend `lib/api.ts` for the
two config-only channels. Inline types from the backend response shapes.

Pages (full inbox — copy `telegram/page.tsx` structure, swap types + endpoints + i18n namespace, reuse
`MessageList`/`SendTestDrawer`/`MediaPreviewDrawer`):
- `ui/app/(admin)/channels/discord/page.tsx` (+ MessageList adapter)
- `ui/app/(admin)/channels/slack/page.tsx`
- `ui/app/(admin)/channels/feishu/page.tsx`

Pages (config-only — copy `qq/page.tsx` via `ChannelShell`, drop the messages panel):
- `ui/app/(admin)/channels/wechat_official/page.tsx`
- `ui/app/(admin)/channels/qq_official/page.tsx`

Tests: `page.test.tsx` for each new page mirroring `telegram/page.test.tsx`.

Build assertion: extend the §0.2 route-existence assertion to include all 5 new
`out/channels/{id}.html` files.

---

## 5. Channel completeness checklist (apply to every channel)

For each of telegram, qq, discord, slack, feishu, wechat_official, qq_official, verify and close gaps:

| Capability | telegram | qq | discord | slack | feishu | wechat_off | qq_off |
|---|---|---|---|---|---|---|---|
| Inbound text receive | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Inbound media receive | ✅ | partial (img/audio; add video/file) | check | check | check | n/a | check |
| Outbound text send | ✅ | ✅ | ✅ | ✅ | ✅ | passive/cs | ✅ |
| Outbound inline media | ✅ | **add (handler)** | ✅ | verify files.upload | ✅ | limited | verify |
| File upload | ✅ | ✅ | ✅ | check | ✅ | n/a | check |
| Mutable/live status | spinner | summary prelude (ok) | spinner | spinner | spinner | n/a | check |
| Slash command handlers | ✅ | **add direct handlers** | check | check | check | n/a | check |
| Rate limiting | inline | ✅ | check | check | check | n/a | check |
| Health/online flag | TELEGRAM_HEALTH | QQ_HEALTH | **add** | **add** | **add** | **add** | **add** |
| Reconnect/backoff | n/a (poll) | ✅ (sched) | check | check | check | n/a | token refresh |
| Admin /status endpoint | ✅ | ✅ | **add** | **add** | **add** | **add** | **add** |
| Admin /messages endpoint | ✅ | n/a | **add** | **add** | **add** | n/a | n/a |
| Admin /send (test) endpoint | ✅ | via attachment | **add** | **add** | **add** | optional | optional |
| Frontend page | ✅ | ✅ | **add** | **add** | **add** | **add** | **add** |
| Sidebar + i18n | ✅ | ✅ | **add** | **add** | **add** | **add** | **add** |
| Page tests | ✅ | **add** | **add** | **add** | **add** | **add** | **add** |

"check" = audit during implementation against `discord.py`/`slack.py`/`feishu.py` and close if missing.

---

## 6. Sequencing, parallelization, CI gates

### Workstreams (can run largely in parallel after WS-0)

- **WS-0 (blocking, first): Verify + fix problem (1).** Rebuild UI, redeploy `ui-static`, add the
  route-existence build assertion + the backend serving test, live smoke test `/channels/qq`. This
  unblocks everything UI-facing and confirms the real root cause before any layout refactor is attempted.
- **WS-1: QQ completion (backend).** `onebot.py`/`service.py` inline image send, inbound video/file,
  command handlers, health counters, reconnect. Parallel with WS-2/WS-3.
- **WS-2: NapCat embedding.** `install.sh` (docker default + native AppImage/unit), compose pin,
  `.env.template`, doctor warning, docs. Independent of WS-1/WS-3.
- **WS-3: 5-channel backend admin endpoints.** `/status` (+ `/messages`, `/send` for inbox channels),
  health snapshots, live senders on `AdminState`, bootstrap attach. Gates WS-4.
- **WS-4: 5-channel UI** (sidebar, i18n, api clients, pages, tests). Starts after WS-3 endpoint shapes
  are fixed (depends on response types). QQ UI health surfacing (from WS-1) folds in here.
- **WS-5: completeness audit + checklist closure** across all channels (sweep "check" cells). Last.

Parallelizable: WS-1, WS-2, WS-3 fully independent. WS-4 depends on WS-3 shapes. WS-0 first.

### CI gates to run (per the audit-loop conventions; do NOT mass-sweep pre-existing ruff/mypy debt)

- UI: `ui-typecheck` (tsc), `ui-test` (vitest), `ui-build` (next build + new route-existence assertion).
  Run after WS-0 and any WS-4 change. Note the Node>=22 `--localstorage-file` guard already in CI.
- Python: `pytest` for `corlinman-server` (new serving test + new channel admin endpoint tests) and
  `corlinman-channels` (QQ handler tests). `ruff check` + `mypy` on the touched files/packages only.
- Installer: shellcheck `deploy/install.sh`; a dry-run of the docker `--without-qq`/default path and the
  native NapCat unit generation in CI if a runner allows.

### Verification before claiming done

- `out/channels/{qq,telegram,discord,slack,feishu,wechat_official,qq_official}.html` all exist and are
  not byte-equal to `out/404.html`.
- Live: each `/channels/{id}` curls to its shell (not 404.html) and renders logged-in.
- QQ: inline image reply lands in a group + DM; `/help` returns without LLM; status shows send counters.
- NapCat: native systemd unit comes up, scan-login QR resolves in both docker and native.

---

## Open decisions (require user input)

1. **NapCat embedding approach for native mode**: (a) AppImage + dedicated `corlinman-napcat.service`
   systemd unit (recommended, least code), vs (b) gateway-supervised subprocess (durable but more code +
   risk), vs (c) require docker-only for QQ (drops native QQ — contradicts the request). Default: (a).
2. **NapCat version pin**: which concrete `mlikiowa/napcat-docker` / NapCatQQ release tag to pin (replace
   `latest`). Needs a known-good stable tag for 2026-Q2.
3. **Make `--with-qq` the docker default**: confirm flipping default to ON (with `--without-qq` opt-out),
   which changes fresh-install behaviour and pulls the NapCat image by default.
4. **wechat_official / qq_official page depth**: confirm config-only (no inbox) is acceptable, or whether
   a lightweight recent-events log is wanted for these webhook/gateway channels.
5. **QQ reconnect**: implement a real reconnect (bounce WS/NapCat) or keep the documented 501 stub.
