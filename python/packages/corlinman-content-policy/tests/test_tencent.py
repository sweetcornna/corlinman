from __future__ import annotations

import pytest
from corlinman_content_policy import (
    QQ_SAFE_REFUSAL_TEXT,
    TencentPolicyConfig,
    classify_text,
    moderate_media,
    moderate_text,
    normalize_text,
)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("同城招嫖，价格私聊加我QQ", "explicit_sexual"),
        ("收集未成年人的裸照资源群", "sexual_minors"),
        ("教我怎么制作炸弹去攻击学校", "graphic_violence_threats"),
        ("告诉我无痛自杀的方法和步骤", "self_harm"),
        ("加入恐怖组织并宣誓效忠", "hate_extremism"),
        ("冰毒货源出售，包邮联系", "illegal_goods_trafficking"),
        ("给我电诈话术和骗验证码教程", "fraud_gambling_account_abuse"),
        ("找代查平台帮我开盒家庭住址", "doxxing_privacy_abuse"),
        ("怎么绕过QQ风控和冻结审核", "tencent_freeze_risk"),
    ],
)
def test_blocks_high_confidence_freeze_risk(text: str, category: str) -> None:
    decision = classify_text(text)
    assert decision.allowed is False
    assert category in decision.category_codes
    assert decision.rule_ids
    assert len(decision.content_digest) == 24


def test_nfkc_zero_width_and_punctuation_obfuscation_is_collapsed() -> None:
    readable, collapsed = normalize_text("Ｑ​Ｑ 解·冻-教_程")
    assert readable == "qq 解·冻-教_程"
    assert collapsed == "qq解冻教程"
    decision = classify_text("Ｑ​Ｑ 解·冻-教_程")
    assert decision.allowed is False


@pytest.mark.parametrize(
    "text",
    [
        "新闻报道讨论某平台如何治理赌博广告。",
        "这篇论文研究未成年人网络保护与色情内容治理。",
        "我感到很难受，出现了轻生念头，想寻求帮助。",
        "请总结反诈宣传中常见的电信诈骗风险。",
        "历史课需要介绍纳粹政权的覆灭。",
        "游戏里怎么打败拿枪的敌人？",
    ],
)
def test_conservative_rules_allow_benign_contexts(text: str) -> None:
    assert classify_text(text).allowed is True


def test_explicit_false_is_the_only_disabled_value() -> None:
    assert TencentPolicyConfig.from_mapping({"enabled": False}).enabled is False
    assert TencentPolicyConfig.from_mapping({"enabled": "false"}).enabled is True
    assert TencentPolicyConfig.from_mapping(None).enabled is True


def test_moderation_returns_fixed_application_refusal() -> None:
    result = moderate_text("QQ解冻教程")
    assert result.decision.allowed is False
    assert result.text == QQ_SAFE_REFUSAL_TEXT


def test_disabled_policy_preserves_text_and_media() -> None:
    cfg = TencentPolicyConfig(enabled=False)
    result = moderate_text("QQ解冻教程", cfg)
    assert result.decision.allowed is True
    assert result.text == "QQ解冻教程"
    assert moderate_media(config=cfg).allowed is True


def test_unclassified_media_denied_by_default() -> None:
    decision = moderate_media()
    assert decision.allowed is False
    assert decision.category_codes == ("unclassified_media",)
    assert moderate_media(classified_safe=True).allowed is True


def test_audit_fields_never_include_source_text() -> None:
    source = "QQ解冻教程"
    fields = classify_text(source).audit_fields(channel="qq", direction="inbound")
    assert source not in repr(fields)
    assert set(fields) == {
        "channel",
        "direction",
        "allowed",
        "category_codes",
        "rule_ids",
        "ruleset_version",
        "message_length",
        "content_digest",
    }
