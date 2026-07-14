"""``corlinman_server.system.upgrader`` — one-click upgrade backend.

W1.1 of ``docs/PLAN_ONE_CLICK_UPGRADE.md`` §2 Wave 1/W1.1.

This module owns the abstract upgrade contract
(:class:`UpgraderProtocol`), the Docker SDK implementation
(:class:`DockerUpgrader`), and the shared persistence layer
(:class:`UpgradeStateStore`). The native systemd helper impl
(``NativeUpgrader``) lands in W1.2. Admin endpoint wiring lands in W1.3.

Consumers should not import the concrete impls directly. Instead use
:func:`resolve_upgrader` so the runtime mode and "is this impl actually
importable" decisions stay in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corlinman_server.system.upgrader.docker_upgrader import DockerUpgrader
from corlinman_server.system.upgrader.finalizer import finalize_boot
from corlinman_server.system.upgrader.native_upgrader import NativeUpgrader
from corlinman_server.system.upgrader.protocol import (
    UpgradeAlreadyRunning,
    UpgraderProtocol,
    UpgraderUnavailable,
)
from corlinman_server.system.upgrader.state import (
    UpgradeRequest,
    UpgradeStateStore,
    UpgradeStatus,
)

__all__ = [
    "DockerUpgrader",
    "NativeUpgrader",
    "UpgradeAlreadyRunning",
    "UpgradeRequest",
    "UpgradeStateStore",
    "UpgradeStatus",
    "UpgraderProtocol",
    "UpgraderUnavailable",
    "finalize_boot",
    "resolve_upgrader",
]


def resolve_upgrader(
    mode: str,
    *,
    store: UpgradeStateStore,
    **kwargs: Any,
) -> UpgraderProtocol | None:
    """Pick the concrete impl for ``mode`` or return ``None``.

    Behaviour:

    * ``mode="docker"`` → :class:`DockerUpgrader`. ``kwargs`` are forwarded
      so the gateway lifecycle can override ``repo``, ``container_name``,
      ``data_dir`` (shared request/status files) or
      ``docker_client_factory``.
    * ``mode="native"`` → :class:`NativeUpgrader` (systemd path-watcher).
    * Any other / unknown mode → ``None``. The admin route maps ``None``
      to a UI state that disables the one-click button and surfaces the
      copy-paste fallback.
    """
    if mode == "docker":
        return DockerUpgrader(store=store, **kwargs)
    if mode == "native":
        return NativeUpgrader(store=store, **kwargs)
    return None


def default_persist_path(data_dir: Path) -> Path:
    """Conventional location of the persisted upgrade state JSON.

    ``$DATA_DIR/.upgrade-state.json`` — mirrors
    ``update_checker``'s ``.update_check.json`` convention so all
    system-level cache/audit files cluster predictably.
    """
    return data_dir / ".upgrade-state.json"
