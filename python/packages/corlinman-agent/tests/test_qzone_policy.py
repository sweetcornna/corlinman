from __future__ import annotations

import json

from corlinman_agent.qzone.comment import (
    _redact_feeds,
    dispatch_qzone_list_friends,
    dispatch_qzone_post_comment,
)
from corlinman_agent.qzone.publish import dispatch_qzone_publish


async def test_missing_resolver_is_fail_closed() -> None:
    publish = json.loads(
        await dispatch_qzone_publish(
            args_json=json.dumps({"text": "QQ 解冻教程"})
        )
    )
    comment = json.loads(
        await dispatch_qzone_post_comment(
            args_json=json.dumps(
                {
                    "owner_uin": "123",
                    "tid": "abc",
                    "content": "QQ 解冻教程",
                }
            )
        )
    )
    assert publish["error"] == "content_policy_blocked"
    assert comment["error"] == "content_policy_blocked"


async def test_publish_policy_blocks_before_generator_or_onebot() -> None:
    called = False

    async def generator(**_kwargs):
        nonlocal called
        called = True
        return json.dumps({"ok": True, "path": "x.png"})

    out = json.loads(
        await dispatch_qzone_publish(
            args_json=json.dumps(
                {
                    "text": "同城招嫖价格私聊加我QQ",
                    "generate": {"prompt": "safe", "characters": []},
                }
            ),
            image_with_refs_dispatcher=generator,
            policy_resolver=lambda: True,
        )
    )
    assert out["error"] == "content_policy_blocked"
    assert called is False


async def test_publish_suppresses_media_before_generator_when_text_is_safe() -> None:
    called = False

    async def generator(**_kwargs):
        nonlocal called
        called = True
        return json.dumps({"ok": True, "path": "x.png"})

    out = json.loads(
        await dispatch_qzone_publish(
            args_json=json.dumps(
                {
                    "text": "今天的晚风很舒服。",
                    "generate": {"prompt": "an evening view", "characters": []},
                }
            ),
            image_with_refs_dispatcher=generator,
            policy_resolver=lambda: True,
            execution_mode="shadow",
        )
    )
    assert out == {
        "ok": True,
        "shadow": True,
        "effect": "qzone_publish",
        "text_chars": 9,
        "media_suppressed": True,
    }
    assert called is False


async def test_comment_shadow_stops_before_auth_and_http() -> None:
    class _ExplodingClient:
        async def fetch_login_info(self):
            raise AssertionError("shadow comment touched OneBot")

    out = json.loads(
        await dispatch_qzone_post_comment(
            args_json=json.dumps(
                {
                    "owner_uin": "123",
                    "tid": "abc",
                    "content": "今天天气真好",
                }
            ),
            onebot_client=_ExplodingClient(),
            policy_resolver=lambda: True,
            execution_mode="shadow",
        )
    )
    assert out == {
        "ok": True,
        "shadow": True,
        "effect": "qzone_post_comment",
        "owner_uin": "123",
        "tid": "abc",
        "is_reply": False,
        "comment_identity": "",
        "content_chars": 6,
    }


async def test_comment_policy_blocks_before_auth() -> None:
    out = json.loads(
        await dispatch_qzone_post_comment(
            args_json=json.dumps(
                {
                    "owner_uin": "123",
                    "tid": "abc",
                    "content": "QQ 解冻教程",
                }
            ),
            policy_resolver=lambda: True,
        )
    )
    assert out["error"] == "content_policy_blocked"


def test_feed_policy_redacts_source_text_and_counts_categories() -> None:
    clean, counts = _redact_feeds(
        [
            {
                "tid": "1",
                "name": "safe author",
                "content": "QQ 解冻教程",
                "comments": [
                    {
                        "id": "c1",
                        "name": "QQ 解冻教程",
                        "content": "今晚一起吃饭吗",
                    }
                ],
            }
        ],
        lambda: True,
    )
    assert clean[0]["content"] == "[内容已按 QQ 风控策略隐藏]"
    assert clean[0]["comments"][0]["name"] == "[内容已按 QQ 风控策略隐藏]"
    assert clean[0]["comments"][0]["content"] == "今晚一起吃饭吗"
    assert counts == {"fraud_gambling_account_abuse": 2}


async def test_friend_names_are_redacted_before_model_exposure() -> None:
    class _Client:
        async def fetch_friend_list(self):
            return [
                {
                    "user_id": 123,
                    "nickname": "QQ 解冻教程",
                    "remark": "safe",
                }
            ]

    out = json.loads(
        await dispatch_qzone_list_friends(
            args_json="{}",
            onebot_client=_Client(),
            policy_resolver=lambda: True,
        )
    )
    assert out["friends"] == [
        {
            "uin": "123",
            "nickname": "[内容已按 QQ 风控策略隐藏]",
            "remark": "safe",
        }
    ]
