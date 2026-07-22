"""Fail-closed mtime-aware Tencent policy snapshot resolver."""

from __future__ import annotations

import json
from pathlib import Path


class ReloadingTencentPolicyResolver:
    """Read ``tencent_safety.enabled`` from the gateway's Python sidecar.

    Unlike provider reloads, any missing, unreadable, or malformed snapshot
    immediately resolves enabled so a stale opt-out cannot survive file loss.
    """

    def __init__(self, path: str | None) -> None:
        self._path = Path(path) if path else None
        self._mtime_ns: int | None = None
        self._enabled = True

    def _reload(self) -> None:
        if self._path is None:
            self._enabled = True
            return
        try:
            stat = self._path.stat()
            if self._mtime_ns == stat.st_mtime_ns:
                return
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            section = raw.get("tencent_safety") if isinstance(raw, dict) else None
            # Older/partial snapshots do not carry this section. Only a
            # well-formed section whose value is the literal boolean false may
            # opt out; every other valid JSON shape stays fail-closed.
            self._enabled = not (
                isinstance(section, dict) and section.get("enabled") is False
            )
            self._mtime_ns = stat.st_mtime_ns
        except Exception:
            self._enabled = True
            self._mtime_ns = None

    def __call__(self) -> bool:
        self._reload()
        return self._enabled
