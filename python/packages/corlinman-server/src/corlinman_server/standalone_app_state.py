"""Shared state handles for the standalone agent process."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class StandaloneAppState:
    memory_host: Any = None
    memory_kernel: Any = None
    identity_store: Any = None
    identity_resolver: Any = None
    memory_scope_config: dict[str, Any] = field(default_factory=dict)
    memory_kernel_config: dict[str, Any] = field(default_factory=dict)
    _closed: bool = False

    async def aclose(self) -> None:
        """Close each independently owned handle exactly once."""
        if self._closed:
            return
        self._closed = True
        for handle in (
            self.identity_resolver,
            self.memory_kernel,
            self.memory_host,
        ):
            close = getattr(handle, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()
        self.identity_resolver = None
        self.identity_store = None
        self.memory_kernel = None
        self.memory_host = None


async def build_standalone_app_state(data_dir: Path) -> StandaloneAppState:
    """Open the same identity/memory handles under the execution-state root."""
    data_dir.mkdir(parents=True, exist_ok=True)
    state = StandaloneAppState()
    try:
        from corlinman_memory_host import LocalSqliteHost

        state.memory_host = await LocalSqliteHost.open(
            "local", data_dir / "memory.sqlite"
        )
    except Exception as exc:  # noqa: BLE001 — memory-free chat degrades
        logger.warning("agent.standalone.memory_host_failed", error=str(exc))
    try:
        from corlinman_memory_kernel import MemoryKernel

        state.memory_kernel = await MemoryKernel.open(data_dir / "memory.sqlite")
    except Exception as exc:  # noqa: BLE001 — kernel-free chat degrades
        logger.warning("agent.standalone.memory_kernel_failed", error=str(exc))
    try:
        from corlinman_identity import (
            SqliteIdentityStore,
            UserIdentityResolver,
            identity_db_path,
            legacy_default,
        )

        store = await SqliteIdentityStore.open(
            identity_db_path(data_dir, legacy_default())
        )
        state.identity_store = store
        state.identity_resolver = UserIdentityResolver(store)
    except Exception as exc:  # noqa: BLE001 — raw sender fallback remains safe
        logger.warning("agent.standalone.identity_resolver_failed", error=str(exc))
    return state
