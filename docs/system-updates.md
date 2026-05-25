# System updates

corlinman polls its own GitHub release feed and tells the operator when a
newer version is available. There is no in-app one-click upgrade — the
gateway can't `sudo` into the host, so the page hands you copy-paste
commands instead of pretending to run them for you.

This doc is for operators running a corlinman gateway who want to know
how the update banner works, how to turn it off, and what to do when it
misbehaves.

---

## How it works

On a fixed interval (default 6h) the gateway hits
`api.github.com/repos/ymylive/corlinman/releases/latest` with a stored
`If-None-Match: <etag>`. GitHub answers `200 OK` + a body the first
time, then `304 Not Modified` (with no body, no rate-limit cost) until a
new release tag goes out. The handler parses the response, compares the
returned tag against `importlib.metadata.version("corlinman-server")`
via `packaging.version.Version`, and stores the result in memory.

`<UpdateBubble />` in the admin TopNav polls `/admin/system/info` every
30s and renders an amber chip when the cached status says
`update_available = true`. Clicking it navigates to `/admin/system`,
where the operator sees the new tag, the release-notes body, and three
copy-paste upgrade commands (native / Docker / Docker + QQ adapter).

The actual upgrade — `git pull && uv sync && systemctl restart` or
`docker compose pull && up -d` — runs in the operator's shell, not in
the gateway. See [Runbook §10](runbook.md) for the canonical procedure.

---

## Configuration

The feature ships enabled with sensible defaults. The full stanza
(also documented inline in `docs/config.example.toml`):

