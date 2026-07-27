"""``[approvals]`` → ``[permissions]`` translation (audit W3-2, plan §2.2).

The gateway's ``[[approvals.rules]]`` TOML section was dead config (fact M1
of the design plan): a complete rule grammar with zero consumers. W3-2
retires it by TRANSLATING it into the unified ``[permissions]`` rule
language at config-load time:

===============================================  =================================
``[approvals]`` field                            ``[permissions]`` equivalent
===============================================  =================================
``plugin = "p"`` (no tool)                       ``tool = "plugin:p/*"``
``plugin = "p", tool = "t"``                     ``tool = "plugin:p/t"``
``mode = "auto"``                                ``action = "allow"``
``mode = "prompt"``                              ``action = "ask"``
``mode = "deny"``                                ``action = "deny"``
``allow_session_keys = [...]``                   per-key ``allow`` rules with
                                                 ``scope.session = "<key>"``
===============================================  =================================

Ordering (risk R7): the legacy matcher picked the MOST specific rule
(exact ``(plugin, tool)`` over plugin-wide, first declaration wins within a
tier — ``gateway/middleware/approval.py:match_rule``), while the unified
gate is last-match-wins. The translator therefore emits **most general
first**: plugin-wide rules, their session whitelists, then exact rules,
then the exact rules' session whitelists. Duplicate declarations for the
same key are dropped (first declaration wins, like the old matcher). A
property test pins verdict equivalence sample by sample.

The translated block is merged BEFORE the operator's own
``[[permissions.rules]]`` so, under last-match-wins, ``[permissions]``
always wins during the double-read deprecation window.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from corlinman_agent.authz.matcher import glob_escape

logger = structlog.get_logger(__name__)

__all__ = [
    "merge_approvals_into_permissions",
    "translate_approvals_rules",
]

_MODE_TO_ACTION: dict[str, str] = {
    "auto": "allow",
    "prompt": "ask",
    "deny": "deny",
}

_TRANSLATION_NOTE = "translated from deprecated [[approvals.rules]]"

#: WARN once per process — the translator runs on every config render /
#: hot reload, and the deprecation is actionable exactly once.
_WARNED = False


def _get(entry: Any, key: str) -> Any:
    """Attr-or-key accessor (config objects vs plain TOML dicts)."""
    if isinstance(entry, Mapping):
        return entry.get(key)
    return getattr(entry, key, None)


def translate_approvals_rules(section: Any) -> list[dict[str, Any]]:
    """Translate one ``[approvals]`` section into permission-rule dicts.

    Tolerant by design (config-driven, must never raise into boot): a
    non-mapping section, a rule without a plugin name, or an unknown mode
    is skipped. Returns ``[]`` when there is nothing to translate.
    """
    rules_raw = _get(section, "rules") if section is not None else None
    if not isinstance(rules_raw, (list, tuple)):
        return []

    # First declaration wins within each specificity tier — mirror the
    # legacy matcher's scan exactly, then emit general-before-specific so
    # last-match-wins reproduces "exact beats plugin-wide".
    plugin_wide: dict[str, dict[str, Any]] = {}
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in rules_raw:
        plugin = _get(entry, "plugin")
        if not isinstance(plugin, str) or not plugin.strip():
            continue
        plugin = plugin.strip()
        mode = _get(entry, "mode")
        mode_key = str(mode).strip().lower() if mode is not None else "auto"
        action = _MODE_TO_ACTION.get(mode_key)
        if action is None:
            logger.warning(
                "corlinman.config.approvals_unknown_mode",
                plugin=plugin,
                mode=mode_key,
            )
            continue
        tool = _get(entry, "tool")
        tool = tool.strip() if isinstance(tool, str) and tool.strip() else None
        session_keys_raw = _get(entry, "allow_session_keys")
        session_keys = [
            str(k)
            for k in (session_keys_raw or [])
            if isinstance(k, str) and k
        ] if isinstance(session_keys_raw, (list, tuple)) else []

        if tool is None:
            key = f"plugin:{glob_escape(plugin)}/*"
            bucket, bucket_key = plugin_wide, plugin
        else:
            key = f"plugin:{glob_escape(plugin)}/{glob_escape(tool)}"
            bucket, bucket_key = exact, (plugin, tool)  # type: ignore[assignment]
        if bucket_key in bucket:
            continue  # first declaration wins (legacy tier semantics)
        rule: dict[str, Any] = {
            "tool": key,
            "action": action,
            "note": _TRANSLATION_NOTE,
        }
        # ``allow_session_keys`` only ever short-circuited PROMPT rules in
        # the legacy matcher; keep that scoping.
        if action == "ask" and session_keys:
            rule["_session_whitelist"] = session_keys
        bucket[bucket_key] = rule

    def _expand(tier: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        whitelists: list[dict[str, Any]] = []
        for rule in tier:
            session_keys = rule.pop("_session_whitelist", None)
            out.append(rule)
            for sk in session_keys or []:
                whitelists.append(
                    {
                        "tool": rule["tool"],
                        "action": "allow",
                        "note": _TRANSLATION_NOTE,
                        "scope": {"session": glob_escape(sk)},
                    }
                )
        # Whitelist allows are MORE specific than their parent rule, so
        # they come after it (last-match-wins) but still before the next
        # tier — an exact rule must beat a plugin-wide whitelist, exactly
        # like the legacy "exact over plugin-wide" preference.
        return out + whitelists

    return _expand(list(plugin_wide.values())) + _expand(list(exact.values()))


def merge_approvals_into_permissions(
    approvals: Any,
    permissions: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge a deprecated ``[approvals]`` section into a ``[permissions]``
    block (translated rules FIRST, so ``[permissions]`` wins under
    last-match-wins — risk R7). Logs the deprecation WARN once per process.

    Returns the (possibly new) permissions mapping, or the original
    ``permissions`` untouched when there is nothing to translate.
    """
    translated = translate_approvals_rules(approvals)
    if not translated:
        return dict(permissions) if isinstance(permissions, Mapping) else None
    global _WARNED  # noqa: PLW0603 — process-lifetime dedup, deliberate
    if not _WARNED:
        _WARNED = True
        logger.warning(
            "corlinman.config.approvals_deprecated",
            translated_rules=len(translated),
            detail=(
                "[approvals] is deprecated; its rules were translated into "
                "[[permissions.rules]] (prepended, so explicit [permissions] "
                "rules win). Move the policy to [permissions] — the "
                "double-read window lasts one minor release."
            ),
        )
    merged: dict[str, Any] = (
        dict(permissions) if isinstance(permissions, Mapping) else {}
    )
    existing = merged.get("rules")
    merged["rules"] = translated + (
        list(existing) if isinstance(existing, (list, tuple)) else []
    )
    return merged
