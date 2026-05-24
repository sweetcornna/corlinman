"""微信公众号 (WeChat Official Account) inbound adapter — webhook only.

WeChat Official Account is the consumer-facing Tencent messaging surface
documented at `https://developers.weixin.qq.com/doc/offiaccount/`. Unlike
QQ / Telegram / Feishu it does **not** expose a long-poll or WebSocket
event stream — every inbound message arrives as an HTTPS POST from
Tencent's edge to a publicly-reachable URL the operator configures in
the developer console.

## Transport — webhook only

::

    Tencent edge ── POST /wechat/<bot_name> ──► gateway
        sha1(token + timestamp + nonce) == signature
        body = XML envelope (ToUserName/FromUserName/MsgType/Content/...)

The webhook handshake (``GET ?echostr=...``) and the message POST share
one URL. Signature verification is :func:`verify_signature` (constant-time
SHA-1 of the sorted ``token + timestamp + nonce`` triple) per
`Access_Overview.html`_.

.. _Access_Overview.html: https://developers.weixin.qq.com/doc/offiaccount/Basic_Information/Access_Overview.html

## Reply paths — passive vs customer-service

WeChat lets a developer answer in one of two ways:

1. **Passive reply** within 5 s — return an XML envelope in the webhook
   HTTP body. Limited to ONE message, but the user sees it instantly.
2. **Customer-service message** within 48 h — push via
   ``/cgi-bin/message/custom/send`` using an ``access_token`` derived from
   ``appid + appsecret``. Multiple messages allowed; supports text /
   image / voice / video / news / template.

This adapter implements a **passive-first, customer-service-fallback**
flow:

* the webhook routes the inbound to the agent loop and waits on a
  per-user :class:`asyncio.Future` for up to
  ``CORLINMAN_WECHAT_PASSIVE_TIMEOUT_S`` (default 4.5 s);
* if the agent resolves the future in time → return the XML passively;
* otherwise the webhook returns the empty 200 "please wait" sentinel and
  the agent later pushes the reply via
  :class:`WeChatOfficialSender.send_text_customer`.

This trades latency for completeness: short answers feel instant; long
answers still land, just over the customer-service channel.

## AES encryption — TODO

WeChat's "安全模式" / "兼容模式" wraps message bodies in AES-CBC using
the ``encoding_aes_key``. v1 does **not** implement encryption — when
``encoding_aes_key`` is configured :class:`WeChatOfficialAdapter` raises
:class:`NotImplementedError`. Operators must run the console-side
"明文模式" (plaintext) for now. Adding AES is straightforward
(``Crypto.Cipher.AES`` + PKCS7) but kept out of v1 to keep the
crypto-review surface small.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ConfigError,
    InboundEvent,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = [
    "DEFAULT_PASSIVE_TIMEOUT_S",
    "PASSIVE_TIMEOUT_ENV",
    "WeChatOfficialAdapter",
    "WeChatOfficialConfig",
    "build_passive_xml",
    "parse_wechat_xml",
    "verify_signature",
]

_log = logging.getLogger(__name__)

#: Env var that overrides the per-event passive-reply wait deadline.
PASSIVE_TIMEOUT_ENV: str = "CORLINMAN_WECHAT_PASSIVE_TIMEOUT_S"

#: Default passive-reply deadline. WeChat enforces a hard 5-second limit
#: on the HTTP response; we shave half a second so the XML actually
#: reaches Tencent before the timer fires.
DEFAULT_PASSIVE_TIMEOUT_S: float = 4.5


# ===========================================================================
# Config
# ===========================================================================


@dataclass(slots=True)
class WeChatOfficialConfig:
    """Configuration for :class:`WeChatOfficialAdapter`.

    * ``app_id`` / ``app_secret`` — used by :class:`WeChatOfficialSender`
      to mint ``access_token``\\ s for the customer-service push path.
      Required for any reply (the adapter itself only needs ``token``
      for signature verify, but a reply-less adapter is not useful).
    * ``token`` — the developer-console "Token" string the signature is
      derived from. Required.
    * ``encoding_aes_key`` — optional. If set, AES message encryption is
      enabled console-side; v1 does NOT decrypt — the adapter raises
      :class:`NotImplementedError` on construction. Leave empty for v1.
    * ``passive_timeout_s`` — per-event wait deadline; overrides the env
      var when explicitly set (>0).
    """

    app_id: str
    app_secret: str
    token: str
    encoding_aes_key: str = ""
    passive_timeout_s: float = 0.0


# ===========================================================================
# Signature verification — pure helper, unit-tested directly.
# ===========================================================================


def verify_signature(
    token: str,
    timestamp: str,
    nonce: str,
    signature: str,
) -> bool:
    """Verify a WeChat webhook signature.

    The Tencent edge computes ``sha1(sorted_join(token, timestamp,
    nonce))`` and sends the hex digest as ``signature``. We sort the
    three strings, concatenate without separator, hash with SHA-1, and
    constant-time compare against the supplied digest.

    Returns ``True`` on match, ``False`` on any failure including
    missing inputs. Never raises — callers turn ``False`` into a 401.
    """
    if not token or not timestamp or not nonce or not signature:
        return False
    parts = sorted([token, timestamp, nonce])
    digest = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
    # constant-time compare via hmac would force a bytes import; the
    # length is fixed (40 chars) so a straight ``==`` is safe enough
    # against timing attacks here — the secret is the token, not the
    # signature.
    if len(digest) != len(signature):
        return False
    mismatch = 0
    for a, b in zip(digest, signature.lower(), strict=False):
        mismatch |= ord(a) ^ ord(b)
    return mismatch == 0


# ===========================================================================
# XML parsing — pure helper, unit-tested directly.
# ===========================================================================


def parse_wechat_xml(body: bytes) -> dict[str, str]:
    """Decode a WeChat inbound XML envelope into a flat ``dict[str, str]``.

    WeChat ships every inbound message — text, image, voice, event — as
    an XML document with the same top-level shape:

    .. code-block:: xml

        <xml>
          <ToUserName><![CDATA[gh_xxx]]></ToUserName>
          <FromUserName><![CDATA[oxxx]]></FromUserName>
          <CreateTime>1700000000</CreateTime>
          <MsgType><![CDATA[text]]></MsgType>
          <Content><![CDATA[hello]]></Content>
          <MsgId>1234567890</MsgId>
        </xml>

    The parser flattens every direct child of ``<xml>`` to a string —
    nested elements (unusual for WeChat) are XML-serialised back so
    callers can re-decode if they care. CDATA wrappers are stripped by
    ``ElementTree`` automatically.

    Raises :class:`ValueError` on malformed XML so the webhook can return
    400. An empty body returns an empty dict (caller decides how to
    handle the no-op).
    """
    if not body:
        return {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"wechat xml parse failed: {exc}") from exc
    if root.tag != "xml":
        raise ValueError(f"wechat xml root is {root.tag!r}, expected 'xml'")
    out: dict[str, str] = {}
    for child in root:
        if len(child) == 0:
            out[child.tag] = (child.text or "").strip()
        else:
            # Rare nested element — fold to inner XML string.
            out[child.tag] = ET.tostring(child, encoding="unicode")
    return out


# ===========================================================================
# Passive-reply XML builders
# ===========================================================================


def build_passive_xml(
    *,
    to_user: str,
    from_user: str,
    content: str,
    create_time: int | None = None,
) -> bytes:
    """Build a passive-reply ``MsgType=text`` XML envelope.

    Per WeChat's protocol the ``ToUserName`` / ``FromUserName`` are
    swapped relative to the inbound message — the reply goes from the
    official account (``from_user`` in the reply, which was
    ``ToUserName`` inbound) back to the original sender. CDATA wrapping
    is mandatory for string fields per the WeChat parser quirks.

    Returns the raw bytes ready to drop into the HTTP response body.
    """
    ts = create_time if create_time is not None else int(time.time())
    safe_content = content.replace("]]>", "]]]]><![CDATA[>")
    xml = (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{safe_content}]]></Content>"
        "</xml>"
    )
    return xml.encode("utf-8")


# ===========================================================================
# Adapter
# ===========================================================================


class WeChatOfficialAdapter:
    """WeChat Official Account webhook adapter.

    Unlike the other adapters in this package (which yield events from a
    long-poll / WebSocket via ``inbound()``) this adapter is **driven by
    inbound HTTP requests**. The gateway mounts :meth:`handle_webhook`
    on a public route per bot; each request runs one full agent turn,
    blocking on a per-sender :class:`asyncio.Future` for the passive
    deadline, then either returning the XML inline or falling back to
    the customer-service push path.

    Lifecycle:

    * construct with :class:`WeChatOfficialConfig`;
    * mount :meth:`handle_webhook` (one route per bot);
    * the channel runtime keeps :func:`run_wechat_official_channel`
      alive as a no-op idle loop so cancellation lines up with the
      other channels.
    """

    #: Background turn tasks the webhook spawned. Kept alive so the
    #: asyncio loop's weakref-based task GC doesn't drop a still-running
    #: ``self._on_event`` coroutine while the webhook waits on its
    #: passive future. Tasks self-remove via ``add_done_callback``.
    _bg_tasks: set[Any]

    def __init__(
        self,
        config: WeChatOfficialConfig,
        *,
        on_event: Any = None,
    ) -> None:
        if not config.token:
            raise ConfigError("WeChatOfficialConfig.token is empty")
        if not config.app_id:
            raise ConfigError("WeChatOfficialConfig.app_id is empty")
        if not config.app_secret:
            raise ConfigError("WeChatOfficialConfig.app_secret is empty")
        if config.encoding_aes_key:
            # TODO: AES-CBC + PKCS7 decryption per the "安全模式" doc.
            # Tracking ticket: WeChat AES message encryption (v1 gap).
            raise NotImplementedError(
                "WeChat Official Account AES message encryption is not "
                "implemented in v1 — set the developer console to 明文模式 "
                "(plaintext) or leave WeChatOfficialConfig.encoding_aes_key "
                "empty. AES support tracked separately."
            )
        self._cfg = config
        # Per-sender single-shot futures the agent loop resolves with a
        # short reply (the first sentence) before the passive deadline.
        self._passive_futures: dict[str, asyncio.Future[str]] = {}
        # Optional inbound sink the runtime sets — invoked once per
        # accepted message. Signature: ``async fn(inbound, passive_future)``.
        self._on_event = on_event
        self._bg_tasks = set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> WeChatOfficialConfig:
        return self._cfg

    @property
    def passive_timeout_s(self) -> float:
        """Resolve the per-event wait deadline.

        Precedence: explicit ``WeChatOfficialConfig.passive_timeout_s``
        (when > 0) → env :data:`PASSIVE_TIMEOUT_ENV` (when parseable) →
        :data:`DEFAULT_PASSIVE_TIMEOUT_S`. Values < 0.5 are clamped to
        0.5 so the agent has *some* chance to resolve before the HTTP
        response fires.
        """
        cfg_value = self._cfg.passive_timeout_s
        if cfg_value and cfg_value > 0:
            return max(0.5, float(cfg_value))
        raw = os.environ.get(PASSIVE_TIMEOUT_ENV, "")
        try:
            if raw:
                return max(0.5, float(raw))
        except ValueError:
            pass
        return DEFAULT_PASSIVE_TIMEOUT_S

    def set_on_event(self, on_event: Any) -> None:
        """Wire the inbound sink. Called by :func:`run_wechat_official_channel`."""
        self._on_event = on_event

    # ------------------------------------------------------------------
    # Passive-reply future map
    # ------------------------------------------------------------------

    def resolve_passive(self, from_user: str, content: str) -> bool:
        """Resolve the pending future for ``from_user`` with ``content``.

        Returns ``True`` when a future was actually pending (the agent
        beat the deadline), ``False`` otherwise. Idempotent — once a
        future is set or cancelled the next call is a no-op.
        """
        fut = self._passive_futures.pop(from_user, None)
        if fut is None or fut.done():
            return False
        fut.set_result(content)
        return True

    # ------------------------------------------------------------------
    # Webhook handler
    # ------------------------------------------------------------------

    async def handle_webhook(self, request: Request) -> Response:
        """Drive one WeChat webhook request end-to-end.

        Flow:

        1. Verify signature against the configured token. Mismatch → 401.
        2. ``GET ?echostr=...`` (the verify-URL handshake) → echo back.
        3. ``POST`` body → parse XML → build :class:`InboundEvent` →
           hand to the runtime sink (``on_event``) and wait up to
           :attr:`passive_timeout_s` for a passive resolution.
        4. On timeout, return an empty 200 — Tencent's silent "please
           wait" path. The agent later pushes the full reply via the
           customer-service path.

        Lazy-imports starlette types so :mod:`corlinman_channels` keeps
        zero web-framework deps at module load time.
        """
        from starlette.responses import PlainTextResponse
        from starlette.responses import Response as _Response

        params = request.query_params
        signature = params.get("signature", "") or params.get("msg_signature", "")
        timestamp = params.get("timestamp", "")
        nonce = params.get("nonce", "")
        echostr = params.get("echostr", "")

        if not verify_signature(self._cfg.token, timestamp, nonce, signature):
            _log.warning(
                "wechat_official signature mismatch ts=%s nonce=%s",
                timestamp, nonce,
            )
            return PlainTextResponse("forbidden", status_code=401)

        # Console URL-verify handshake. WeChat sends a GET with echostr
        # and expects the raw string echoed back as the response body.
        if request.method == "GET":
            if echostr:
                return PlainTextResponse(echostr)
            return PlainTextResponse("ok")

        # POST — parse the XML envelope.
        try:
            body = await request.body()
        except Exception as exc:
            _log.warning("wechat_official body read failed: %s", exc)
            return PlainTextResponse("", status_code=200)

        try:
            fields = parse_wechat_xml(body)
        except ValueError as exc:
            _log.warning("wechat_official bad xml: %s", exc)
            return PlainTextResponse("", status_code=200)

        if not fields:
            return PlainTextResponse("", status_code=200)

        inbound = _build_inbound_event(fields)
        if inbound is None:
            # MsgType we don't surface (e.g. event/CLICK without a payload).
            return PlainTextResponse("", status_code=200)

        # Register the passive future BEFORE invoking the sink so the
        # sink's earliest possible ``resolve_passive`` call wins the
        # race. Map key is the sender openid (``FromUserName``).
        from_user = inbound.binding.sender
        to_user = inbound.binding.account
        loop = asyncio.get_running_loop()
        passive_future: asyncio.Future[str] = loop.create_future()
        self._passive_futures[from_user] = passive_future

        if self._on_event is not None:
            # Spawn the agent turn so the webhook itself doesn't block
            # on the full chat run — only on the passive timeout.
            task = asyncio.create_task(
                self._on_event(inbound, passive_future),
                name=f"wechat-turn-{from_user[:8]}",
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        try:
            content = await asyncio.wait_for(
                passive_future, timeout=self.passive_timeout_s
            )
        except TimeoutError:
            # Agent missed the deadline — return empty 200 so Tencent
            # stops the retry loop, and let the customer-service path
            # deliver the reply.
            _log.info(
                "wechat_official passive timeout user=%s (falling back to "
                "customer-service push)", from_user,
            )
            self._passive_futures.pop(from_user, None)
            return PlainTextResponse("", status_code=200)
        except asyncio.CancelledError:
            self._passive_futures.pop(from_user, None)
            raise
        finally:
            # Defensive: remove a future that fired so a slow retry
            # request doesn't keep the entry alive forever.
            self._passive_futures.pop(from_user, None)

        if not content:
            return PlainTextResponse("", status_code=200)

        xml = build_passive_xml(
            to_user=from_user,
            from_user=to_user,
            content=content,
            create_time=int(time.time()),
        )
        return _Response(
            content=xml,
            media_type="application/xml; charset=utf-8",
            status_code=200,
        )


# ===========================================================================
# InboundEvent construction
# ===========================================================================


def _build_inbound_event(fields: dict[str, str]) -> InboundEvent[dict[str, str]] | None:
    """Turn a parsed XML envelope into a normalized :class:`InboundEvent`.

    Returns ``None`` for envelopes we don't surface to the agent (e.g.
    ``MsgType=event`` of kinds the agent can't action like ``LOCATION``
    or ``CLICK`` without payload). Subscribe / unsubscribe events are
    surfaced so the agent can produce a welcome message.
    """
    msg_type = fields.get("MsgType", "").lower()
    from_user = fields.get("FromUserName", "")
    to_user = fields.get("ToUserName", "")
    if not from_user or not to_user:
        return None

    binding = ChannelBinding(
        channel="wechat_official",
        account=to_user,
        thread=from_user,  # 1:1 — thread keys per peer
        sender=from_user,
    )
    msg_id = fields.get("MsgId", "") or fields.get("FromUserName", "")
    ts_raw = fields.get("CreateTime", "0")
    try:
        timestamp = int(ts_raw)
    except ValueError:
        timestamp = 0

    text = ""
    attachments: list[Attachment] = []
    if msg_type == "text":
        text = fields.get("Content", "")
    elif msg_type == "image":
        pic_url = fields.get("PicUrl", "")
        attachments.append(
            Attachment(
                kind=AttachmentKind.IMAGE,
                url=pic_url or None,
                mime="image/*",
            )
        )
    elif msg_type == "voice":
        # WeChat exposes voice as a MediaId; download is operator-side.
        text = fields.get("Recognition", "")  # ASR when 语音识别 is on
        attachments.append(
            Attachment(
                kind=AttachmentKind.AUDIO,
                url=None,
                mime="audio/amr",
            )
        )
    elif msg_type == "video" or msg_type == "shortvideo":
        attachments.append(
            Attachment(kind=AttachmentKind.VIDEO, url=None, mime="video/*")
        )
    elif msg_type == "event":
        event = fields.get("Event", "").lower()
        if event == "subscribe":
            text = "[subscribe]"
        elif event == "unsubscribe":
            text = "[unsubscribe]"
        elif event == "click":
            text = fields.get("EventKey", "")
        else:
            # Unknown event — silently drop.
            return None
    else:
        # link / location / etc. — fold to empty text + raw payload.
        text = ""

    return InboundEvent(
        channel="wechat_official",
        binding=binding,
        text=text,
        message_id=msg_id or None,
        timestamp=timestamp,
        mentioned=True,  # every DM is implicitly addressed
        attachments=attachments,
        payload=fields,
    )


