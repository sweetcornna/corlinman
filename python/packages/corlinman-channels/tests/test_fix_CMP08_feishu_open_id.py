"""CMP-08 — Feishu bot open_id must be resolved on connect().

``FeishuAdapter`` sets ``_bot_open_id`` to ``""`` and never assigns it, so
``is_mentioning_bot`` short-circuits to ``False`` and the group gate drops
every group message (``respond_to_all`` defaults False).

Acceptance: after ``connect()`` the adapter's ``_bot_open_id`` is the real id
from ``GET /open-apis/bot/v3/info``, so a group @mention is detected.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from corlinman_channels.feishu import FeishuAdapter, FeishuConfig

BOT_OPEN_ID = "ou_real_bot_42"


def _rest_client_with_bot_info() -> httpx.AsyncClient:
    """Mock transport answering token + bot-info + ws-endpoint."""

    def _handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200},
            )
        if path.endswith("/bot/v3/info"):
            return httpx.Response(
                200,
                json={"code": 0, "bot": {"open_id": BOT_OPEN_ID, "app_name": "x"}},
            )
        if path.endswith("/callback/ws/endpoint"):
            return httpx.Response(
                200, json={"code": 0, "data": {"URL": "wss://feishu.example/link"}}
            )
        return httpx.Response(200, json={"code": 99999, "msg": "not mocked"})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handle))


@pytest.mark.asyncio
async def test_connect_resolves_bot_open_id() -> None:
    adapter = FeishuAdapter(
        FeishuConfig(app_id="a", app_secret="s"),
        http_client=_rest_client_with_bot_info(),
    )
    # Don't actually dial the long-conn WS — stub the reader-loop spawn by
    # replacing _connection_loop so connect() returns once token + bot info
    # are fetched.
    async def _noop_loop() -> None:
        await asyncio.sleep(3600)

    adapter._connection_loop = _noop_loop  # type: ignore[method-assign]
    try:
        await adapter.connect()
        # Before the fix this is "" — the group mention gate is dead.
        assert adapter._bot_open_id == BOT_OPEN_ID
    finally:
        await adapter.close()
