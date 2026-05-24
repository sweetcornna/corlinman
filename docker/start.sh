#!/bin/sh
set -e
export CORLINMAN_PY_CONFIG=/data/py-config.json

# Pre-render py-config.json in Python so the env is set before either
# process boots. Rust gateway also writes this file (identical schema)
# on config reload — this bootstrap lets python see providers on its
# very first resolve.
/opt/venv/bin/python3 - <<PY
import os, json, tomllib
cfg_path = os.environ.get("CORLINMAN_CONFIG", "/data/config.toml")
try:
    with open(cfg_path, "rb") as f:
        c = tomllib.load(f)
except FileNotFoundError:
    c = {}

providers = []
for name, p in (c.get("providers") or {}).items():
    if not isinstance(p, dict): continue
    kind = p.get("kind", name)
    # resolve api_key: {env=...} → env lookup; {value=...} → literal
    ak = p.get("api_key")
    if isinstance(ak, dict):
        if "env" in ak:
            ak = os.environ.get(ak["env"])
        elif "value" in ak:
            ak = ak["value"]
        else:
            ak = None
    providers.append({
        "name": name,
        "kind": kind,
        "enabled": p.get("enabled", True),
        "base_url": p.get("base_url"),
        "api_key": ak,
        "params": p.get("params", {}),
    })

aliases = {}
for k, v in (c.get("models", {}).get("aliases") or {}).items():
    if isinstance(v, dict):
        aliases[k] = v
    # else shorthand string → legacy fallback handles it; skip

emb = c.get("embedding")
if emb and emb.get("enabled") is not False and emb.get("provider") and emb.get("model") and emb.get("dimension"):
    emb_out = {k: emb.get(k) for k in ("provider","model","dimension","enabled","params")}
else:
    emb_out = None

out = {"providers": providers, "aliases": aliases, "embedding": emb_out}
with open("/data/py-config.json","w") as f:
    json.dump(out, f, indent=2)
print(f"py-config bootstrapped: {len(providers)} providers, {len(aliases)} aliases, embedding={bool(emb_out)}")
PY

# Now boot the agent sidecar + gateway in order (sidecar first).
/opt/venv/bin/corlinman-python-server &

# Wait for the agent sidecar's gRPC listener to accept on :50051 before
# starting the gateway — replaces a blind `sleep 3` that raced on slow
# boots. We test-connect via the bundled venv python (no socket-utils
# needed) and bail after ~18s so a broken sidecar can't block boot
# indefinitely; the gateway will then surface the failure via its own
# /health probe.
echo "waiting for python sidecar gRPC ready on :50051..."
ready=""
i=1
while [ "$i" -le 60 ]; do
    if /opt/venv/bin/python3 -c "import socket; s=socket.socket(); s.settimeout(0.5); s.connect(('127.0.0.1',50051))" 2>/dev/null; then
        ready="1"
        break
    fi
    sleep 0.3
    i=$((i + 1))
done
if [ -z "$ready" ]; then
    echo "WARN: python sidecar did not become ready within 18s — starting gateway anyway"
fi

exec /opt/venv/bin/corlinman-gateway --config /data/config.toml
