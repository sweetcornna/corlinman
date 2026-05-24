"""Tencent QQ 官方机器人 REST sender — outbound reply surface.

Companion to :mod:`corlinman_channels.qq_official`. The adapter owns
the inbound gateway + the access-token lifecycle; the sender takes a
``token_provider`` async callable (typically
:meth:`QqOfficialAdapter.access_token`) so it always sees a fresh
token without duplicating refresh state.

## Endpoints used

The 官方 platform splits send endpoints by addressee kind:

* **频道 (guild) channel message** —
  ``POST /channels/{channel_id}/messages``.
* **群@机器人** —
  ``POST /v2/groups/{group_openid}/messages``.
* **C2C 私信** —
  ``POST /v2/users/{openid}/messages``.

## Passive-reply window (5 minutes)

Every reply MUST carry the inbound ``msg_id`` (or push ``event_id``)
within 5 minutes of receipt or the platform rejects with
``code 22009`` ("passive reply window expired"). The adapter
:class:`InboundEvent.message_id` carries the right id; the channel
handler threads it through to every send call.

## Image / file attachments

For each endpoint:

* **频道**: ``msg_type=7`` with a ``image`` URL or a multipart upload
  to ``/channels/{id}/files``.
* **C2C / 群**: must FIRST upload via
  ``/v2/users/{openid}/files`` or ``/v2/groups/{gid}/files`` which
  returns a ``file_info`` token; THEN ``send`` references the token
  via ``msg_type=7 (image)`` + ``media.file_info``. The platform
  **does not support direct file (document/audio/video) push** for
  the C2C / 群 path as of 2024-Q4 — the channel handler renders a
  human-readable status text instead.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from corlinman_channels.common import ConfigError, TransportError

_log = logging.getLogger(__name__)

__all__ = [
    "FILE_TYPE_IMAGE",
    "MSG_TYPE_ARK",
    "MSG_TYPE_IMAGE",
    "MSG_TYPE_MARKDOWN",
    "MSG_TYPE_RICH_MEDIA",
    "MSG_TYPE_TEXT",
    "QqOfficialSender",
]

# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

#: ``msg_type=0`` — plain text. Default for ``send_*_text``.
MSG_TYPE_TEXT: int = 0

#: ``msg_type=1`` — markdown. Channel-only.
MSG_TYPE_MARKDOWN: int = 2

#: ``msg_type=4`` — ark template card. Channel-only.
MSG_TYPE_ARK: int = 3

#: ``msg_type=7`` — rich media (image). Used by both guild and the
#: C2C / group ``v2`` endpoints.
MSG_TYPE_IMAGE: int = 7

#: Alias for richer media (image, video, audio, file) — same int as
#: :data:`MSG_TYPE_IMAGE` but kept under a clearer name for callers
#: that pre-upload via ``file_info``.
MSG_TYPE_RICH_MEDIA: int = 7

#: ``file_type=1`` — image. The QQ Official ``file_info`` upload uses
#: an integer file-type discriminator; image is the only kind safely
#: supported across all three send endpoints today.
FILE_TYPE_IMAGE: int = 1

# Type alias for the token provider — async callable returning the
# current access token. The adapter's :meth:`access_token` satisfies it.
TokenProvider = Callable[[], Awaitable[str]]


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------


class QqOfficialSender:
    """Thin client over the 官方 QQ Bot REST surface, scoped to outbound.

    Parallel to :class:`corlinman_channels.feishu.FeishuSender`: the
    sender holds an :class:`httpx.AsyncClient` + a token provider that
    yields a current access token. Construct once per bot and reuse —
    the underlying connection pool + token cache are the real cost.

    All ``send_*`` methods accept either ``msg_id`` (inbound message
    id) or ``event_id`` (push event id) so the platform's 5-minute
    passive-reply window is honoured. Without one the platform
    rejects the call with ``code 22009``.
    """

    __slots__ = ("api_base", "app_id", "client", "token_provider")

    def __init__(
        self,
        client: httpx.AsyncClient,
        token_provider: TokenProvider,
        app_id: str,
        api_base: str = "https://api.sgroup.qq.com",
    ) -> None:
        if not app_id:
            raise ConfigError("QqOfficialSender.app_id is empty")
        if token_provider is None:
            raise ConfigError("QqOfficialSender.token_provider is required")
        self.client = client
        self.token_provider = token_provider
        self.app_id = app_id
        self.api_base = api_base

    # ------------------------------------------------------------------
    # Internal HTTP helper
    # ------------------------------------------------------------------

    async def _post_json(
        self, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST ``body`` as JSON to ``path``, returning the parsed envelope.

        Raises :class:`TransportError` on transport / HTTP / decode
        failure. The body is logged at DEBUG so production deploys
        can correlate per-message envelopes when chasing rate-limit
        rejections.
        """
        token = await self.token_provider()
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Union-Appid": self.app_id,
        }
        try:
            resp = await self.client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TransportError(f"qq_official POST {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            text = resp.text[:200] if hasattr(resp, "text") else ""
            raise TransportError(
                f"qq_official POST {path} HTTP {resp.status_code}: {text}"
            )
        try:
            env = resp.json()
        except ValueError as exc:
            raise TransportError(
                f"qq_official POST {path} invalid JSON: {exc}"
            ) from exc
        if not isinstance(env, dict):
            raise TransportError(
                f"qq_official POST {path} response was not a JSON object"
            )
        return env

    @staticmethod
    def _reply_ids(
        msg_id: str | None, event_id: str | None
    ) -> dict[str, Any]:
        """Build the ``msg_id`` / ``event_id`` keys for a reply body.

        Strict precedence: caller-supplied ``msg_id`` wins. If neither
        is set, the body carries nothing — the platform will reject
        with ``22009`` and the channel handler logs / surfaces.
        """
        out: dict[str, Any] = {}
        if msg_id:
            out["msg_id"] = msg_id
        elif event_id:
            out["event_id"] = event_id
        return out

    # ==================================================================
    # 频道 (Guild channel) — POST /channels/{channel_id}/messages
    # ==================================================================

    async def send_text(
        self,
        channel_id: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Send a plain-text message into a 频道 channel.

        Returns the new message id (empty string when the platform
        omits one — some passive replies don't echo it back).
        """
        body: dict[str, Any] = {"content": content, "msg_type": MSG_TYPE_TEXT}
        body.update(self._reply_ids(msg_id, event_id))
        env = await self._post_json(f"/channels/{channel_id}/messages", body)
        return str(env.get("id", "")) if isinstance(env, dict) else ""

    async def send_image(
        self,
        channel_id: str,
        image_url: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
        content: str = "",
    ) -> str:
        """Send an image into a 频道 channel.

        The 频道 path takes a public HTTPS ``image`` URL directly — no
        pre-upload needed. ``content`` is an optional caption.
        """
        body: dict[str, Any] = {
            "msg_type": MSG_TYPE_TEXT,
            "image": image_url,
        }
        if content:
            body["content"] = content
        body.update(self._reply_ids(msg_id, event_id))
        env = await self._post_json(f"/channels/{channel_id}/messages", body)
        return str(env.get("id", "")) if isinstance(env, dict) else ""

    # ==================================================================
    # 群@机器人 — POST /v2/groups/{group_openid}/messages
    # ==================================================================

    async def send_group_text(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Send a plain-text message to a QQ group via the @机器人 path.

        Returns the new message id from the response envelope.
        """
        body: dict[str, Any] = {
            "content": content,
            "msg_type": MSG_TYPE_TEXT,
        }
        body.update(self._reply_ids(msg_id, event_id))
        env = await self._post_json(
            f"/v2/groups/{group_openid}/messages", body
        )
        return str(env.get("id", "")) if isinstance(env, dict) else ""

    async def send_group_image(
        self,
        group_openid: str,
        file_info: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
        content: str = "",
    ) -> str:
        """Send an image message to a QQ group via @机器人.

        ``file_info`` is the opaque token returned by
        :meth:`upload_group_image` (the 群 path doesn't accept a raw
        URL — Tencent gates this behind a pre-upload to their CDN).
        """
        body: dict[str, Any] = {
            "msg_type": MSG_TYPE_RICH_MEDIA,
            "media": {"file_info": file_info},
        }
        if content:
            body["content"] = content
        body.update(self._reply_ids(msg_id, event_id))
        env = await self._post_json(
            f"/v2/groups/{group_openid}/messages", body
        )
        return str(env.get("id", "")) if isinstance(env, dict) else ""

    # ==================================================================
    # C2C 私信 — POST /v2/users/{openid}/messages
    # ==================================================================

    async def send_c2c_text(
        self,
        openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Send a plain-text DM (C2C) to a QQ user by openid."""
        body: dict[str, Any] = {
            "content": content,
            "msg_type": MSG_TYPE_TEXT,
        }
        body.update(self._reply_ids(msg_id, event_id))
        env = await self._post_json(
            f"/v2/users/{openid}/messages", body
        )
        return str(env.get("id", "")) if isinstance(env, dict) else ""

    async def send_c2c_image(
        self,
        openid: str,
        file_info: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
        content: str = "",
    ) -> str:
        """Send an image DM to a QQ user by openid.

        ``file_info`` is the opaque token returned by
        :meth:`upload_c2c_image` — same pre-upload requirement as the
        group path.
        """
        body: dict[str, Any] = {
            "msg_type": MSG_TYPE_RICH_MEDIA,
            "media": {"file_info": file_info},
        }
        if content:
            body["content"] = content
        body.update(self._reply_ids(msg_id, event_id))
        env = await self._post_json(
            f"/v2/users/{openid}/messages", body
        )
        return str(env.get("id", "")) if isinstance(env, dict) else ""

    # ==================================================================
    # File uploads (pre-upload for image responses)
    # ==================================================================

    async def upload_group_image(
        self,
        group_openid: str,
        *,
        url: str | None = None,
        file_data: bytes | None = None,
        srv_send_msg: bool = False,
    ) -> str:
        """Upload an image to the 群 file CDN, returning a ``file_info``.

        Either ``url`` (HTTPS, public) or ``file_data`` (base64-encoded
        bytes) must be supplied. The returned ``file_info`` is opaque
        and short-lived; pair it with :meth:`send_group_image` to ship
        the actual message.

        ``srv_send_msg=True`` would tell the platform to send the
        message on our behalf; we always pre-upload + then send so the
        channel handler controls the ``msg_id`` threading.
        """
        body = self._build_upload_body(
            file_type=FILE_TYPE_IMAGE,
            url=url,
            file_data=file_data,
            srv_send_msg=srv_send_msg,
        )
        env = await self._post_json(
            f"/v2/groups/{group_openid}/files", body
        )
        return _extract_file_info(env, path="groups files upload")

    async def upload_c2c_image(
        self,
        openid: str,
        *,
        url: str | None = None,
        file_data: bytes | None = None,
        srv_send_msg: bool = False,
    ) -> str:
        """Upload an image to the C2C file CDN, returning a ``file_info``.

        Same contract as :meth:`upload_group_image` but for the
        single-user DM endpoint.
        """
        body = self._build_upload_body(
            file_type=FILE_TYPE_IMAGE,
            url=url,
            file_data=file_data,
            srv_send_msg=srv_send_msg,
        )
        env = await self._post_json(
            f"/v2/users/{openid}/files", body
        )
        return _extract_file_info(env, path="users files upload")

    async def upload_image(
        self,
        *,
        group_openid: str | None = None,
        openid: str | None = None,
        url: str | None = None,
        file_data: bytes | None = None,
    ) -> str:
        """Convenience dispatcher for image pre-upload.

        Exactly one of ``group_openid`` / ``openid`` must be supplied;
        the call routes to :meth:`upload_group_image` or
        :meth:`upload_c2c_image` accordingly. Returns the
        ``file_info`` token.
        """
        if (group_openid is None) == (openid is None):
            raise ValueError(
                "upload_image: exactly one of group_openid / openid required"
            )
        if group_openid is not None:
            return await self.upload_group_image(
                group_openid, url=url, file_data=file_data
            )
        assert openid is not None  # for type-checkers
        return await self.upload_c2c_image(
            openid, url=url, file_data=file_data
        )

    # ------------------------------------------------------------------
    # Upload-body helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_upload_body(
        *,
        file_type: int,
        url: str | None,
        file_data: bytes | None,
        srv_send_msg: bool,
    ) -> dict[str, Any]:
        """Assemble the shared ``files`` POST body.

        Caller supplies *either* ``url`` (the platform fetches it) or
        ``file_data`` (base64 of raw bytes). Mixing is a config bug —
        we raise so the channel handler doesn't silently send the wrong
        media.
        """
        import base64

        if (url is None) == (file_data is None):
            raise ValueError(
                "upload requires exactly one of url / file_data"
            )
        body: dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": bool(srv_send_msg),
        }
        if url is not None:
            body["url"] = url
        else:
            assert file_data is not None
            body["file_data"] = base64.b64encode(file_data).decode("ascii")
        return body


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _extract_file_info(env: dict[str, Any], *, path: str) -> str:
    """Pull the ``file_info`` token out of an upload-response envelope.

    The 官方 platform returns it under ``file_info`` at the top level;
    some sandbox deployments nest it under ``data``. We accept both
    and raise :class:`TransportError` when neither is present.
    """
    if "file_info" in env:
        token = env.get("file_info")
    else:
        data = env.get("data") or {}
        token = data.get("file_info") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise TransportError(
            f"qq_official {path} returned no file_info: {env}"
        )
    return token


def guess_image_mime(path: Path | str) -> str:
    """Best-effort image MIME from a filename.

    Used by the channel handler to short-circuit non-image attachments
    (the QQ Official platform doesn't support free-form file uploads
    via the v2 group / C2C endpoints).
    """
    name = str(path)
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


# Pre-import ``json`` for tests that monkeypatch the module-level name;
# the actual senders use the stdlib json directly through ``httpx``.
_ = json  # keep import alive for downstream test patches
