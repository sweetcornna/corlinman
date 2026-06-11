"""Attach brain — SSE client to a running gateway (opencode pattern).

``corlinman console --attach http://host:8000`` turns the console into a
pure client of the gateway's OpenAI-compatible ``/v1/chat/completions``
SSE surface — the same contract the web playground and external OpenAI
SDK callers use. Session continuity rides the ``X-Session-Key`` header
(see ``gateway/routes/chat.py::_resolve_session_key``).

Wire notes (must track ``routes/chat.py``):

* token text   → ``choices[0].delta.content``
* tool calls   → ``choices[0].delta.tool_calls[]`` with **complete**
  ``function.arguments`` per chunk (the gateway buffers fragments);
  tool *results* are not rendered over HTTP, so attach mode shows tool
  starts only — the renderer tolerates unmatched starts.
* terminal     → ``choices[0].finish_reason`` set, then ``data: [DONE]``
* errors       → ``{"error": {reason, message, …}}`` data frame
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from corlinman_server.console.events import (
    ConsoleEvent,
    TextDelta,
    ToolStarted,
    TurnDone,
    TurnError,
)

__all__ = ["AttachBrain", "parse_sse_data"]

log = logging.getLogger(__name__)

_TURN_TIMEOUT_S = 15 * 60.0  # generous — agent turns with tools run long


def parse_sse_data(data: str) -> list[ConsoleEvent]:
    """Map one SSE ``data:`` payload to console events.

    Pure function (unit-tested without a server). ``[DONE]`` is handled
    by the caller; a malformed payload is skipped, not fatal — a single
    bad frame must not kill the turn.
    """
    try:
        obj: Any = json.loads(data)
    except ValueError:
        log.debug("console.attach.bad_frame data=%.120s", data)
        return []
    if not isinstance(obj, dict):
        return []

    err = obj.get("error")
    if isinstance(err, dict):
        return [
            TurnError(
                reason=str(err.get("reason") or err.get("code") or "unknown"),
                message=str(err.get("message") or ""),
            )
        ]

    events: list[ConsoleEvent] = []
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            events.append(TextDelta(text=content))
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            events.append(
                ToolStarted(
                    tool=str(fn.get("name") or ""),
                    call_id=str(tc.get("id") or ""),
                    args_json=str(fn.get("arguments") or "{}").encode("utf-8"),
                )
            )
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            events.append(TurnDone(finish_reason=finish))
    return events


class AttachBrain:
    """Brain backed by a remote gateway's SSE chat endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = dict(headers or {})
        self.descriptor = f"attached ({self.base_url})"

    def run_turn(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        session_key: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[ConsoleEvent]:
        async def _gen() -> AsyncIterator[ConsoleEvent]:
            import httpx  # noqa: PLC0415 — keep module import light

            url = f"{self.base_url}/v1/chat/completions"
            payload = {"model": model, "messages": messages, "stream": True}
            headers = {"X-Session-Key": session_key, **self._headers}
            terminal_seen = False

            try:
                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(_TURN_TIMEOUT_S, connect=10.0)) as client,
                    client.stream("POST", url, json=payload, headers=headers) as resp,
                ):
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        yield TurnError(
                            reason=f"http_{resp.status_code}",
                            message=body[:300],
                        )
                        return
                    async for line in resp.aiter_lines():
                        if cancel.is_set():
                            yield TurnError(reason="unknown", message="cancelled")
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data:
                            continue
                        if data == "[DONE]":
                            break
                        for ev in parse_sse_data(data):
                            if isinstance(ev, (TurnDone, TurnError)):
                                terminal_seen = True
                                yield ev
                                # The gateway still sends [DONE] after the
                                # finish chunk; we can stop reading now.
                                return
                            yield ev
            except (httpx.HTTPError, OSError) as exc:
                with contextlib.suppress(Exception):
                    log.info("console.attach.transport_error err=%s", exc)
                yield TurnError(reason="transport", message=str(exc))
                return
            if not terminal_seen:
                yield TurnDone(finish_reason="stop")

        return _gen()

    async def aclose(self) -> None:
        """Nothing persistent to close — each turn owns its client."""
