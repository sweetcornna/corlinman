"""QQ空间 (QZone) read + comment builtin tools.

Companion to :mod:`corlinman_agent.qzone.publish`. Where ``publish`` only
*posts* 说说, this module *reads* the 好友动态 timeline and lets the bound
account *comment* — on its own posts (replying to incoming comments) and
on friends' posts.

Port of ``hermes-agent/tools/qzone_comment_tool.py`` adapted to the
corlinman shape:

* Async :class:`httpx.AsyncClient` instead of blocking ``urllib.request``.
* QQ login state (uin + cookies + g_tk) borrowed via
  :class:`~corlinman_agent.onebot.OneBotClient` rather than the old shared
  ``_onebot_call`` helper. Auth helpers (``_compute_gtk`` /
  ``_extract_cookie_value`` / ``_DESKTOP_UA`` / ``_QZONE_TIMEOUT`` /
  ``_QZONE_COOKIE_DOMAIN``) are reused from the ``publish`` module so the
  two tools stay bit-for-bit consistent.
* Every failure folds into the canonical ``{"ok": false, "error": …,
  "message": …}`` envelope — the dispatchers never raise.

Four tools:

* ``qzone_list_feed``   — read the 好友动态 timeline (recent 说说 by the
  bound account + its friends), each with author, text, tid, and inline
  comments. Optional ``owner_uin`` filters to one author.
* ``qzone_get_post``    — pull one post (by tid) out of the timeline with
  its full comment list.
* ``qzone_post_comment``— comment under any 说说 (top-level, or a reply to
  a specific commenter).
* ``qzone_list_friends``— the QQ friend list (uin + nickname + remark) via
  the OneBot ``get_friend_list`` action.

Endpoint choice (learned the hard way in hermes): ``emotion_cgi_msglist_v6``
rejects automated reads with ``-10000 使用人数过多`` because it demands a
JS-generated ``qzonetoken`` absent from the borrowed cookie jar. The
unified feed CGI ``feeds3_html_more`` does NOT need it and returns the
timeline fine with the same g_tk, so the read path is built on that. It
returns a JS-object-literal blob whose per-feed ``html:'…'`` fields hold
JS-escaped rendered HTML; we unescape and regex out the structured bits.
Brittle by nature — when Tencent changes the markup the raw response is
surfaced so the break is diagnosable from the logs.
"""

from __future__ import annotations

import contextlib
import html as _html
import json
import re
from typing import Any

import httpx
import structlog

from corlinman_agent.onebot import OneBotClient, OneBotError
from corlinman_agent.qzone.publish import (
    _DESKTOP_UA,
    _QZONE_COOKIE_DOMAIN,
    _QZONE_TIMEOUT,
    _compute_gtk,
    _extract_cookie_value,
)

logger = structlog.get_logger(__name__)


__all__ = [
    "QZONE_COMMENT_TOOLS",
    "QZONE_GET_POST_TOOL",
    "QZONE_LIST_FEED_TOOL",
    "QZONE_LIST_FRIENDS_TOOL",
    "QZONE_POST_COMMENT_TOOL",
    "dispatch_qzone_get_post",
    "dispatch_qzone_list_feed",
    "dispatch_qzone_list_friends",
    "dispatch_qzone_post_comment",
    "qzone_comment_tool_schemas",
    "qzone_get_post_tool_schema",
    "qzone_list_feed_tool_schema",
    "qzone_list_friends_tool_schema",
    "qzone_post_comment_tool_schema",
]


#: Wire-stable tool names. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin`` switch.
QZONE_LIST_FEED_TOOL: str = "qzone_list_feed"
QZONE_GET_POST_TOOL: str = "qzone_get_post"
QZONE_POST_COMMENT_TOOL: str = "qzone_post_comment"
QZONE_LIST_FRIENDS_TOOL: str = "qzone_list_friends"

