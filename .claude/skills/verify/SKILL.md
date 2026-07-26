---
name: verify
description: Verify gateway changes through an isolated running server
---

# Gateway runtime verification

1. Create a temporary data directory and a minimal TOML config for the changed section.
2. Launch with `uv run corlinman-gateway --config <tmp>/config.toml --data-dir <tmp>/data --host 127.0.0.1 --port <unused>` and redirect logs.
3. Poll `/health`, then drive the changed HTTP surface. Keep public effects disabled or use dry-run/shadow seams.
4. For py-config work, inspect `<tmp>/data/py-config.json`; do not pre-set `CORLINMAN_PY_CONFIG`, or the placement check is invalid.
5. Probe an adjacent failure (for admin mutation work, an unauthenticated request should return 401).
6. Send SIGTERM and `wait` for graceful shutdown. Capture health, response bodies, sidecar fields, and shutdown status inline.

A config must exist for `py-config.json` to be emitted; `config_path=None` intentionally writes no sidecar.
