# Runbook — VPS prod model swap (gpt-5.5 / high reasoning)

**Target**: `corlinman.cornna.xyz` — `43.133.12.98`, native systemd mode (data dir `/opt/corlinman/data`).
**Date**: 2026-05-25
**Operator**: run as the user that owns `/opt/corlinman/data` (usually `root` or `corlinman`).
**Estimated time**: ~2 min, ~5s downtime during service restart.

---

## Goal

Switch the production agent to a new provider + model:
- Provider name: `cornna`
- Base URL: `https://api.cornna.xyz/v1`
- API key: `<REDACTED — see operator-only secrets store; key was rotated 2026-05-25 after a git-history leak; do NOT use the original key from the commit message of the runbook>`
- Model: `gpt-5.5`
- `reasoning_effort = "high"` (deep-think mode)

The `[models]` default alias is repointed so every agent + the protocol playground picks it up automatically.

---

## Step 0 — SSH in & pre-check

```bash
ssh root@43.133.12.98   # or your usual access alias

# Confirm we're on the native-systemd box and service is running
systemctl status corlinman --no-pager | head -10
test -d /opt/corlinman/data && echo "data dir present"
test -f /opt/corlinman/data/config.toml && echo "config present"
```

Expected: `Active: active (running)` and both file checks print.

---

## Step 1 — Snapshot the current config (rollback insurance)

```bash
sudo cp /opt/corlinman/data/config.toml \
        /opt/corlinman/data/config.toml.bak.$(date +%Y%m%d-%H%M%S)
ls -lh /opt/corlinman/data/config.toml.bak.*
```

Roll back at any time with:

```bash
sudo cp /opt/corlinman/data/config.toml.bak.<TIMESTAMP> /opt/corlinman/data/config.toml
sudo systemctl restart corlinman
```

---

## Step 2 — Edit config.toml

Open the file:

```bash
sudo $EDITOR /opt/corlinman/data/config.toml
```

### 2a. Add the `cornna` provider

If a `[providers.cornna]` block already exists, **replace** its body; otherwise append this block at the bottom of the `[providers.*]` section (above `[models]`):

```toml
[providers.cornna]
kind = "openai_compatible"
api_key = "REPLACE_WITH_THE_NEW_KEY_FROM_OPERATOR"  # paste here, do not commit
base_url = "https://api.cornna.xyz/v1"
enabled = true
```

> The actual key is delivered out-of-band (1Password / Lark DM / paper).
> If the operator accidentally typed it into a file that is tracked by
> git, rotate it on api.cornna.xyz immediately.

### 2b. Repoint the default alias

Find the `[models]` table and ensure:

```toml
[models]
default = "gpt-5.5"
```

### 2c. Add (or replace) the `gpt-5.5` alias with `reasoning_effort = "high"`

In the `[models.aliases.*]` section, ensure this block exists. **Replace** it if there's already a `[models.aliases."gpt-5.5"]` pointing somewhere else:

```toml
[models.aliases."gpt-5.5"]
provider = "cornna"
model = "gpt-5.5"
params = { reasoning_effort = "high" }
```

Save and exit.

### 2d. Validate TOML syntax (catches typos before restart)

```bash
python3 -c 'import tomllib; tomllib.loads(open("/opt/corlinman/data/config.toml").read()); print("ok")'
```

Must print `ok`. If it raises, fix the line it points at — do **not** continue.

---

## Step 3 — Reload

Pick **one** of the two paths:

### Path A — hot reload (no service interruption, preferred)

```bash
# Discover the admin token (server prints it on first boot; usually stashed here):
sudo cat /opt/corlinman/data/admin-token 2>/dev/null || \
  sudo grep -E 'admin_token|ADMIN_TOKEN' /opt/corlinman/data/config.toml

ADMIN_TOKEN="<paste-it>"

curl -sS -X POST http://127.0.0.1:8080/admin/config/reload \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq .
```

Expected: `{"ok": true, "applied": [...]}` listing `providers.cornna` and `models.aliases.gpt-5.5` (or `models.default`) in the applied diff.

### Path B — full restart (if reload fails or you'd rather)

```bash
sudo systemctl restart corlinman
sleep 2
systemctl is-active corlinman   # should print "active"
journalctl -u corlinman -n 30 --no-pager
```

Look for `gateway.ready` / `providers.loaded provider=cornna` lines. Bail and roll back (Step 1) if you see `provider.cornna.error` or pydantic validation failures.

---

## Step 4 — Verify end-to-end

```bash
# 1) Provider is loaded
curl -sS http://127.0.0.1:8080/admin/providers \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq '.[] | select(.name=="cornna")'

# 2) Alias is reachable
curl -sS http://127.0.0.1:8080/admin/models \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq '.data[] | select(.id=="gpt-5.5")'

# 3) Fire one real completion (smoke test — costs ~$0.001)
curl -sS http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.5",
    "messages": [{"role":"user","content":"reply with the single word: pong"}],
    "max_tokens": 8
  }' | jq .
```

Expected from step 3: a `chat.completion` envelope with `choices[0].message.content` containing "pong" (or a reasoning-mode equivalent). Latency for high-reasoning may be 5-15s — that's normal.

### 4b — UI smoke

Open `https://corlinman.cornna.xyz` in a browser. Send a turn in the protocol playground. Confirm:
- Cost footer shows non-zero token counts.
- Sidebar shows `gpt-5.5` as the active model.
- No red toast / no `provider error` banner.

---

## Step 5 — Cleanup

Once verified, the backup file from Step 1 can be retained for ~7 days then deleted:

```bash
ls /opt/corlinman/data/config.toml.bak.*
# leave them for now
```

---

## Rollback (if anything in Step 3 or 4 fails)

```bash
sudo cp /opt/corlinman/data/config.toml.bak.<TIMESTAMP> /opt/corlinman/data/config.toml
sudo systemctl restart corlinman
journalctl -u corlinman -n 50 --no-pager
```

---

## Notes

- The API key is sensitive. Don't paste it into Slack/Lark, don't commit it. It's only in `/opt/corlinman/data/config.toml` (root-owned, 0600 by default).
- `reasoning_effort = "high"` increases per-call latency and token spend by 3-5x versus default. Watch the cost footer / `/admin/metrics` for the first day.
- If you ever rotate the key: update the same `[providers.cornna].api_key` line + reload (Step 3a). No other changes needed.
