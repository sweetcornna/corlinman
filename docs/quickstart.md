# Quickstart

The shortest path from a clean machine to a working corlinman gateway
you can chat with. The whole thing fits in five minutes; the chat-ready
state fits in sixty seconds if you accept the defaults.

> 中文读者：本页用英文写。如需中文版，欢迎补 PR；项目暂未维护双语 doc。

---

## Prerequisites

Pick one:

- **Docker path** — Docker Engine 24+ (the recommended setup).
- **Native path** — Python 3.12 with [`uv`](https://docs.astral.sh/uv/),
  Node 20+ with `pnpm`, and `protoc`. The gateway binds `:6005` by default.

Nothing else is required — no separate database, no message broker, no
external embedding service. corlinman boots from a single
`config.toml` under `$CORLINMAN_DATA_DIR` (default `~/.corlinman/`).

---

## Boot the gateway

### Docker (recommended)

```bash
git clone https://github.com/ymylive/corlinman && cd corlinman
docker compose -f docker/compose/docker-compose.yml up -d
```

The container exposes the gateway on `http://localhost:6005`. The first
boot writes a fresh `config.toml` into the mounted data volume.
Docker-backed plugin sandboxing is opt-in because it mounts the host Docker
socket:

```bash
docker compose -f docker/compose/docker-compose.yml \
  -f docker/compose/docker-compose.sandbox.yml up -d
```

### Native (from source)

```bash
git clone https://github.com/ymylive/corlinman && cd corlinman
./scripts/dev-setup.sh                              # deps + proto + hooks
uv sync --all-packages --frozen
pnpm -C ui install && pnpm -C ui build

uv run corlinman-gateway                            # FastAPI + uvicorn on :6005
```

Either path converges on the same URL: <http://localhost:6005>.

![Login screen](assets/quickstart-login.png "TODO: screenshot")
<!-- TODO: screenshot of the /login page in the Tidepool theme -->

---

## First login

On first boot the gateway seeds a default admin account:

| Field    | Value  |
| -------- | ------ |
| Username | `admin` |
| Password | `root`  |

Open <http://localhost:6005/login> and sign in with those credentials.

The gateway returns a session cookie **and** a `must_change_password`
flag on `/admin/me`. The UI honours the flag: regardless of where you
were trying to go, you land on **Account & Security** (`/account/security`).

![Account & Security page](assets/quickstart-security.png "TODO: screenshot")
<!-- TODO: screenshot of the /account/security page with the two forms -->

Change at least the password (the username is optional but recommended).
The page is two forms: one for username, one for password. Each requires
the current password and uses paste-only password fields with an eye
icon to reveal what you typed. On success the red "default password"
banner disappears, the flag flips server-side, and the rest of the admin
surface unlocks.

> **Why this exists**: `admin/root` is a deliberate convenience for
> local development. The forced rotation + persistent banner make sure
> nobody accidentally ships a production gateway with the seed
> credentials. The seed itself lives in
> [`gateway/lifecycle/admin_seed.py`][admin-seed].

---

## Watching the agent work

Once a turn is in flight, open `/admin/sessions/{key}` to see exactly
what the agent is doing — live, as it happens. `{key}` is the channel-
qualified session id (for example `telegram:42` or `qq:group:9001`); the
session list at `/admin/sessions` links straight to each row.

The session detail page is built from five linked surfaces. Together
they replace the old "spinning dots in the chat thread" with a
structured timeline:

1. **Text** — model output as it streams in, with a blinking caret on
   the trailing token.
2. **Reasoning block** — collapsible "Thinking" panel that shimmers
   amber while the model is producing thinking-mode tokens, then
   settles into a quiet card when the block closes. Click to fold it
   away.
3. **Tool widget** — one row per tool call. The badge shows
   `pending → running → completed` (or `error`) and the elapsed counter
   ticks live every second. Click to expand the row and see the full
   arguments + result through a per-tool renderer (`bash`, `read_file`,
   `write_file`, `webfetch`, `grep`, fallback `generic`). Long tools
   that take more than 10s post a heartbeat so the row keeps moving.
4. **Sub-agent tree** — when the agent spawns a child via `delegate`
   or a sub-task tool, the child's timeline nests inside the spawning
   tool widget (depth cap 3 so deeply recursive agents stay readable).
5. **Cost footer** — sticky at the bottom of the scroll area, five
   pills: total USD, turn count, average turn time, tool call count,
   "last turn N ago". Polled every 15s and refreshed instantly on
   `TurnComplete`.

A finished turn doesn't disappear — click into the turn card or visit
`/admin/sessions/{key}/turns/{turn_id}` to re-watch every event in
replay mode. The replay page is the same React component as the live
view, just seeded from the journal instead of the SSE stream, so it
looks pixel-identical to how the turn looked when it ran. A past-turns
pill row above the timeline jumps you to any earlier turn in the
session, and `/admin/models` opens a two-column ModelPickerDialog for
adding model aliases without typing the provider + model name twice.

Channel-side, the same data drives a compact one-liner. Telegram /
Discord / Slack / Feishu mutate a single message in place
(`🔧 bash … 12s` → `✅ bash (12.4s)`) and append a footer to the
final reply: `(elapsed: 12.4s · 3 tool calls · ~$0.012)`. QQ-family
channels can't edit messages so they post the same footer as a
separate `📋 本次操作:` summary block. The `~` prefix on the cost
disappears once the provider returns a billed figure.

See [Observability](observability.md) for the full event taxonomy and
API contract.

Want the model to dispatch a topic-specific agent or to pin a session
to a specific persona? `/admin/agents` lists what's available,
`/admin/subagents` shows live dispatches — see [multi-agent](multi-agent.md).

Want more procedural-knowledge skills than the 16 starters that ship
in-wheel? `/admin/skills` is two tabs: **Installed** lists everything
in the active profile (bundled rows are read-only, hub installs and
operator-authored rows can be pinned or removed), and **Browse Hub**
proxies the [openclaw ClawHub](https://clawhub.ai) so an operator can
search, preview, and install a community skill without touching the
host shell. The Install button kicks off a server-side download +
extract pipeline driven by SSE progress; on success the new row shows
up under Installed with a `hub:<slug>@<version>` origin badge. Full
walkthrough at [skill-hub](skill-hub.md).

Want to know when a new version drops? `/admin/system` polls GitHub and
surfaces a TopNav bubble when an upgrade is available — see
[system-updates](system-updates.md).

---

## Choose your setup path

Once the default password is gone, the gateway is technically ready to
chat — but no provider is wired yet. You have two choices.

### Path A — I want a real LLM

Visit `/onboard`. The wizard is four steps:

1. **Account** — auto-skipped when the seed has already created `admin`,
   with a "Customize admin account" escape hatch if you want to rename.
2. **Connect LLM** — paste credentials on **Settings → Credentials**
   (`/admin/credentials`) for any built-in provider (Anthropic, OpenAI,
   Google, DeepSeek, Qwen, GLM), or add an OpenAI-compatible upstream
   (vLLM, channel-pool sidecar, local gateway, etc.) via
   **Settings → Providers** (`/admin/providers`).
3. **Models** — pick the LLM, embedding, and (optional) TTS channels.
   The picker is two-stage (provider → model), with a search box for
   long channel lists.
4. **Confirm** — atomic write to `config.toml`, then a success card
   with a CTA back to **Account & Security** in case you skipped earlier.

After confirm, the agent loop is live. Try
<http://localhost:6005/v1/chat/completions> with the OpenAI client of
your choice, or open `/` for the admin dashboard.

### Path B — I'm just exploring

Visit `/onboard` and on **Step 2 (Connect LLM)** click
**Skip — use mock provider**.

The skip path POSTs `/admin/onboard/finalize-skip`, which writes a
`[providers.mock] enabled = true` block. The mock provider is a
deterministic echo built into
[`corlinman-providers`][mock-provider]: it returns the prompt back at
you so you can verify the agent loop end-to-end (tools, channels,
plugins, RAG) without spending tokens.

You can come back to `/onboard` later — it's idempotent (see the
**Troubleshooting** section below).

---

## Where to go next

| If you want to…                                  | Go to                          |
| ------------------------------------------------ | ------------------------------ |
| Run multiple isolated agents on the same box     | [Profiles](profiles.md)        |
| Add or rotate provider API keys from the UI      | [Credentials](credentials.md)  |
| Let the agent improve its skills over time       | [Evolution & Curator](evolution-curator.md) |
| Understand the gateway internals                 | [Architecture](architecture.md) |
| Deploy behind nginx + acme.sh                    | [Runbook](runbook.md)          |
| Author a tool plugin in Python / Node / bash     | [Plugin authoring](plugin-authoring.md) |

---

## Troubleshooting

### `/onboard` says "already onboarded"

The wizard short-circuits once `[admin]` and at least one enabled
`[providers.*]` block are present. If you want to re-run it:

```bash
# Stop the gateway first.
docker compose -f docker/compose/docker-compose.yml down
# Or, native:  pkill -INT -f corlinman-gateway

# Snapshot the current config, then strip the [providers.*] blocks
# (or the whole file — the gateway will re-seed admin/root).
cp ~/.corlinman/config.toml ~/.corlinman/config.toml.bak
$EDITOR ~/.corlinman/config.toml
```

Restart the gateway and visit `/onboard` again. To **change providers
without re-running the wizard**, use the [Credentials](credentials.md)
page instead — it's the supported way to add or rotate keys after
first boot.

### `/admin/login` returns 503

503 from `/admin/login` means the admin state isn't wired yet — the
gateway is still warming up. Wait two seconds and retry, or check
the logs:

```bash
# Docker
docker logs corlinman --tail 50

# Native
journalctl -u corlinman-gateway -f
```

A persistent 503 with `{"error": "session_store_missing"}` means the
session store didn't initialise — typically a data-dir permissions
problem. The session store path is
`<CORLINMAN_DATA_DIR>/admin-sessions.sqlite`; `chown` it back to the
process owner and the next request succeeds.

### I forgot the password I just set

Stop the gateway, delete the `[admin]` block from `config.toml`,
restart. The seed kicks back in: you're on `admin/root` again with
`must_change_password=true`.

### The mock provider is replying with "echo:" but I wanted GPT-4

You skipped the LLM step. Open the [Credentials](credentials.md) page,
paste your OpenAI key into the `openai` row's `api_key` field, hit
**Save**, and the next chat request routes to the real provider. The
mock provider stays enabled but only catches the chats whose `model`
field maps to it via `[models]` aliases.

---

## What the gateway actually wrote

After a clean onboard + skip path you'll find:

```text
~/.corlinman/
├── config.toml              # main config — [admin], [providers.*], [models]
├── admin-sessions.sqlite    # session cookies (argon2id-hashed seed key)
├── sessions.sqlite          # per-channel conversation history
├── kb.sqlite                # RAG knowledge-base index
├── evolution.sqlite         # evolution signals + curator state
└── profiles/                # multi-agent isolation (see profiles.md)
    └── default/
        ├── SOUL.md
        ├── MEMORY.md
        ├── USER.md
        ├── state.db
        └── skills/
```

Everything is plain SQLite + markdown — no opaque blobs. The
[Architecture](architecture.md) doc explains who reads and writes
each file.

[admin-seed]: ../python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/admin_seed.py
[mock-provider]: ../python/packages/corlinman-providers/src/corlinman_providers/mock.py