QZONE_COMMENT_TOOLS: frozenset[str] = frozenset(
    {
        QZONE_LIST_FEED_TOOL,
        QZONE_GET_POST_TOOL,
        QZONE_POST_COMMENT_TOOL,
        QZONE_LIST_FRIENDS_TOOL,
    }
)


# QZone web endpoints — fixed constants, never built from user input.

#: Unified 好友动态 feed. Works without a qzonetoken (unlike msglist_v6).
_QZONE_FEEDS3_URL: str = (
    "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com"
    "/cgi-bin/feeds/feeds3_html_more"
)
#: Post / reply to a comment. Handles both top-level comments and replies.
_QZONE_COMMENT_URL: str = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_re_feeds"
)

_DEFAULT_LIST_NUM: int = 10
_MAX_LIST_NUM: int = 40
_MAX_COMMENT_LEN: int = 1500


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _decode(args_json: bytes | str) -> dict[str, Any]:
    """Decode ``args_json`` into a dict; invalid payloads collapse to ``{}``."""
    raw: str
    if isinstance(args_json, (bytes, bytearray)):
        try:
            raw = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    else:
        raw = args_json or ""
    try:
        obj = json.loads(raw or "{}")
    except (ValueError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _err(error: str, message: str, **extra: Any) -> str:
    """Render a failure envelope. ``error`` is the wire-stable failure
    *code*; named ``error`` (not ``code``) so callers can attach a numeric
    ``code=…`` field via ``**extra`` without clobbering the positional —
    same convention as :mod:`corlinman_agent.qzone.publish`."""
    payload: dict[str, Any] = {"ok": False, "error": error, "message": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Auth (borrowed from OneBot, shared shape with qzone.publish)
# ---------------------------------------------------------------------------


async def _qzone_auth(client: OneBotClient) -> tuple[str, str, int]:
    """Return ``(my_uin, cookie_string, gtk)``.

    Raises :class:`OneBotError` when the QQ login state is unavailable and
    :class:`RuntimeError` when the QZone cookie jar lacks ``p_skey`` (login
    is stale / NapCat hasn't been granted QZone access).
    """
    login_info = await client.fetch_login_info()
    cookie = await client.fetch_cookies(_QZONE_COOKIE_DOMAIN)
    my_uin = str(login_info.get("qq") or login_info.get("user_id") or "").strip()
    if not my_uin:
        raise RuntimeError("OneBot login info missing user_id / qq")
    p_skey = _extract_cookie_value(cookie, "p_skey")
    if not p_skey:
        raise RuntimeError(
            "p_skey not found in OneBot cookies — the QQ login state may be "
            "stale or NapCat is not fully logged into QZone."
        )
    return my_uin, cookie, _compute_gtk(p_skey)


def _onebot_from(
    onebot_client: OneBotClient | None,
    onebot_client_factory: Any | None,
) -> tuple[OneBotClient, bool]:
    """Resolve a OneBot client + an ``own`` flag (True = caller must close).

    Mirrors :func:`corlinman_agent.qzone.publish.dispatch_qzone_publish`'s
    precedence: an injected client (tests / shared) wins, else a factory,
    else a fresh env-configured :class:`OneBotClient`.
    """
    if onebot_client is not None:
        return onebot_client, False
    if onebot_client_factory is not None:
        return onebot_client_factory(), True
    return OneBotClient(), True


# ---------------------------------------------------------------------------
# QZone HTTP (async)
# ---------------------------------------------------------------------------


async def _qzone_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    cookie: str,
    owner_uin: str,
) -> str:
    """GET a QZone endpoint, returning the decoded body text."""
    headers = {
        "Cookie": cookie,
        "Referer": f"https://user.qzone.qq.com/{owner_uin}",
        "User-Agent": _DESKTOP_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        resp = await client.get(
            url, params=params, headers=headers, timeout=_QZONE_TIMEOUT
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"QZone request timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Cannot reach QZone: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"QZone HTTP {resp.status_code}. {resp.text[:200]}".strip())
    return resp.text


async def _qzone_post(
    client: httpx.AsyncClient,
    url: str,
    form: dict[str, str],
    cookie: str,
    owner_uin: str,
) -> str:
    """POST form-encoded data to QZone, returning the decoded body text."""
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"https://user.qzone.qq.com/{owner_uin}",
        "User-Agent": _DESKTOP_UA,
    }
    try:
        resp = await client.post(
            url, data=form, headers=headers, timeout=_QZONE_TIMEOUT
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"QZone request timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Cannot reach QZone: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"QZone HTTP {resp.status_code}. {resp.text[:200]}".strip())
    return resp.text


def _qzone_http_client(http_transport: httpx.BaseTransport | None) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": _QZONE_TIMEOUT}
    if http_transport is not None:
        kwargs["transport"] = http_transport
    return httpx.AsyncClient(**kwargs)


# ---------------------------------------------------------------------------
# feeds3 timeline parsing (pure, unit-testable)
# ---------------------------------------------------------------------------

# feeds3 embeds rendered HTML as a JS string literal carrying JS escapes:
# ``\xNN`` / ``\uNNNN`` byte escapes AND the simple two-char escapes ``\/``
# ``\"`` ``\t`` ``\n`` ``\\`` etc. Tags arrive as ``<\/div>`` — if only the
# \xNN form is decoded, every text-node regex silently misses because it
# never sees a real ``</div>``. Decode the lot in one pass.
_JS_ESCAPE_RE = re.compile(r"""\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|[/"'\\tnrbf0])""")

_SIMPLE_JS_ESCAPES = {
    "/": "/", '"': '"', "'": "'", "\\": "\\",
    "t": "\t", "n": "\n", "r": "\r", "b": "", "f": "", "0": "",
}

_FEED_ROOT_RE = re.compile(r'<li class="f-single[^"]*"\s+id="fct_(\d+)_')

_COMMENT_ITEM_RE = re.compile(
    r'<li class="comments-item[^"]*"'
    r'[^>]*?data-tid="([^"]*)"'
    r'[^>]*?data-uin="(\d+)"'
    r'[^>]*?data-nick="([^"]*)"',
    re.DOTALL,
)


def _unescape_hex(s: str) -> str:
    """Decode the JS string escapes in a feeds3 ``html:'…'`` payload."""
    def _repl(m: re.Match[str]) -> str:
        g = m.group(1)
        if g[0] in ("x", "u"):
            return chr(int(g[1:], 16))
        return _SIMPLE_JS_ESCAPES.get(g, g)
    return _JS_ESCAPE_RE.sub(_repl, s)


def _strip_html_lite(text: str) -> str:
    """Reduce QZone inline HTML to readable plain text (lossy)."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text).strip()


def _parse_callback_json(body: str) -> dict[str, Any] | None:
    """Extract the JSON inside a ``frameElement.callback({…})`` shim.

    A naive ``{.*}`` search would match the ``try{…}`` block in the
    wrapper instead, so anchor on the ``callback(`` token specifically.
    """
    m = re.search(r"callback\(\s*(\{.*\})\s*\)\s*;?\s*</script>", body, re.DOTALL)
    if not m:
        m = re.search(r"callback\(\s*(\{.*?\})\s*\)", body, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _feed_author_nick(block: str) -> str:
    m = re.search(r'class="f-name q_namecard[^"]*"[^>]*>([^<]+)</a>', block)
    return _html.unescape(m.group(1).strip()) if m else ""


def _feed_tid(block: str) -> str:
    m = re.search(r'data-tid="([0-9a-fA-F]+)"', block)
    if m:
        return m.group(1)
    m = re.search(r'data-key="([0-9a-fA-F]+)"', block)
    return m.group(1) if m else ""


def _feed_content(block: str) -> str:
    m = re.search(r'<div class="f-info"[^>]*>(.*?)</div>', block, re.DOTALL)
    return _strip_html_lite(m.group(1)) if m else ""


def _feed_time(block: str) -> str:
    m = re.search(r'class="[^"]*\bstate\b[^"]*"[^>]*>\s*([^<]+?)\s*</span>', block)
    return m.group(1).strip() if m else ""


def _feed_comments(block: str) -> list[dict[str, str]]:
    """Pull comments out of a feed's comments-list HTML."""
    out: list[dict[str, str]] = []
    for m in _COMMENT_ITEM_RE.finditer(block):
        cid, cuin, cnick = m.group(1), m.group(2), _html.unescape(m.group(3))
        # Comment text follows the nickname anchor:
        # ``…>nick</a>&nbsp; : TEXT<div class="comments-op"``.
        after = block[m.end():m.end() + 2000]
        tm = re.search(
            r'</a>\s*(?:&nbsp;)?\s*[:：]\s*(.*?)<div class="comments-op"',
            after,
            re.DOTALL,
        )
        content = _strip_html_lite(tm.group(1)) if tm else ""
        out.append({"id": cid, "uin": cuin, "name": cnick, "content": content})
    return out


def _parse_feeds3(body: str) -> list[dict[str, Any]]:
    """Parse a feeds3_html_more response into a list of feed dicts."""
    text = _unescape_hex(body)
    starts = [(m.start(), m.group(1)) for m in _FEED_ROOT_RE.finditer(text)]
    feeds: list[dict[str, Any]] = []
    for i, (pos, uin) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        block = text[pos:end]
        tid = _feed_tid(block)
        if not tid:
            continue
        feeds.append(
            {
                "tid": tid,
                "uin": uin,
                "name": _feed_author_nick(block),
                "time": _feed_time(block),
                "content": _feed_content(block),
                "comments": _feed_comments(block),
            }
        )
    return feeds


async def _fetch_timeline(
    http_client: httpx.AsyncClient,
    my_uin: str,
    cookie: str,
    gtk: int,
    count: int,
) -> list[dict[str, Any]]:
    """Fetch + parse the 好友动态 timeline via feeds3_html_more."""
    params = {
        "uin": my_uin,
        "scope": "0",
        "view": "1",
        "filter": "all",
        "flag": "1",
        "applist": "all",
        "pagenum": "1",
        "count": str(count),
        "aisortEndTime": "0",
        "aisortOffset": "0",
        "begintime": "0",
        "format": "json",
        "g_tk": str(gtk),
        "useutf8": "1",
        "outputhtmlfeed": "1",
    }
    body = await _qzone_get(http_client, _QZONE_FEEDS3_URL, params, cookie, my_uin)
    if '"code":0' not in body and '"code": 0' not in body:
        m = re.search(
            r'"code"\s*:\s*(-?\d+).*?"message"\s*:\s*"([^"]*)"', body, re.DOTALL
        )
        if m:
            raise RuntimeError(
                f"feeds3 returned code={m.group(1)} message={m.group(2)!r}"
            )
        raise RuntimeError(f"feeds3 unexpected response: {body[:200]}")
    return _parse_feeds3(body)


# ---------------------------------------------------------------------------
# Dispatchers
# ---------------------------------------------------------------------------


async def dispatch_qzone_list_feed(
    *,
    args_json: bytes | str,
    onebot_client: OneBotClient | None = None,
    onebot_client_factory: Any | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> str:
    """``qzone_list_feed`` — read the 好友动态 timeline."""
    args = _decode(args_json)
    try:
        num = int(args.get("num") or _DEFAULT_LIST_NUM)
    except (TypeError, ValueError):
        return _err("invalid_args", f"'num' must be an integer 1..{_MAX_LIST_NUM}.")
    num = max(1, min(num, _MAX_LIST_NUM))

    owner_uin = str(args.get("owner_uin") or "").strip()
    if owner_uin and not owner_uin.isdigit():
        return _err("invalid_args", "'owner_uin' must be a numeric QQ if provided.")

    client, own = _onebot_from(onebot_client, onebot_client_factory)
    try:
        try:
            my_uin, cookie, gtk = await _qzone_auth(client)
        except OneBotError as exc:
            return _err("onebot_failed", str(exc))
        except RuntimeError as exc:
            return _err("qzone_cookie_stale", str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("qzone_list_feed.auth_unexpected")
            return _err("onebot_failed", f"could not borrow QQ login state: {exc}")
        fetch_count = num if not owner_uin else min(_MAX_LIST_NUM, max(num * 3, 20))
        async with _qzone_http_client(http_transport) as http_client:
            try:
                feeds = await _fetch_timeline(
                    http_client, my_uin, cookie, gtk, fetch_count
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("qzone_list_feed.read_failed", error=str(exc))
                return _err("qzone_read_failed", f"QZone feed read failed: {exc}")
    finally:
        if own:
            with contextlib.suppress(Exception):
                await client.aclose()

    if owner_uin:
        feeds = [f for f in feeds if f["uin"] == owner_uin]
    feeds = feeds[:num]
    return json.dumps(
        {
            "ok": True,
            "my_uin": my_uin,
            "filter_owner_uin": owner_uin or None,
            "returned": len(feeds),
            "feed": feeds,
            "note": (
                "feed = 好友动态时间线 (你和好友的最近说说). 每条有 uin/name/"
                "content/comments. uin==my_uin 的是你自己的说说(可回评论), "
                "其它是好友的(可去评论)."
            ),
        },
        ensure_ascii=False,
    )


async def dispatch_qzone_get_post(
    *,
    args_json: bytes | str,
    onebot_client: OneBotClient | None = None,
    onebot_client_factory: Any | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> str:
    """``qzone_get_post`` — find one post (by tid) in the timeline."""
    args = _decode(args_json)
    tid = (args.get("tid") or "").strip()
    if not tid:
        return _err("invalid_args", "'tid' is required.")

    client, own = _onebot_from(onebot_client, onebot_client_factory)
    try:
        try:
            my_uin, cookie, gtk = await _qzone_auth(client)
        except OneBotError as exc:
            return _err("onebot_failed", str(exc))
        except RuntimeError as exc:
            return _err("qzone_cookie_stale", str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("qzone_get_post.auth_unexpected")
            return _err("onebot_failed", f"could not borrow QQ login state: {exc}")
        async with _qzone_http_client(http_transport) as http_client:
            try:
                feeds = await _fetch_timeline(
                    http_client, my_uin, cookie, gtk, _MAX_LIST_NUM
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("qzone_get_post.read_failed", error=str(exc))
                return _err("qzone_read_failed", f"QZone feed read failed: {exc}")
    finally:
        if own:
            with contextlib.suppress(Exception):
                await client.aclose()

    for f in feeds:
        if f["tid"] == tid:
            return json.dumps({"ok": True, "found": True, "post": f}, ensure_ascii=False)
    return json.dumps(
        {
            "ok": True,
            "found": False,
            "note": (
                f"tid {tid} 不在当前时间线里(可能太旧或已滚出). list_feed 返回的 "
                "每条已经带完整 comments, 通常不需要再 get_post."
            ),
        },
        ensure_ascii=False,
    )


async def dispatch_qzone_post_comment(
    *,
    args_json: bytes | str,
    onebot_client: OneBotClient | None = None,
    onebot_client_factory: Any | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> str:
    """``qzone_post_comment`` — comment on a 说说 (top-level or @reply)."""
    args = _decode(args_json)
    content = (args.get("content") or "").strip()
    if not content:
        return _err("invalid_args", "'content' is required.")
    if len(content) > _MAX_COMMENT_LEN:
        return _err(
            "invalid_args", f"'content' must be under {_MAX_COMMENT_LEN} characters."
        )
    tid = (args.get("tid") or "").strip()
    if not tid:
        return _err("invalid_args", "'tid' is required (the 说说 id from list_feed).")
    owner_uin = (args.get("owner_uin") or "").strip()
    if not owner_uin or not owner_uin.isdigit():
        return _err("invalid_args", "'owner_uin' is required and must be a numeric QQ.")

    reply_to_uin = (args.get("reply_to_uin") or "").strip()
    reply_to_name = (args.get("reply_to_name") or "").strip()
    if reply_to_uin and not reply_to_uin.isdigit():
        return _err("invalid_args", "'reply_to_uin' must be a numeric QQ if provided.")

    client, own = _onebot_from(onebot_client, onebot_client_factory)
    try:
        try:
            my_uin, cookie, gtk = await _qzone_auth(client)
        except OneBotError as exc:
            return _err("onebot_failed", str(exc))
        except RuntimeError as exc:
            return _err("qzone_cookie_stale", str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("qzone_post_comment.auth_unexpected")
            return _err("onebot_failed", f"could not borrow QQ login state: {exc}")

        form = {
            "topicId": f"{owner_uin}_{tid}__1",
            "feedsType": "100",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "ref": "feeds",
            "content": content,
            "hostUin": owner_uin,
            "uin": my_uin,
            "format": "fs",
            "iNotice": "0",
            "private": "0",
            "paramstr": "1",
            "qzreferrer": f"https://user.qzone.qq.com/{owner_uin}",
        }
        if reply_to_uin:
            # Reply-to-commenter: the QZone web UI prepends an @nick token
            # and carries the target uin. Best-effort — top-level comments
            # are the primary, verified path.
            if reply_to_name:
                mention = f"@{{uin:{reply_to_uin},nick:{reply_to_name},who:1}} "
                if not content.startswith(mention.strip()):
                    form["content"] = mention + content
            form["targetUin"] = reply_to_uin

        url = f"{_QZONE_COMMENT_URL}?g_tk={gtk}"
        async with _qzone_http_client(http_transport) as http_client:
            try:
                body = await _qzone_post(http_client, url, form, cookie, owner_uin)
            except Exception as exc:  # noqa: BLE001
                logger.warning("qzone_post_comment.request_failed", error=str(exc))
                return _err("qzone_request_failed", f"QZone comment request failed: {exc}")
    finally:
        if own:
            with contextlib.suppress(Exception):
                await client.aclose()

    obj = _parse_callback_json(body)
    if obj is None:
        return _err("qzone_unparseable", f"unparseable comment response: {body[:200]!r}")
    code = obj.get("code") if obj.get("code") is not None else obj.get("ret")
    subcode = obj.get("subcode", 0)
    if code not in (0, None) or subcode not in (0, None):
        return _err(
            "qzone_rejected",
            f"QZone rejected the comment: code={code}, subcode={subcode}, "
            f"message={obj.get('message') or obj.get('msg')!r}",
            code=code,
        )
    return json.dumps(
        {
            "ok": True,
            "owner_uin": owner_uin,
            "tid": tid,
            "is_reply": bool(reply_to_uin),
            "content_sent": form["content"],
        },
        ensure_ascii=False,
    )


async def dispatch_qzone_list_friends(
    *,
    args_json: bytes | str,
    onebot_client: OneBotClient | None = None,
    onebot_client_factory: Any | None = None,
) -> str:
    """``qzone_list_friends`` — the QQ friend list via OneBot."""
    args = _decode(args_json)
    client, own = _onebot_from(onebot_client, onebot_client_factory)
    try:
        try:
            friends_raw = await client.fetch_friend_list()
        except OneBotError as exc:
            return _err("onebot_failed", f"OneBot get_friend_list failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("qzone_list_friends.unexpected")
            return _err("onebot_failed", f"OneBot get_friend_list failed: {exc}")
    finally:
        if own:
            with contextlib.suppress(Exception):
                await client.aclose()

    out = [
        {
            "uin": str(f.get("user_id") or ""),
            "nickname": f.get("nickname") or "",
            "remark": f.get("remark") or "",
        }
        for f in friends_raw
    ]
    name_filter = (args.get("filter") or "").strip().lower()
    if name_filter:
        out = [
            f
            for f in out
            if name_filter in f["nickname"].lower()
            or name_filter in f["remark"].lower()
            or name_filter in f["uin"]
        ]
    try:
        limit = int(args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))
    return json.dumps(
        {
            "ok": True,
            "total": len(out),
            "returned": min(len(out), limit),
            "friends": out[:limit],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def qzone_list_feed_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": QZONE_LIST_FEED_TOOL,
            "description": (
                "Read the QQ空间 好友动态 timeline — the recent 说说 posted "
                "by the bound account and its friends, newest first. Each "
                "item carries the author's uin + name, the post text, the "
                "post tid, and its comments (uin + name + content). Items "
                "where uin == your own QQ are your own posts (reply to "
                "their comments); other items are friends' posts (go "
                "comment on them). Pass `owner_uin` to filter to a single "
                "author."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner_uin": {
                        "type": "string",
                        "description": "Numeric QQ to filter the timeline to one author. Omit for the full timeline.",
                    },
                    "num": {
                        "type": "integer",
                        "description": f"How many timeline items to return (1..{_MAX_LIST_NUM}, default {_DEFAULT_LIST_NUM}).",
                        "minimum": 1,
                        "maximum": _MAX_LIST_NUM,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def qzone_get_post_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": QZONE_GET_POST_TOOL,
            "description": (
                "Find one 说说 (by tid) in the current 好友动态 timeline and "
                "return it with its full comment list. Usually unnecessary "
                "— `qzone_list_feed` already returns each post's comments "
                "inline. Use only when you have a tid and want to re-check "
                "its comments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tid": {
                        "type": "string",
                        "description": "The 说说's tid (from list_feed).",
                    },
                },
                "required": ["tid"],
                "additionalProperties": False,
            },
        },
    }


def qzone_post_comment_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": QZONE_POST_COMMENT_TOOL,
            "description": (
                "Post a comment under a 说说. Set `owner_uin` to your own "
                "QQ to reply to someone on your post; set it to a friend's "
                "QQ to comment on their post. `reply_to_uin` makes it a "
                "reply to that specific commenter (with @ mention); omit "
                "it for a top-level comment. Be selective — don't comment "
                "on everything, just what's worth engaging."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner_uin": {
                        "type": "string",
                        "description": "Numeric QQ of the 说说's owner.",
                    },
                    "tid": {"type": "string", "description": "The 说说's tid."},
                    "content": {
                        "type": "string",
                        "description": f"Comment body (under {_MAX_COMMENT_LEN} chars).",
                    },
                    "reply_to_uin": {
                        "type": "string",
                        "description": "Numeric QQ of the commenter being replied to. Omit for top-level comment.",
                    },
                    "reply_to_name": {
                        "type": "string",
                        "description": "Display name of the commenter being replied to, used for the @ mention.",
                    },
                },
                "required": ["owner_uin", "tid", "content"],
                "additionalProperties": False,
            },
        },
    }


def qzone_list_friends_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": QZONE_LIST_FRIENDS_TOOL,
            "description": (
                "List the bound account's QQ friends (uin + nickname + "
                "remark). Used to pick whose QZone to visit. Supports an "
                "optional substring `filter` over nickname / remark / uin."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Substring filter over nickname / remark / uin (case-insensitive).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Cap returned friends (1..500, default 50).",
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def qzone_comment_tool_schemas() -> list[dict[str, Any]]:
    """Return every qzone comment/read tool schema as a list."""
    return [
        qzone_list_feed_tool_schema(),
        qzone_get_post_tool_schema(),
        qzone_post_comment_tool_schema(),
        qzone_list_friends_tool_schema(),
    ]
