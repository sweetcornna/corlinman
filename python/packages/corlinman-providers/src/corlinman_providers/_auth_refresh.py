"""Reactive 401 auth-refresh helper shared by env-var- and file-keyed providers.

Two flavours of provider need to self-heal on a credential rotation that
happened *after* the adapter was constructed:

1. **Env-var-keyed** (OpenAI, OpenAI-compatible, Azure, Google, Bedrock):
   the operator may rotate the secret via ``export OPENAI_API_KEY=…`` /
   the gateway's ``/admin/secrets`` writer between requests. The
   in-process ``self._api_key`` is stale and the next chat returns 401.
2. **File-keyed** (Anthropic OAuth file, Codex OAuth file): another
   process (the CLI, a sibling gateway worker) rotated the JSON; our
   in-memory copy is stale.

Both cases share the same retry skeleton — attempt the call, on a 401
ask a provider-supplied ``refresh()`` callback to fix the in-process
credential, retry once. Only the first attempt's 401 triggers a retry;
a second 401 means the refreshed cred is also dead and the caller
should see the original :class:`AuthError`.

The Codex provider already has its own reactive recovery
(``_attempt_token_recovery`` in :mod:`codex_provider`) for the more
narrow ``token_invalidated`` server-revocation case. The Anthropic
provider has on-construction OAuth-file watching. This helper plugs the
remaining five env-var-keyed providers into the same pattern.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog

from corlinman_providers.failover import AuthError

logger = structlog.get_logger(__name__)


T = TypeVar("T")


async def with_401_recovery(
    call: Callable[[], Awaitable[T]],
    *,
    refresh: Callable[[], Awaitable[bool]],
    max_attempts: int = 1,
    provider: str | None = None,
) -> T:
    """Run ``call()``. On :class:`AuthError` from the first attempt, run
    ``refresh()`` once and retry. Re-raise on second 401.

    Parameters
    ----------
    call:
        Async zero-arg factory that performs the auth-bearing work
        (open the upstream stream, sign + POST, etc.).
    refresh:
        Async zero-arg callable that returns ``True`` when the
        in-process credential was actually updated (retry is worth it)
        and ``False`` when nothing changed (the same credential will
        get the same 401 — propagate the original error). Helpers like
        :func:`refresh_env_key_if_rotated` and
        :func:`refresh_file_credential_if_rotated` provide the standard
        shapes.
    max_attempts:
        How many extra attempts after the first 401. Defaults to 1
        (so total attempts = 2). Callers that want pure "raise" behaviour
        pass 0.
    provider:
        Optional provider name used in the recovery-attempt log line so
        operators can tell the env-rotated providers apart in shared
        logs.
    """
    attempts_left = max_attempts
    last_exc: AuthError | None = None
    while True:
        try:
            return await call()
        except AuthError as exc:
            last_exc = exc
            if attempts_left <= 0:
                raise
            attempts_left -= 1
            try:
                rotated = await refresh()
            except Exception as refresh_exc:
                # Refresh-callback exceptions never kill the request flow;
                # surface the original AuthError to the caller (with the
                # refresh failure chained as ``__cause__``) so failover
                # can pick the next adapter.
                logger.warning(
                    "auth_refresh.refresh_failed",
                    provider=provider,
                    error=str(refresh_exc),
                )
                raise last_exc from refresh_exc
            if not rotated:
                # The credential didn't change — retrying would just hit
                # the same 401. Surface the original AuthError so the
                # caller (failover layer) can pick the next adapter.
                logger.info(
                    "auth_refresh.no_rotation_detected",
                    provider=provider,
                )
                raise
            logger.info(
                "auth_refresh.retrying_after_rotation",
                provider=provider,
            )


# ---------------------------------------------------------------------------
# Helpers for the two refresh flavours
# ---------------------------------------------------------------------------


async def refresh_env_key_if_rotated(
    *,
    env_name: str,
    current: str | None,
    on_update: Callable[[str], None],
) -> bool:
    """Re-read ``env_name``; if it differs from ``current``, call ``on_update``.

    Returns ``True`` iff the env var carries a non-empty value that
    differs from the one currently held in-process. Empty env values
    are treated as "no rotation" — the operator didn't actually swap a
    new secret in, and clearing the key in-memory would just produce a
    "key missing" RuntimeError on retry.
    """
    new_value = os.environ.get(env_name)
    if not new_value:
        return False
    if new_value == current:
        return False
    on_update(new_value)
    return True


async def refresh_file_credential_if_rotated(
    *,
    path: Any,  # pathlib.Path — typed loose so this module stays import-light
    last_mtime: float | None,
    on_update: Callable[[Any, float], None],
) -> bool:
    """Stat ``path``; if mtime moved past ``last_mtime``, call ``on_update``.

    ``on_update(new_data, new_mtime)`` is invoked when the file has
    been modified. The caller is responsible for re-parsing the file
    inside ``on_update`` (the helper hands it the raw bytes so providers
    with different on-disk formats — JSON, TOML — share the same gate).

    Returns ``True`` iff the file's mtime advanced and ``on_update``
    was called.
    """
    try:
        stat = path.stat()
    except (OSError, AttributeError):
        return False
    new_mtime = stat.st_mtime
    if last_mtime is not None and new_mtime <= last_mtime:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    on_update(data, new_mtime)
    return True


__all__ = [
    "refresh_env_key_if_rotated",
    "refresh_file_credential_if_rotated",
    "with_401_recovery",
]
