"""``/admin/authz/policy`` — structured editor for the ``[permissions]`` section (W3-4).

The policy editor's backend. Reads and writes the same ``[permissions]``
config section the agent's AuthzGate consumes, through the *existing*
config-admin channel: an atomic ``config.toml`` rewrite followed by
:func:`publish_config_mutation`, which swaps the in-process snapshot AND
re-renders the ``py-config.json`` sidecar. The agent process re-reads the
sidecar on its next model resolution, so a saved policy takes effect on
the next turn without a restart (the W3-1 four-step channel — this route
adds no new plumbing, it reuses ``_persist_section``).

Validation is strict on write (a policy typo must not round-trip into a
silently-dropped rule) while the GET view is tolerant (it renders whatever
the operator hand-wrote, best-effort).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.config_admin.models import (
    _persist_section,
)
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)

_VALID_ACTIONS = ("allow", "deny", "ask", "log")
_VALID_MEMORIES = ("once", "session", "always")
_VALID_MODES = ("default", "acceptEdits", "plan", "bypass")
_SCOPE_KEYS = ("tenant", "surface", "user", "session", "model")


# ---------------------------------------------------------------------------
# Wire shapes — mirror the TOML authority shape from the design plan §1.2.1
# ---------------------------------------------------------------------------


class PolicyRuleScope(BaseModel):
    """``[permissions.rules.scope]`` — every field optional."""

    tenant: str | None = None
    surface: str | None = None
    user: str | None = None
    session: str | None = None
    model: str | None = None


class PolicyRule(BaseModel):
    """One ``[[permissions.rules]]`` entry."""

    tool: str
    action: str
    note: str | None = None
    memory: str | None = None
    scope: PolicyRuleScope | None = None


class PolicyBody(BaseModel):
    """The structured ``[permissions]`` section. ``None`` = unset (the
    key is omitted from the TOML so env/file layers keep their say)."""

    mode: str | None = None
    strict: bool | None = None
    default_action: str | None = None
    last_match_wins: bool | None = None
    external_tools_enforced: bool | None = None
    rules: list[PolicyRule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Section <-> wire conversion
# ---------------------------------------------------------------------------


def _section_to_policy(section: Any) -> PolicyBody:
    """Tolerant read of a raw ``[permissions]`` dict (GET view)."""
    if not isinstance(section, dict):
        return PolicyBody()
    rules: list[PolicyRule] = []
    raw_rules = section.get("rules")
    if isinstance(raw_rules, list):
        for entry in raw_rules:
            if not isinstance(entry, dict):
                continue
            tool = entry.get("tool")
            action = entry.get("action")
            if not isinstance(tool, str) or not isinstance(action, str):
                continue
            scope_raw = entry.get("scope") if isinstance(entry.get("scope"), dict) else None
            # ``match`` is the deprecated alias for ``scope`` (§2.1).
            if scope_raw is None and isinstance(entry.get("match"), dict):
                scope_raw = entry.get("match")
            scope = None
            if scope_raw:
                scope = PolicyRuleScope(
                    **{
                        k: scope_raw[k]
                        for k in _SCOPE_KEYS
                        if isinstance(scope_raw.get(k), str)
                    }
                )
            rules.append(
                PolicyRule(
                    tool=tool,
                    action=action,
                    note=entry.get("note") if isinstance(entry.get("note"), str) else None,
                    memory=entry.get("memory")
                    if isinstance(entry.get("memory"), str)
                    else None,
                    scope=scope,
                )
            )
    return PolicyBody(
        mode=section.get("mode") if isinstance(section.get("mode"), str) else None,
        strict=section.get("strict") if isinstance(section.get("strict"), bool) else None,
        default_action=section.get("default_action")
        if isinstance(section.get("default_action"), str)
        else None,
        last_match_wins=section.get("last_match_wins")
        if isinstance(section.get("last_match_wins"), bool)
        else None,
        external_tools_enforced=section.get("external_tools_enforced")
        if isinstance(section.get("external_tools_enforced"), bool)
        else None,
        rules=rules,
    )


def _validate(body: PolicyBody) -> list[dict[str, str]]:
    """Strict write-side validation; returns a list of issue envelopes."""
    issues: list[dict[str, str]] = []
    if body.mode is not None and body.mode not in _VALID_MODES:
        issues.append(
            {"path": "mode", "message": f"unknown mode {body.mode!r}"}
        )
    if body.default_action is not None and body.default_action not in _VALID_ACTIONS:
        issues.append(
            {
                "path": "default_action",
                "message": f"unknown action {body.default_action!r}",
            }
        )
    for i, rule in enumerate(body.rules):
        if not rule.tool.strip():
            issues.append({"path": f"rules[{i}].tool", "message": "tool is required"})
        if rule.action not in _VALID_ACTIONS:
            issues.append(
                {
                    "path": f"rules[{i}].action",
                    "message": f"unknown action {rule.action!r}",
                }
            )
        if rule.memory is not None and rule.memory not in _VALID_MEMORIES:
            issues.append(
                {
                    "path": f"rules[{i}].memory",
                    "message": f"unknown memory {rule.memory!r}",
                }
            )
    return issues


def _policy_to_section(body: PolicyBody) -> dict[str, Any]:
    """Render the section dict for TOML. Unset keys stay absent — the
    sidecar renderer's "absent key = None payload" contract depends on
    it (config-pattern step 1)."""
    section: dict[str, Any] = {}
    if body.mode is not None:
        section["mode"] = body.mode
    if body.strict is not None:
        section["strict"] = body.strict
    if body.default_action is not None:
        section["default_action"] = body.default_action
    if body.last_match_wins is not None:
        section["last_match_wins"] = body.last_match_wins
    if body.external_tools_enforced is not None:
        section["external_tools_enforced"] = body.external_tools_enforced
    rules: list[dict[str, Any]] = []
    for rule in body.rules:
        entry: dict[str, Any] = {
            "tool": rule.tool.strip(),
            "action": rule.action,
        }
        if rule.note is not None and rule.note.strip():
            entry["note"] = rule.note.strip()
        if rule.memory is not None:
            entry["memory"] = rule.memory
        if rule.scope is not None:
            scope = {
                k: v.strip()
                for k, v in rule.scope.model_dump().items()
                if isinstance(v, str) and v.strip()
            }
            if scope:
                entry["scope"] = scope
        rules.append(entry)
    if rules:
        section["rules"] = rules
    return section


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "authz"])

    @r.get("/admin/authz/policy", response_model=PolicyBody)
    async def get_policy() -> PolicyBody:
        cfg = dict(config_snapshot())
        return _section_to_policy(cfg.get("permissions"))

    @r.put("/admin/authz/policy", response_model=PolicyBody)
    async def put_policy(body: PolicyBody):
        issues = _validate(body)
        if issues:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_policy", "issues": issues},
            )
        state = get_admin_state()
        section = _policy_to_section(body)
        # Whole-section PUT: rules are a wholesale-replaced array, so the
        # asymmetric "omitted = keep" semantics some scalar sections use
        # do not apply here. An empty policy removes the section.
        err = await _persist_section(state, "permissions", section)
        if err is not None:
            return err
        return _section_to_policy(section)

    return r


__all__ = ["PolicyBody", "PolicyRule", "PolicyRuleScope", "router"]
