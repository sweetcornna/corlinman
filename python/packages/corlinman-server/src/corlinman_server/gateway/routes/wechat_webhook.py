"""``/wechat/<bot_name>`` — WeChat Official Account public webhook.

WeChat (微信公众号) delivers every inbound message as an HTTPS POST
from Tencent's edge to a developer-configured URL. The signature lives
in the query string (``sha1(token + timestamp + nonce)``) and the body
is an XML envelope. This route file is the FastAPI seam that mounts
:meth:`corlinman_channels.WeChatOfficialAdapter.handle_webhook` on a
per-bot path so the operator can configure WeChat's "服务器配置" with
``https://<host>/wechat/<bot_name>``.

Why it lives outside ``routes_admin_a`` / ``routes_admin_b``
-----------------------------------------------------------

The two ``routes_admin_*`` packages gate everything behind the admin
auth middleware. WeChat's edge is **unauthenticated** from our side —
the only thing we trust is the SHA-1 signature derived from the shared
``token`` (handled inside the adapter). Mounting under ``/wechat/...``
means the public route surface stays small and the admin auth never
gets in the way of Tencent's prober.

Lifecycle
---------

The route registry is a process-global module-level dict mapped from
``bot_name`` to an :class:`adapter`. The channel runtime
(``run_wechat_official_channel``) calls :func:`register_bot` at startup;
the same name is later looked up per inbound POST. Re-registration with
the same name swaps the adapter in place — useful for /admin-driven hot
reload, harmless when not used.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

__all__ = [
    "WECHAT_BOT_REGISTRY",
    "build_wechat_router",
    "register_bot",
    "unregister_bot",
]

_log = logging.getLogger(__name__)

#: Process-global ``bot_name → WeChatOfficialAdapter`` registry. Kept as
#: a plain dict (not threaded) because every read/write happens on the
#: asyncio loop thread.
WECHAT_BOT_REGISTRY: MutableMapping[str, Any] = {}


def register_bot(bot_name: str, adapter: Any) -> None:
    """Register ``adapter`` under ``bot_name``.

    Called by :func:`corlinman_channels.run_wechat_official_channel` via
    the ``register_route`` callback. Logs a swap when the name was
    already taken — usually a sign of a config reload during dev.
    """
    if not bot_name:
        raise ValueError("wechat register_bot: bot_name is empty")
    if bot_name in WECHAT_BOT_REGISTRY:
        _log.info("wechat bot registry: replacing existing entry name=%s", bot_name)
    WECHAT_BOT_REGISTRY[bot_name] = adapter
    _log.info("wechat bot registry: registered name=%s", bot_name)


def unregister_bot(bot_name: str) -> None:
    """Drop ``bot_name`` from the registry. Idempotent.

    The channel-runtime shutdown path uses this so a restarted gateway
    doesn't accidentally route to a dead adapter held by an old worker.
    """
    if WECHAT_BOT_REGISTRY.pop(bot_name, None) is not None:
        _log.info("wechat bot registry: unregistered name=%s", bot_name)


def build_wechat_router() -> APIRouter:
    """Build the FastAPI ``APIRouter`` mounting ``/wechat/<bot_name>``.

    The router serves BOTH the URL-verify handshake (GET with ``echostr``)
    and the message POST. We delegate signature verification, XML parsing
    and passive-reply orchestration to
    :meth:`WeChatOfficialAdapter.handle_webhook` so the route stays a
    thin lookup-+-dispatch.
    """
    api = APIRouter(prefix="/wechat", tags=["wechat-official"])

    async def _dispatch(bot_name: str, request: Request) -> Response:
        adapter = WECHAT_BOT_REGISTRY.get(bot_name)
        if adapter is None:
            return PlainTextResponse(
                f"no wechat bot registered as {bot_name!r}",
                status_code=404,
            )
        return await adapter.handle_webhook(request)

    @api.get("/{bot_name}")
    async def wechat_verify(bot_name: str, request: Request) -> Response:
        """URL-verify handshake. WeChat sends ``GET ?echostr=...``."""
        return await _dispatch(bot_name, request)

    @api.post("/{bot_name}")
    async def wechat_message(bot_name: str, request: Request) -> Response:
        """Inbound message webhook. XML in, XML (or empty 200) out."""
        return await _dispatch(bot_name, request)

    return api
