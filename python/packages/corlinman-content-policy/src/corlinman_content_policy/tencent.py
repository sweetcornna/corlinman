"""Deterministic fail-closed policy for Tencent-facing transports.

The classifier intentionally targets high-confidence freeze-risk content. It is
not a general-purpose safety model: rules are narrow, versioned, local, and
return only identifiers safe to persist in logs and audit records.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

RULESET_VERSION: Final[str] = "tencent-freeze-risk-2026-07-21.1"
QQ_SAFE_REFUSAL_TEXT: Final[str] = "这个话题不适合在 QQ 上讨论，我们换个安全的话题吧。"

_ZERO_WIDTH_RE = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")
_SPACE_RE = re.compile(r"\s+")
_COLLAPSE_RE = re.compile(r"[^0-9a-z㐀-鿿]+", re.IGNORECASE)


class TencentTopicCategory(StrEnum):
    EXPLICIT_SEXUAL = "explicit_sexual"
    SEXUAL_MINORS = "sexual_minors"
    GRAPHIC_VIOLENCE_THREATS = "graphic_violence_threats"
    SELF_HARM = "self_harm"
    HATE_EXTREMISM = "hate_extremism"
    ILLEGAL_GOODS_TRAFFICKING = "illegal_goods_trafficking"
    FRAUD_GAMBLING_ACCOUNT_ABUSE = "fraud_gambling_account_abuse"
    DOXXING_PRIVACY_ABUSE = "doxxing_privacy_abuse"
    TENCENT_FREEZE_RISK = "tencent_freeze_risk"
    UNCLASSIFIED_MEDIA = "unclassified_media"
    CLASSIFIER_FAILURE = "classifier_failure"


@dataclass(frozen=True, slots=True)
class TencentPolicyConfig:
    enabled: bool = True
    unclassified_media: Literal["deny", "allow"] = "deny"

    @classmethod
    def from_mapping(cls, value: object) -> TencentPolicyConfig:
        """Parse a policy snapshot. Anything except explicit ``False`` is on."""
        if not isinstance(value, Mapping):
            return cls()
        enabled = value.get("enabled") is not False
        media = value.get("unclassified_media")
        return cls(
            enabled=enabled,
            unclassified_media="allow" if media == "allow" else "deny",
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    category_codes: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    ruleset_version: str = RULESET_VERSION
    message_length: int = 0
    content_digest: str = ""

    def audit_fields(self, *, channel: str, direction: str) -> dict[str, object]:
        """Return the only policy fields callers should persist."""
        return {
            "channel": channel,
            "direction": direction,
            "allowed": self.allowed,
            "category_codes": list(self.category_codes),
            "rule_ids": list(self.rule_ids),
            "ruleset_version": self.ruleset_version,
            "message_length": self.message_length,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class ModerationResult:
    decision: PolicyDecision
    text: str | None


@dataclass(frozen=True, slots=True)
class _TextRule:
    rule_id: str
    category: TencentTopicCategory
    matcher: Callable[[str, str], bool]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]


def normalize_text(text: str) -> tuple[str, str]:
    """Return readable-normalized and punctuation/spacing-collapsed forms."""
    folded = unicodedata.normalize("NFKC", text or "")
    folded = _ZERO_WIDTH_RE.sub("", folded)
    folded = "".join(
        ch if ch in "\n\t" or unicodedata.category(ch) not in {"Cc", "Cf"} else " "
        for ch in folded
    ).casefold()
    readable = _SPACE_RE.sub(" ", folded).strip()
    collapsed = _COLLAPSE_RE.sub("", readable)
    return readable, collapsed


def _contains_any(*terms: str) -> Callable[[str, str], bool]:
    normalized = tuple(normalize_text(term)[1] for term in terms)

    def _match(_readable: str, collapsed: str) -> bool:
        return any(term and term in collapsed for term in normalized)

    return _match


def _regex(pattern: str) -> Callable[[str, str], bool]:
    compiled = re.compile(pattern, re.IGNORECASE)

    def _match(readable: str, _collapsed: str) -> bool:
        return compiled.search(readable) is not None

    return _match


def _all_of(*groups: tuple[str, ...]) -> Callable[[str, str], bool]:
    normalized = tuple(
        tuple(normalize_text(term)[1] for term in group)
        for group in groups
    )

    def _match(_readable: str, collapsed: str) -> bool:
        return all(any(term and term in collapsed for term in group) for group in normalized)

    return _match


_RULES: Final[tuple[_TextRule, ...]] = (
    _TextRule(
        "sex.explicit.trade-or-invite",
        TencentTopicCategory.EXPLICIT_SEXUAL,
        _all_of(
            ("约炮", "招嫖", "援交", "裸聊", "成人视频", "黄片", "色情服务"),
            ("微信", "qq", "加我", "价格", "资源", "下载", "群", "上门", "同城"),
        ),
    ),
    _TextRule(
        "sex.explicit.instructions",
        TencentTopicCategory.EXPLICIT_SEXUAL,
        _all_of(
            ("强奸", "迷奸", "下药", "偷拍视频", "偷拍裙底"),
            ("教程", "怎么", "方法", "技巧", "实施", "不被发现"),
        ),
    ),
    _TextRule(
        "sex.minors",
        TencentTopicCategory.SEXUAL_MINORS,
        _all_of(
            ("未成年", "儿童", "小学生", "初中生", "幼女", "幼童", "萝莉"),
            ("色情资源", "裸照", "裸聊", "性服务", "约炮", "性行为视频", "成人视频"),
        ),
    ),
    _TextRule(
        "violence.graphic-or-threat",
        TencentTopicCategory.GRAPHIC_VIOLENCE_THREATS,
        _all_of(
            ("杀了", "砍死", "炸死", "灭门", "割喉", "肢解", "虐杀", "枪杀"),
            ("你", "他", "她", "他们", "学校", "公司", "全家", "现场", "视频", "图片"),
        ),
    ),
    _TextRule(
        "violence.attack-instructions",
        TencentTopicCategory.GRAPHIC_VIOLENCE_THREATS,
        _all_of(
            ("炸弹", "爆炸物", "枪支改造", "制式枪", "投毒", "纵火", "杀人"),
            ("制作", "配方", "教程", "怎么实施", "步骤", "不被发现", "实施方案"),
        ),
    ),
    _TextRule(
        "self-harm.encourage-or-instruct",
        TencentTopicCategory.SELF_HARM,
        _all_of(
            ("自杀", "割腕", "上吊", "跳楼", "服毒", "轻生"),
            ("方法", "教程", "怎么", "成功率", "无痛", "鼓励", "去死", "步骤"),
        ),
    ),
    _TextRule(
        "hate.extremist-recruitment",
        TencentTopicCategory.HATE_EXTREMISM,
        _all_of(
            ("恐怖组织", "纳粹", "极端组织", "圣战组织", "isis", "is 核心"),
            ("加入", "招募", "万岁", "宣誓", "效忠", "宣传", "行动指南", "攻击"),
        ),
    ),
    _TextRule(
        "illegal.trade",
        TencentTopicCategory.ILLEGAL_GOODS_TRAFFICKING,
        _all_of(
            ("冰毒", "海洛因", "摇头丸", "大麻", "枪支", "子弹", "迷药", "人口买卖"),
            ("购买", "出售", "出货", "价格", "渠道", "货源", "交易", "包邮", "联系"),
        ),
    ),
    _TextRule(
        "fraud.account-abuse",
        TencentTopicCategory.FRAUD_GAMBLING_ACCOUNT_ABUSE,
        _contains_any(
            "qq 解冻教程",
            "qq 防封教程",
            "qq 养号防封",
            "qq 群发引流",
            "批量注册 qq",
            "盗 qq 号",
            "qq 号轰炸",
            "刷流水返佣",
            "博彩下注群",
            "赌博平台代理",
            "电诈话术",
            "杀猪盘话术",
        ),
    ),
    _TextRule(
        "fraud.credential-phishing",
        TencentTopicCategory.FRAUD_GAMBLING_ACCOUNT_ABUSE,
        _all_of(
            ("钓鱼", "盗号", "骗验证码", "洗钱", "跑分", "银行卡四件套"),
            ("教程", "话术", "链接", "平台", "渠道", "购买", "出售", "接单"),
        ),
    ),
    _TextRule(
        "privacy.doxxing",
        TencentTopicCategory.DOXXING_PRIVACY_ABUSE,
        _all_of(
            ("开盒", "人肉搜索", "查身份证", "查家庭住址", "查手机号定位", "泄露隐私"),
            ("教程", "平台", "渠道", "帮我", "代查", "曝光", "发布", "购买"),
        ),
    ),
    _TextRule(
        "tencent.freeze-evasion",
        TencentTopicCategory.TENCENT_FREEZE_RISK,
        _regex(
            r"(?:绕过|规避|逃避).{0,16}(?:qq|腾讯).{0,16}(?:风控|冻结|封号|审核)"
            r"|(?:qq|腾讯).{0,16}(?:绕过|规避|逃避).{0,16}(?:风控|冻结|封号|审核)"
            r"|(?:qq|腾讯).{0,16}(?:风控|冻结|封号|审核).{0,16}(?:绕过|规避|逃避)"
            r"|(?:风控|冻结|封号|审核).{0,16}(?:绕过|规避|逃避).{0,16}(?:qq|腾讯)"
        ),
    ),
)


def classify_text(text: str, config: TencentPolicyConfig | None = None) -> PolicyDecision:
    cfg = config or TencentPolicyConfig()
    raw = text if isinstance(text, str) else str(text or "")
    if not cfg.enabled:
        return PolicyDecision(
            allowed=True,
            message_length=len(raw),
            content_digest=_digest(raw),
        )

    readable, collapsed = normalize_text(raw)
    matched = [rule for rule in _RULES if rule.matcher(readable, collapsed)]
    categories = tuple(dict.fromkeys(rule.category.value for rule in matched))
    rule_ids = tuple(rule.rule_id for rule in matched)
    return PolicyDecision(
        allowed=not matched,
        category_codes=categories,
        rule_ids=rule_ids,
        message_length=len(raw),
        content_digest=_digest(raw),
    )


def classifier_failure_decision(text: str = "") -> PolicyDecision:
    raw = text if isinstance(text, str) else str(text or "")
    return PolicyDecision(
        allowed=False,
        category_codes=(TencentTopicCategory.CLASSIFIER_FAILURE.value,),
        rule_ids=("policy.classifier-failure",),
        message_length=len(raw),
        content_digest=_digest(raw),
    )


def moderate_text(text: str, config: TencentPolicyConfig | None = None) -> ModerationResult:
    cfg = config or TencentPolicyConfig()
    try:
        decision = classify_text(text, cfg)
    except Exception:
        if not cfg.enabled:
            decision = PolicyDecision(
                allowed=True,
                message_length=len(text or ""),
                content_digest=_digest(text or ""),
            )
        else:
            decision = classifier_failure_decision(text)
    return ModerationResult(
        decision=decision,
        text=text if decision.allowed else QQ_SAFE_REFUSAL_TEXT,
    )


def moderate_media(
    *,
    config: TencentPolicyConfig | None = None,
    classified_safe: bool | None = None,
) -> PolicyDecision:
    cfg = config or TencentPolicyConfig()
    if not cfg.enabled or classified_safe is True or cfg.unclassified_media == "allow":
        return PolicyDecision(allowed=True)
    return PolicyDecision(
        allowed=False,
        category_codes=(TencentTopicCategory.UNCLASSIFIED_MEDIA.value,),
        rule_ids=("media.unclassified-deny",),
    )