```toml
[system.update_check]
enabled = true
interval_hours = 6
include_prereleases = false
repo = "ymylive/corlinman"
# github_token = { env = "CORLINMAN_GITHUB_TOKEN" }
```

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Master switch. `false` skips GitHub polls entirely (see [Air-gapped deployments](#air-gapped-deployments)). |
| `interval_hours` | `6` | Background poll cadence. The UI's "Check now" button bypasses this and is server-side rate-limited to 1/min. |
| `include_prereleases` | `false` | When `true`, `vX.Y.Z-rc.N` tags count as upgrade targets. |
| `repo` | `ymylive/corlinman` | Override only if you maintain a fork with its own release cadence. |
| `github_token` | unset | Optional PAT, read from env. Unauthenticated requests are throttled to 60/hr/IP; ETag caching keeps single-instance deploys well under that. Multi-instance setups should authenticate (see [GitHub rate limits](#github-rate-limits)). |

---

## The TopNav bubble

`<UpdateBubble />` sits in the admin TopNav between the health dot and
the language toggle. Three states:

- **No update** — nothing rendered. The DOM is empty; no layout shift.
- **Update available** — amber chip showing `vX.Y.Z` plus a small dot.
  Clicking it navigates to `/admin/system`.
- **Dismissed** — once the operator hits the close button on the chip,
  the tag is written to `localStorage["corlinman.update.dismissed"]`
  and the bubble stays hidden **for that tag**. The next release
  brings it back.

Dismissal is per-browser, per-tag — there is no server-side "I saw
this" state, so a second admin on a second machine still sees the
bubble until they dismiss it themselves.

---

## The `/admin/system` page

Reachable from the **System** entry in the admin sidebar (also from
clicking the bubble). Three cards:

1. **Version** — current vs. latest tag, last-checked-at, deploy mode
   (`docker` or `native`, sniffed from env), and a **Check now**
   button that POSTs `/admin/system/check-updates`.
2. **Release notes** — the GitHub release body, rendered through
   `react-markdown` + `rehype-sanitize`. Headings, lists, links, and
   fenced code blocks render; everything else is dropped. Empty when
   you're already on the latest tag.
3. **Upgrade commands** — three tabs (Native / Docker / Docker + QQ),
   each with a one-line shell snippet pre-filled with the target tag
   and a copy button.

---

## GitHub rate limits

The GitHub REST API allows 60 requests/hr/IP unauthenticated, 5000/hr
authenticated. With ETag-cached 304 responses the gateway burns one
*billable* call per release (the next 200), so a single instance polling
every 6h sits comfortably inside the unauth budget even on release-heavy
weeks.

You should configure `CORLINMAN_GITHUB_TOKEN` when:

- You run several gateway instances behind the same egress NAT and want
  to avoid them competing for the same 60/hr quota.
- Your egress IP is shared with other GitHub API consumers.
- You're seeing `403` responses in the gateway logs from
  `api.github.com`.

The token only needs public-repo read access. Fine-grained tokens
should grant **Contents: read**; classic tokens can ship with no scopes
selected.

---

## Air-gapped deployments

Set `enabled = false`. The poll loop never starts, `/admin/system/info`
returns the current version with `update_available = false` and
`latest_version = null`, and the `/admin/system` page still renders —
just without the release-notes card. The upgrade-commands card stays
populated against the current version so operators have something to
paste when they get a tag out of band.

---

## Security notes

- **Release-body sanitization.** GitHub release bodies are arbitrary
  markdown authored outside this codebase. `<ReleaseNotes>` runs them
  through `react-markdown` with `rehype-sanitize`'s default schema:
  `<script>` tags, `javascript:` URLs, inline event handlers
  (`onclick=`, `onerror=`, …), and `<iframe>`/`<object>`/`<embed>` are
  stripped. A unit test asserts that a `<script>` payload in the
  release body doesn't reach the DOM.
- **PAT handling.** `github_token` is read from env at boot and lives
  only in the `UpdateChecker` closure. The `/admin/system/info` and
  `/admin/system/check-updates` responses never include the token, the
  raw GitHub response headers, or any other upstream auth material.

---

## Limitations

- **No in-app upgrade.** Triggering `git pull` or `docker compose pull`
  requires privileges the gateway process intentionally does not have.
  The page is a polished version of "here's what to paste, you press
  enter."
- **Scheduler-driven auto-check is pending.** The `system.update_check`
  builtin is registered with the scheduler, but the gateway lifespan
  hasn't been wired to spawn the scheduler loop yet. In the meantime
  the per-tab 30s poll from `<UpdateBubble />` and the on-page-load
  fetch on `/admin/system` keep detection live — what's missing is the
  background poll that fires when no admin tab is open. A future
  release will close this gap.
- **Single source repo.** The checker polls one repo at a time. Forks
  that want to track both upstream and their own releases need to wire
  a second checker themselves.

---

## Troubleshooting

### The bubble never appears even though a new release is out

1. Confirm `[system.update_check].enabled = true` in `config.toml`.
2. Open `/admin/system` and click **Check now**. If the request returns
   `update_available = true`, the issue is client-side — clear
   `localStorage["corlinman.update.dismissed"]` and reload.
3. If **Check now** returns `update_available = false`, check the
   gateway logs for `update_checker` events. Common causes: the host
   can't reach `api.github.com`, or `repo` is misconfigured.
4. `last_checked_at` more than `2 * interval_hours` old means the
   scheduled poll isn't running — see [Limitations](#limitations);
   the manual button is the workaround until the lifespan wire-up
   lands.

### `403` from `api.github.com` in the logs

You're being rate-limited. Either accept the stale cache (the next
successful 304/200 will refresh it) or set `CORLINMAN_GITHUB_TOKEN`
and restart. See [GitHub rate limits](#github-rate-limits).

### "Check now" returns 429

The endpoint is server-side rate-limited to 1/min per gateway to keep
operators from hammering GitHub from the UI. Wait a minute and retry;
the background poll is unaffected.

---

## See also

- [Runbook §10 — upgrading the gateway](runbook.md#10-升级新版本) for the
  actual upgrade procedure the copy buttons hand you.
- [`docs/config.example.toml`](config.example.toml) — the inline-
  commented version of the config stanza.
