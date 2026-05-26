"""OneBot v11 HTTP client — credential bridge for QZone publishing (W5).

Backs PLAN_PERSONA_STUDIO W5.1. Talks to the SAME NapCat/Lagrange
instance the QQ channel already uses (``channels.qq.ws_url``-derived
HTTP endpoint), but uses the OneBot v11 *HTTP* API rather than the
WebSocket transport — the only consumers (``qzone_publish``) need
short, low-frequency request/reply round-trips and HTTP keeps the
client trivially testable via :class:`httpx.MockTransport`.

Public surface
--------------
* :class:`OneBotClient` — async httpx client with the three actions
  ``qzone_publish`` needs: ``fetch_login_info`` / ``fetch_cookies`` /
  ``fetch_csrf_token``.
* :class:`OneBotError` — :class:`RuntimeError` subclass raised on
  non-``status:"ok"`` responses or transport failures.
"""

from __future__ import annotations

from corlinman_agent.onebot.client import OneBotClient, OneBotError

__all__ = [
    "OneBotClient",
    "OneBotError",
]
