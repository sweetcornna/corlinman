"""Abstract upgrader contract shared by Docker + Native impls.

W1.1 of ``docs/PLAN_ONE_CLICK_UPGRADE.md`` §2 Wave 1/W1.1.

Both impls (``DockerUpgrader`` in W1.1, ``NativeUpgrader`` in W1.2) must
satisfy :class:`UpgraderProtocol`. The admin route in W1.3 picks the
right impl through :func:`corlinman_server.system.upgrader.resolve_upgrader`
based on the runtime mode (``docker`` / ``native``) and is unaware of
the underlying mechanism beyond what the protocol exposes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from corlinman_server.system.upgrader.state import (
    UpgradeRequest,
    UpgradeStatus,
)


__all__ = [
    "UpgraderProtocol",
    "UpgradeAlreadyRunning",
    "UpgraderUnavailable",
]


class UpgradeAlreadyRunning(Exception):
    """Raised by :meth:`UpgraderProtocol.start` when a request is in flight.

    The admin route maps this to ``409 Conflict``. The ``in_flight``
    attribute carries the existing status so the caller can surface
    "another upgrade is already running for tag X (request_id Y)".
    """

    def __init__(self, in_flight: UpgradeStatus) -> None:
        super().__init__(
            f"upgrade already in flight: request_id={in_flight.request_id!r} "
            f"tag={in_flight.tag!r} state={in_flight.state!r}"
        )
        self.in_flight = in_flight


class UpgraderUnavailable(Exception):
    """Raised when the upgrader can't run right now (no docker.sock, …).

    The admin route maps this to ``503 Service Unavailable``. Endpoints
    are expected to short-circuit by calling :meth:`is_available` first
    and only reaching this exception for races (socket lost between the
    pre-check and ``start()``).
    """


@runtime_checkable
class UpgraderProtocol(Protocol):
    """One impl per runtime mode (docker / native)."""

    async def is_available(self) -> bool:
        """Returns ``True`` iff this upgrader can actually run an upgrade.

        Docker impl: ``docker.from_env()`` succeeds and ``ping()`` works
        — i.e. the socket is mounted read/write into this container.
        Native impl: the systemd ``corlinman-upgrader.service`` unit is
        installed and the path-watched request file is writable.

        The admin route short-circuits with ``503`` when this returns
        ``False`` so the UI never offers a button the user can't action.
        """
        ...

    async def start(
        self, target_tag: str, actor: str
    ) -> UpgradeRequest:
        """Kick off an upgrade in the background. Returns immediately.

        Implementations MUST:

        1. Read :meth:`UpgradeStateStore.current_in_flight` and raise
           :class:`UpgradeAlreadyRunning` when one exists.
        2. Mint a request_id (``uuid4().hex``), persist via
           :meth:`UpgradeStateStore.begin`.
        3. Spawn an ``asyncio.create_task`` for the actual work so the
           HTTP handler can return ``202 Accepted`` in tens of
           milliseconds.

        Failure is reported through the store (``state="failed"``),
        never via exception — except for the two single-flight /
        availability guards above.
        """
        ...

    async def progress(
        self, request_id: str
    ) -> AsyncIterator[UpgradeStatus]:
        """Yield :class:`UpgradeStatus` snapshots until terminal.

        The admin SSE route iterates this and emits one event per
        snapshot. Implementations should poll the store at a sane
        interval (the default Docker impl uses 500ms).

        After the first terminal snapshot (``succeeded`` / ``failed``)
        the iterator MUST stop — the SSE consumer terminates the
        connection on that boundary.

        Yields nothing and returns immediately if ``request_id`` is
        unknown — callers can treat an empty stream as 404.
        """
        # Protocol method — implementations override; this body exists so
        # the docstring + type signature are valid.
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]
