"""Tests for the ``qzone_*`` read + comment builtin tools.

Network is mocked via :class:`httpx.MockTransport` — one transport for the
OneBot HTTP API (login / cookies / friend list) and one for the QZone web
endpoints (feeds3 timeline GET + comment POST). The feeds3 parser is also
unit-tested directly against a JS-escaped sample blob.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from corlinman_agent.onebot import OneBotClient
from corlinman_agent.qzone import (
    dispatch_qzone_get_post,
    dispatch_qzone_list_feed,
    dispatch_qzone_list_friends,
    dispatch_qzone_post_comment,
    qzone_comment_tool_schemas,
)
from corlinman_agent.qzone.comment import (
    _parse_callback_json,
    _parse_feeds3,
    _unescape_hex,
)

_MY_UIN = "10001"
_FRIEND_UIN = "20002"
_QZONE_COOKIE = f"uin=o{_MY_UIN}; skey=@Skey1; p_skey=PKEY_ABCDEFGHIJK; pt4_token=T"

# A single JS-escaped feed (as feeds3 ships it): the root <li> carries the
# author uin + the post tid; a nested comments-item carries one comment.
_FEED_HTML = (
    '<li class=\\"f-single nopic\\" id=\\"fct_10001_abc\\" data-tid=\\"deadbeef\\">'
    '<a class=\\"f-name q_namecard\\" target=\\"_blank\\">测试昵称<\\/a>'
    '<div class=\\"f-info\\">这是一条说说<\\/div>'
    '<span class=\\"state\\">3小时前<\\/span>'
    '<li class=\\"comments-item\\" data-tid=\\"c1\\" data-uin=\\"20002\\" '
    'data-nick=\\"好友A\\"><a class=\\"comments-name\\">好友A<\\/a>'
    '&nbsp; : 评论内容<div class=\\"comments-op\\">回复<\\/div><\\/li>'
    '<\\/li>'
)
_FEEDS_BODY = (
    '_Callback({"code":0,"message":"","data":{"data":"' + _FEED_HTML + '"}});'
)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def _onebot_transport(
    *,
    fail_login: bool = False,
    empty_cookies: bool = False,
    friends: list[dict] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/get_login_info"):
            if fail_login:
                return httpx.Response(
                    200, json={"status": "failed", "retcode": 1404, "message": "offline"}
                )
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": int(_MY_UIN), "nickname": "Me"},
                },
            )
        if path.endswith("/get_cookies"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"cookies": "" if empty_cookies else _QZONE_COOKIE},
                },
            )
        if path.endswith("/get_friend_list"):
            return httpx.Response(
                200,
                json={"status": "ok", "retcode": 0, "data": friends or []},
            )
        return httpx.Response(404, json={"status": "failed", "retcode": 1, "message": "?"})

    return httpx.MockTransport(handler)


def _qzone_transport(
    *,
    feeds_body: str = _FEEDS_BODY,
    comment_code: int = 0,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/feeds3_html_more"):
            return httpx.Response(200, text=feeds_body)
        if path.endswith("/emotion_cgi_re_feeds"):
            body = (
                "<script>frameElement.callback("
                + json.dumps({"code": comment_code, "subcode": 0, "message": "ok"})
                + ");</script>"
            )
            return httpx.Response(200, text=body)
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def _onebot(**kw) -> OneBotClient:
    return OneBotClient(base_url="http://napcat.test", transport=_onebot_transport(**kw))


def _args(**kw) -> str:
    return json.dumps(kw)


class _EffectStore:
    def __init__(self) -> None:
        self.prepared: list[dict[str, Any]] = []
        self.completed: list[tuple[int, dict[str, Any]]] = []

    async def prepare_effect(self, **kwargs: Any) -> Any:
        self.prepared.append(kwargs)
        return type("Effect", (), {"id": 1})()

    async def complete_effect(self, effect_id: int, **kwargs: Any) -> Any:
        self.completed.append((effect_id, kwargs))
        return type("Effect", (), {"id": effect_id})()


_EFFECT_CONTEXT = {
    "source_system": "external",
    "source_job_id": "job-1",
    "occurrence_key": "external:job-1:1234",
}


# ---------------------------------------------------------------------------
# Pure parser unit tests
# ---------------------------------------------------------------------------


def test_unescape_hex_decodes_js_escapes() -> None:
    assert _unescape_hex(r"a\/b") == "a/b"
    assert _unescape_hex(r"x\x41y") == "xAy"
    assert _unescape_hex(r"<\/div>") == "</div>"
    assert _unescape_hex(r"中") == "中"


def test_parse_feeds3_extracts_feed_and_comment() -> None:
    feeds = _parse_feeds3(_FEEDS_BODY)
    assert len(feeds) == 1
    feed = feeds[0]
    assert feed["uin"] == _MY_UIN
    assert feed["tid"] == "deadbeef"
    assert feed["name"] == "测试昵称"
    assert feed["content"] == "这是一条说说"
    assert feed["time"] == "3小时前"
    assert len(feed["comments"]) == 1
    c = feed["comments"][0]
    assert c["uin"] == _FRIEND_UIN
    assert c["name"] == "好友A"
    assert c["content"] == "评论内容"


def test_parse_callback_json() -> None:
    body = '<script>frameElement.callback({"code":0,"subcode":0});</script>'
    obj = _parse_callback_json(body)
    assert obj == {"code": 0, "subcode": 0}
    assert _parse_callback_json("garbage no callback") is None


def test_schemas_are_openai_shaped() -> None:
    names = {s["function"]["name"] for s in qzone_comment_tool_schemas()}
    assert names == {
        "qzone_list_feed",
        "qzone_get_post",
        "qzone_post_comment",
        "qzone_list_friends",
    }
    for s in qzone_comment_tool_schemas():
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# list_feed
# ---------------------------------------------------------------------------


async def test_list_feed_happy() -> None:
    client = _onebot()
    try:
        out = json.loads(
            await dispatch_qzone_list_feed(
                args_json=_args(num=5),
                onebot_client=client,
                http_transport=_qzone_transport(),
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    assert out["my_uin"] == _MY_UIN
    assert out["returned"] == 1
    assert out["feed"][0]["tid"] == "deadbeef"


async def test_list_feed_owner_filter_excludes_others() -> None:
    client = _onebot()
    try:
        out = json.loads(
            await dispatch_qzone_list_feed(
                args_json=_args(owner_uin=_FRIEND_UIN),
                onebot_client=client,
                http_transport=_qzone_transport(),
            )
        )
    finally:
        await client.aclose()
    # Only feed is authored by _MY_UIN, so filtering to the friend yields 0.
    assert out["ok"] is True
    assert out["returned"] == 0


async def test_list_feed_bad_owner_uin_rejected() -> None:
    out = json.loads(
        await dispatch_qzone_list_feed(
            args_json=_args(owner_uin="not-a-number"),
            onebot_client=_onebot(),
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_list_feed_login_failure_envelope() -> None:
    client = _onebot(fail_login=True)
    try:
        out = json.loads(
            await dispatch_qzone_list_feed(
                args_json=_args(), onebot_client=client, http_transport=_qzone_transport()
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "onebot_failed"


async def test_list_feed_stale_cookie_envelope() -> None:
    client = _onebot(empty_cookies=True)
    try:
        out = json.loads(
            await dispatch_qzone_list_feed(
                args_json=_args(), onebot_client=client, http_transport=_qzone_transport()
            )
        )
    finally:
        await client.aclose()
    # Empty cookie string trips OneBotClient.fetch_cookies → onebot_failed.
    assert out["ok"] is False
    assert out["error"] in {"onebot_failed", "qzone_cookie_stale"}


async def test_list_feed_qzone_error_code() -> None:
    bad = '_Callback({"code":-10000,"message":"使用人数过多"});'
    client = _onebot()
    try:
        out = json.loads(
            await dispatch_qzone_list_feed(
                args_json=_args(),
                onebot_client=client,
                http_transport=_qzone_transport(feeds_body=bad),
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "qzone_read_failed"
    assert "使用人数过多" not in out["message"]
    assert "code=-10000" in out["message"]


async def test_list_feed_http_error_does_not_echo_response_body() -> None:
    marker = "PRIVATE_QZONE_RESPONSE"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=marker)

    client = _onebot()
    try:
        out = json.loads(
            await dispatch_qzone_list_feed(
                args_json=_args(),
                onebot_client=client,
                http_transport=httpx.MockTransport(handler),
            )
        )
    finally:
        await client.aclose()
    assert out["error"] == "qzone_read_failed"
    assert marker not in out["message"]
    assert "HTTP 502" in out["message"]


# ---------------------------------------------------------------------------
# get_post
# ---------------------------------------------------------------------------


async def test_get_post_found_and_missing() -> None:
    client = _onebot()
    try:
        found = json.loads(
            await dispatch_qzone_get_post(
                args_json=_args(tid="deadbeef"),
                onebot_client=client,
                http_transport=_qzone_transport(),
            )
        )
        missing = json.loads(
            await dispatch_qzone_get_post(
                args_json=_args(tid="0000"),
                onebot_client=client,
                http_transport=_qzone_transport(),
            )
        )
    finally:
        await client.aclose()
    assert found["found"] is True
    assert found["post"]["tid"] == "deadbeef"
    assert missing["found"] is False


async def test_get_post_requires_tid() -> None:
    out = json.loads(
        await dispatch_qzone_get_post(args_json=_args(), onebot_client=_onebot())
    )
    assert out["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# post_comment
# ---------------------------------------------------------------------------


async def test_post_comment_top_level() -> None:
    client = _onebot()
    try:
        out = json.loads(
            await dispatch_qzone_post_comment(
                args_json=_args(owner_uin=_MY_UIN, tid="deadbeef", content="不错"),
                onebot_client=client,
                http_transport=_qzone_transport(),
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    assert out["is_reply"] is False
    assert out["content_sent"] == "不错"


async def test_post_comment_live_scheduler_records_effect_receipt() -> None:
    client = _onebot()
    store = _EffectStore()
    try:
        out = json.loads(
            await dispatch_qzone_post_comment(
                args_json=_args(
                    owner_uin=_MY_UIN,
                    tid="deadbeef",
                    content="不错",
                    reply_to_comment_id="comment-7",
                ),
                onebot_client=client,
                http_transport=_qzone_transport(),
                scheduler_store=store,
                effect_context=_EFFECT_CONTEXT,
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    assert store.prepared == [
        {
            **_EFFECT_CONTEXT,
            "effect_kind": "qzone.comment",
            "effect_target": f"post:{_MY_UIN}:deadbeef:id:comment-7",
        }
    ]
    assert store.completed == [
        (
            1,
            {
                "state": "sent",
                "receipt": {
                    "owner_uin": _MY_UIN,
                    "tid": "deadbeef",
                    "is_reply": False,
                    "comment_identity": "id:comment-7",
                },
                "error_code": None,
            },
        )
    ]


async def test_scheduled_reply_requires_source_comment_identity() -> None:
    out = json.loads(
        await dispatch_qzone_post_comment(
            args_json=_args(
                owner_uin=_MY_UIN,
                tid="deadbeef",
                content="不错",
                reply_to_uin=_FRIEND_UIN,
            ),
            scheduler_store=_EffectStore(),
            effect_context=_EFFECT_CONTEXT,
        )
    )
    assert out["error"] == "scheduler_comment_identity_required"


async def test_scheduled_top_level_comment_deduplicates_by_source_post() -> None:
    client = _onebot()
    store = _EffectStore()
    try:
        out = json.loads(
            await dispatch_qzone_post_comment(
                args_json=_args(
                    owner_uin=_FRIEND_UIN,
                    tid="deadbeef",
                    content="不错",
                ),
                onebot_client=client,
                http_transport=_qzone_transport(),
                scheduler_store=store,
                effect_context=_EFFECT_CONTEXT,
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    assert store.prepared[0]["effect_target"] == (
        f"post:{_FRIEND_UIN}:deadbeef:top-level"
    )


async def test_content_fallback_identity_includes_commenter_uin() -> None:
    client = _onebot()
    store = _EffectStore()
    try:
        out = json.loads(
            await dispatch_qzone_post_comment(
                args_json=_args(
                    owner_uin=_MY_UIN,
                    tid="deadbeef",
                    content="不错",
                    reply_to_uin=_FRIEND_UIN,
                    reply_to_comment_content="same source text",
                ),
                onebot_client=client,
                http_transport=_qzone_transport(),
                scheduler_store=store,
                effect_context=_EFFECT_CONTEXT,
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    target = store.prepared[0]["effect_target"]
    assert f"uin:{_FRIEND_UIN}:sha256:" in target


async def test_post_comment_reply_prepends_mention() -> None:
    client = _onebot()
    try:
        out = json.loads(
            await dispatch_qzone_post_comment(
                args_json=_args(
                    owner_uin=_MY_UIN,
                    tid="deadbeef",
                    content="谢谢",
                    reply_to_uin=_FRIEND_UIN,
                    reply_to_name="好友A",
                ),
                onebot_client=client,
                http_transport=_qzone_transport(),
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    assert out["is_reply"] is True
    assert out["content_sent"].startswith(f"@{{uin:{_FRIEND_UIN},nick:好友A,who:1}}")


async def test_post_comment_rejected_by_qzone() -> None:
    client = _onebot()
    try:
        out = json.loads(
            await dispatch_qzone_post_comment(
                args_json=_args(owner_uin=_MY_UIN, tid="deadbeef", content="x"),
                onebot_client=client,
                http_transport=_qzone_transport(comment_code=-1),
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "qzone_rejected"


async def test_post_comment_validates_args() -> None:
    out = json.loads(
        await dispatch_qzone_post_comment(
            args_json=_args(tid="deadbeef", content="hi"), onebot_client=_onebot()
        )
    )
    assert out["error"] == "invalid_args"  # missing owner_uin


# ---------------------------------------------------------------------------
# list_friends
# ---------------------------------------------------------------------------


async def test_list_friends_with_filter() -> None:
    friends = [
        {"user_id": 20002, "nickname": "好友A", "remark": "战友"},
        {"user_id": 30003, "nickname": "Bob", "remark": ""},
    ]
    client = _onebot(friends=friends)
    try:
        out = json.loads(
            await dispatch_qzone_list_friends(
                args_json=_args(filter="bob"), onebot_client=client
            )
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["friends"][0]["uin"] == "30003"


async def test_list_friends_empty() -> None:
    client = _onebot(friends=[])
    try:
        out = json.loads(
            await dispatch_qzone_list_friends(args_json=_args(), onebot_client=client)
        )
    finally:
        await client.aclose()
    assert out["ok"] is True
    assert out["total"] == 0
    assert out["friends"] == []
