"""Unified authorization model (audit W3 / P4).

One rule language, evaluated by :class:`~corlinman_agent.authz.gate.AuthzGate`
at **call time** (never frozen at construction), with three grant memories
(``once`` / ``session`` / ``always``) stored in
:class:`~corlinman_agent.authz.grants.GrantStore`.

Layout:

* :mod:`~corlinman_agent.authz.model` — domain objects (Subject / Memory /
  PermissionMode / action constants).
* :mod:`~corlinman_agent.authz.matcher` — the matching core, extracted from
  ``corlinman_agent.permission`` (rule syntax is unchanged).
* :mod:`~corlinman_agent.authz.defaults` — the ``[permissions]`` config
  shape layer (same paradigm as :mod:`corlinman_agent.runtime_defaults`).
* :mod:`~corlinman_agent.authz.gate` — the call-time evaluator.
* :mod:`~corlinman_agent.authz.grants` — memoried interactive grants.
"""

from __future__ import annotations

from corlinman_agent.authz.approvals_compat import (
    merge_approvals_into_permissions,
    translate_approvals_rules,
)
from corlinman_agent.authz.defaults import (
    PermissionsDefaults,
    apply_permissions_config,
    get_permissions_defaults,
    reset_permissions_defaults,
    resolve_external_tools_enforced,
)
from corlinman_agent.authz.gate import AuthzGate
from corlinman_agent.authz.grants import GrantStore, get_grant_store, reset_grant_store
from corlinman_agent.authz.matcher import external_candidate_keys
from corlinman_agent.authz.model import Memory, PermissionMode, Subject
from corlinman_agent.authz.prompt_channel import (
    AuthzAnswer,
    AuthzRequest,
    NullPromptChannel,
    PromptChannel,
    ResolverPromptChannel,
    grant_scope_flags,
)

__all__ = [
    "AuthzAnswer",
    "AuthzGate",
    "AuthzRequest",
    "GrantStore",
    "Memory",
    "NullPromptChannel",
    "PermissionMode",
    "PermissionsDefaults",
    "PromptChannel",
    "ResolverPromptChannel",
    "Subject",
    "apply_permissions_config",
    "external_candidate_keys",
    "get_grant_store",
    "get_permissions_defaults",
    "grant_scope_flags",
    "merge_approvals_into_permissions",
    "reset_grant_store",
    "reset_permissions_defaults",
    "resolve_external_tools_enforced",
    "translate_approvals_rules",
]
