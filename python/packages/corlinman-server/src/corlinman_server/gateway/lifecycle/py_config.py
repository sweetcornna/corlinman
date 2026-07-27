"""Python AI-plane config handshake (Feature C last-mile).

Once Python *is* the runtime the Rust→Python file drop is partially
redundant — the same process that owns the live config can simply pass
:class:`ProviderRegistry` / alias maps into the agent servicer directly.
We keep the renderer / writer pair anyway, for three reasons:

1. ``corlinman_server.main._load_config`` still reads the
   ``CORLINMAN_PY_CONFIG`` JSON file when it's set — keeping the writer
   in-tree means a Python admin route that mutates ``providers`` /
   ``aliases`` / ``embedding`` can re-emit the file the existing
   in-process resolver watches.
2. External integrations (sidecars, FFI loaders) that grew up against
   the JSON shape stay supported with zero behavioural change.
3. The schema is small and the test suite anchors it — re-implementing
   it later if a sibling regresses the shape would be needless churn.

The JSON shape preserves the existing Python AI-plane handshake and carries
the sections the agent process needs at boot:

.. code-block:: json

    {
      "providers": [
        { "name": "anthropic", "kind": "anthropic",
          "api_key": "...", "base_url": null,
          "enabled": true, "params": {} }
      ],
      "aliases": {
        "smart": { "provider": "anthropic",
                   "model": "claude-opus-4-7",
                   "params": {"temperature": 0.7} }
      },
      "embedding": {
        "provider": "openai", "model": "text-embedding-3-small",
        "dimension": 1536, "enabled": true, "params": {}
      },
      "subagent": {
        "max_concurrent_per_parent": 10,
        "max_concurrent_per_tenant": 15,
        "max_depth": 1,
        "max_wall_seconds_ceiling": 300
      }
    }

The Python config object can be either a :class:`pydantic.BaseModel`,
a :class:`dict`, or any object whose ``providers`` / ``models.aliases``
/ ``embedding`` / ``subagent`` attributes behave like the gateway config. The
renderer is duck-typed (``getattr`` + ``hasattr``) so a future config
schema rev doesn't break the handshake.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import structlog

log = structlog.get_logger(__name__)

#: Env var name the Python AI plane reads to locate the JSON drop.
#: Mirrors ``corlinman_gateway::py_config::ENV_PY_CONFIG``.
ENV_PY_CONFIG: Final[str] = "CORLINMAN_PY_CONFIG"

#: Filename under ``$CORLINMAN_DATA_DIR``.
DEFAULT_PY_CONFIG_FILENAME: Final[str] = "py-config.json"

#: Per-spec the Python ``ProviderSpec.kind`` enum (mirrors
#: ``corlinman_core::config::ProviderKind``). Used to validate provider
#: ``kind`` values when the input config carries an explicit kind.
KNOWN_PROVIDER_KINDS: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "openai_compatible",
        "gemini",
        "ollama",
        "codex",
    }
)


def default_py_config_path() -> Path:
    """Resolve the default JSON drop location.

    Precedence:

    1. ``$CORLINMAN_DATA_DIR/py-config.json``
    2. ``~/.corlinman/py-config.json``
    3. ``/tmp/corlinman-py-config.json`` (container-friendly fallback)
    """
    data_dir = os.environ.get("CORLINMAN_DATA_DIR")
    if data_dir:
        return Path(data_dir) / DEFAULT_PY_CONFIG_FILENAME
    home = Path.home() if _has_home() else None
    if home is not None:
        return home / ".corlinman" / DEFAULT_PY_CONFIG_FILENAME
    return Path("/tmp/corlinman-py-config.json")


def _has_home() -> bool:
    try:
        Path.home()
        return True
    except (RuntimeError, OSError):
        return False


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_py_config(
    cfg: Any,
    *,
    qq_transport_overlay: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render the in-process config object as the Python JSON shape.

    Duck-typed against the Rust ``Config``:

    * ``cfg.providers`` — iterable of ``(name, entry)`` *or* a mapping
      ``{name: entry}``. Each ``entry`` exposes ``kind`` / ``api_key`` /
      ``base_url`` / ``enabled`` / ``params``.
    * ``cfg.models.aliases`` — mapping ``{alias_name: entry}``. ``entry``
      is either a full-form spec (``provider`` + ``model``) or a
      shorthand string. Shorthands are dropped — the Python legacy-prefix
      fallback handles them.
    * ``cfg.embedding`` — optional ``EmbeddingConfig`` with
      ``provider`` / ``model`` / ``dimension`` / ``enabled`` / ``params``.

    Invariants kept from the Rust impl:

    * Providers without a resolvable ``kind`` are dropped.
    * ``api_key`` is resolved via :func:`_resolve_secret`. Unresolved
      secrets render as ``None`` (Python ``ProviderSpec`` treats this as
      "no auth", same as the Rust side does for ``Option<String>``).
    * Shorthand / provider-less aliases are omitted from the output.
    """
    providers_in = _iter_providers(cfg)
    providers: list[dict[str, Any]] = []
    for name, entry in providers_in:
        kind = _kind_for(name, entry)
        if kind is None:
            continue
        providers.append(
            {
                "name": str(name),
                "kind": kind,
                "api_key": _resolve_secret(_attr(entry, "api_key", None)),
                "base_url": _attr(entry, "base_url", None),
                "enabled": bool(_attr(entry, "enabled", True)),
                "params": _params_to_json(_attr(entry, "params", {})),
            }
        )

    aliases_out: dict[str, Any] = {}
    for alias_name, alias_entry in _iter_aliases(cfg).items():
        rendered = _render_alias(alias_entry)
        if rendered is None:
            continue
        aliases_out[str(alias_name)] = rendered

    embedding = _render_embedding(_attr(cfg, "embedding", None))
    subagent = _render_subagent(_attr(cfg, "subagent", None))
    channels = _attr(cfg, "channels", None)
    qq = _attr(channels, "qq", None)
    tencent_safety = {
        # Only the literal boolean false opts out. Missing/malformed stays on.
        "enabled": _attr(qq, "freeze_risk_topic_blocking", True) is not False,
        "unclassified_media": "deny",
    }
    canonical_instances = isinstance(_attr(qq, "instances", None), Mapping)
    if canonical_instances:
        # Canonical fleets are actionable only through the runtime overlay,
        # after the current OneBot connection has verified its live UIN.
        qq_onebot: dict[str, Any] | None = None
        qq_onebot_instances: dict[str, dict[str, Any]] = {}
    else:
        qq_onebot, qq_onebot_instances = _render_qq_onebot_transports(qq)
    for instance_id, transport in (qq_transport_overlay or {}).items():
        rendered = _render_qq_onebot(transport)
        if rendered is None:
            continue
        expected_uin = _attr(transport, "expected_uin", None)
        qq_onebot_instances[str(instance_id)] = {
            **rendered,
            "expected_uin": (str(expected_uin) if expected_uin not in (None, "") else None),
        }
    default_instance = _attr(qq, "default_instance", None)
    if not canonical_instances:
        default_instance = "default" if qq_onebot_instances else None
    qq_onebot = qq_onebot_instances.get(str(default_instance or ""))

    return {
        "providers": providers,
        "aliases": aliases_out,
        # The `text_to_speech` tool runs in the agent process, which never
        # sees the gateway config snapshot. Without this the [voice] block
        # would only reach the admin preview route and an operator's UI
        # choice would never affect what channels actually send.
        "voice": _render_voice(_attr(cfg, "voice", None)),
        # Global image-generation binding; same rationale as "voice".
        "image": _render_image(_attr(cfg, "models", None)),
        # Ditto for `web_search`: the agent unit carries no EnvironmentFile,
        # so CORLINMAN_WEB_SEARCH_* is unreachable there and every native
        # deployment silently fell back to the keyless DuckDuckGo scrape.
        "web_search": _render_web_search(_attr(cfg, "web_search", None)),
        # The whole `CORLINMAN_*` knob surface the agent reads — round
        # budgets, compaction thresholds, the execute_code opt-in, the shell
        # sandbox backend — has the same reachability problem, so it rides
        # the sidecar too.
        "agent_runtime": _render_agent_runtime(_attr(cfg, "agent_runtime", None)),
        # W3-1: the [permissions] block — rules / mode / strict /
        # default_action / last_match_wins — is consumed by the agent-side
        # AuthzGate at call time. Same reachability story as the rest: the
        # sidecar is the only channel that works in a native deployment.
        # W3-2: the deprecated [approvals] section is translated into
        # leading permission rules here (double-read window, risk R7), so
        # the agent process only ever sees ONE merged block.
        "permissions": _render_permissions(
            _attr(cfg, "permissions", None), _attr(cfg, "approvals", None)
        ),
        "embedding": embedding,
        # G2 dense retrieval: conversational-memory retrieval runs inside
        # the agent process (its LocalSqliteHost), which never sees the
        # gateway config snapshot — same reachability story as "voice" /
        # "image" above, so the [rag] dense knobs ride the sidecar too.
        "rag": _render_rag(_attr(cfg, "rag", None)),
        "subagent": subagent,
        "tencent_safety": tencent_safety,
        "qq_onebot_default_instance": (
            str(default_instance) if default_instance not in (None, "") else None
        ),
        "qq_onebot": qq_onebot,
        "qq_onebot_instances": qq_onebot_instances,
    }


def _render_rag(section: Any) -> dict[str, Any] | None:
    """Render the ``[rag]`` dense-retrieval knobs for the agent sidecar.

    Only well-typed values are forwarded: ``dense_enabled`` must be a
    literal boolean and the two int knobs reject bools (``isinstance(raw,
    bool)`` is checked before ``int`` — bools are ints in Python). The
    agent-side shape layer (``LocalSqliteHost._dense_settings``) re-parses
    defensively, so dropping a malformed key here just means "default".
    """
    if section is None:
        return None
    out: dict[str, Any] = {}
    dense_enabled = _attr(section, "dense_enabled", None)
    if isinstance(dense_enabled, bool):
        out["dense_enabled"] = dense_enabled
    for key in ("rrf_k", "dense_top_k"):
        value = _attr(section, key, None)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        out[key] = value
    return out or None


def _render_image(models: Any) -> dict[str, Any] | None:
    """Render ``[models].image_provider`` / ``image_model`` for the agent."""
    if models is None:
        return None
    out: dict[str, Any] = {}
    for key in ("image_provider", "image_model"):
        value = _attr(models, key, None)
        if value not in (None, ""):
            out[key] = value
    return out or None


def _render_web_search(section: Any) -> dict[str, Any] | None:
    """Render the ``[web_search]`` block for the agent-process sidecar.

    ``api_key`` goes through :func:`_resolve_secret` so an operator can
    write ``api_key = { env = "SERPAPI_KEY" }`` and keep the literal out of
    ``config.toml`` — the gateway *does* load an ``EnvironmentFile``, so the
    env lookup resolves here even though it never would in the agent.
    """
    if section is None:
        return None
    out: dict[str, Any] = {}
    backend = _attr(section, "backend", None)
    if backend not in (None, ""):
        out["backend"] = str(backend).strip().lower()
    api_key = _resolve_secret(_attr(section, "api_key", None))
    if api_key not in (None, ""):
        out["api_key"] = api_key
    return out or None


#: Keys of the ``[agent_runtime]`` block, grouped by wire type. The
#: renderer is explicit rather than pass-through so a typo in ``config.toml``
#: is dropped here instead of reaching the agent as a silently-ignored key.
_AGENT_RUNTIME_INT_KEYS: tuple[str, ...] = (
    "max_rounds",
    "tool_result_cap",
    "tool_result_spill",
    "turn_output_budget",
    "context_budget",
    "context_reserve_cap",
    "context_reserve_tokens",
    "compact_summary_cooldown_rounds",
    "compact_summary_breaker_limit",
    "shell_tasks_max",
    "shell_task_max_lifetime_s",
    "shell_task_max_log_bytes",
    "shell_task_read_max_bytes",
    "mailbox_maxsize",
    "skill_refresh_interval_ms",
)
_AGENT_RUNTIME_FLOAT_KEYS: tuple[str, ...] = (
    "context_reserve_fraction",
    "compact_summary_threshold",
)
_AGENT_RUNTIME_BOOL_KEYS: tuple[str, ...] = (
    "enable_execute_code",
    "web_fetch_allow_private",
    "require_read_before_edit",
    "strict_mode",
)
_AGENT_RUNTIME_STR_KEYS: tuple[str, ...] = (
    "sandbox_backend",
    "sandbox_image",
    "sandbox_user",
    "python",
)


def _render_agent_runtime(section: Any) -> dict[str, Any] | None:
    """Render the ``[agent_runtime]`` block for the agent-process sidecar.

    Only keys the operator actually set are emitted. An absent key must
    stay absent rather than being rendered as its built-in default —
    ``AgentRuntimeDefaults`` uses ``None`` to mean "not configured", which
    is what lets the legacy env layer still apply per knob.
    """
    if section is None:
        return None
    out: dict[str, Any] = {}
    for key in _AGENT_RUNTIME_INT_KEYS:
        value = _attr(section, key, None)
        if value is None or isinstance(value, bool):
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    for key in _AGENT_RUNTIME_FLOAT_KEYS:
        value = _attr(section, key, None)
        if value is None or isinstance(value, bool):
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    for key in _AGENT_RUNTIME_BOOL_KEYS:
        value = _attr(section, key, None)
        if isinstance(value, bool):
            out[key] = value
    for key in _AGENT_RUNTIME_STR_KEYS:
        value = _attr(section, key, None)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out or None


#: Valid rule actions / memories — mirrors ``corlinman_agent.authz``. Kept
#: literal here so the gateway renderer has no import-time dependency on
#: the agent package's internals.
_PERMISSION_ACTIONS: frozenset[str] = frozenset({"allow", "deny", "ask", "log"})
_PERMISSION_MEMORIES: frozenset[str] = frozenset({"once", "session", "always"})
#: String keys accepted inside a rule's ``scope`` (new) / ``match``
#: (legacy alias) table.
_PERMISSION_SCOPE_KEYS: tuple[str, ...] = (
    "model",
    "session",
    "session_pattern",
    "user",
    "user_pattern",
    "tenant",
    "surface",
)


def _render_scope(raw: Any) -> dict[str, str] | None:
    """Whitelist-filter one ``scope`` / ``match`` table (strings only)."""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, str] = {}
    for key in _PERMISSION_SCOPE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out or None


def _render_rule_list(raw: Any) -> tuple[list[dict[str, Any]], int]:
    """Whitelist-filter the ``[[permissions.rules]]`` array.

    Structured data, not a scalar — every rule is rebuilt field by field so
    a typo'd key is dropped HERE (visibly, via the dropped counter) instead
    of travelling to the agent as a silently-ignored key. Returns
    ``(rules, dropped_count)``; a rule without a valid ``tool`` + ``action``
    is dropped entirely.
    """
    if not isinstance(raw, list):
        return [], 0
    rules: list[dict[str, Any]] = []
    dropped = 0
    for entry in raw:
        if not isinstance(entry, Mapping):
            dropped += 1
            continue
        tool = entry.get("tool")
        action = entry.get("action")
        if (
            not isinstance(tool, str)
            or not tool.strip()
            or not isinstance(action, str)
            or action.strip() not in _PERMISSION_ACTIONS
        ):
            dropped += 1
            continue
        rendered: dict[str, Any] = {
            "tool": tool.strip(),
            "action": action.strip(),
        }
        arg_pattern = entry.get("arg_pattern")
        if isinstance(arg_pattern, str) and arg_pattern.strip():
            rendered["arg_pattern"] = arg_pattern.strip()
        note = entry.get("note")
        if isinstance(note, str) and note.strip():
            rendered["note"] = note.strip()
        memory = entry.get("memory")
        if isinstance(memory, str) and memory.strip().lower() in _PERMISSION_MEMORIES:
            rendered["memory"] = memory.strip().lower()
        scope = _render_scope(entry.get("scope"))
        if scope is not None:
            rendered["scope"] = scope
        else:
            legacy = _render_scope(entry.get("match"))
            if legacy is not None:
                rendered["match"] = legacy
        rules.append(rendered)
    return rules, dropped


def _translated_approvals_rules(approvals: Any) -> list[dict[str, Any]]:
    """Translate a deprecated ``[approvals]`` section (W3-2, plan §2.2).

    Lazy import + best-effort: a translation failure must not take the
    sidecar render down — it degrades to "no translated rules" with a
    warning, and the operator's own ``[permissions]`` block still renders.
    """
    if approvals is None:
        return []
    try:
        from corlinman_agent.authz.approvals_compat import (  # noqa: PLC0415
            translate_approvals_rules,
        )

        return translate_approvals_rules(approvals)
    except Exception as exc:  # noqa: BLE001 — never break the render
        log.warning("gateway.py_config.approvals_translation_failed", error=str(exc))
        return []


def _render_permissions(section: Any, approvals: Any = None) -> dict[str, Any] | None:
    """Render the ``[permissions]`` block for the agent-process sidecar.

    Explicit key whitelist, and — as everywhere in this file — a key the
    operator did not set is NOT emitted: ``None`` means "not configured"
    on the agent side, which is what lets the env layer still apply per
    knob. ``_dropped`` (rule count filtered out for shape problems) is a
    meta field for doctor/diagnostics; the agent-side parser ignores it.

    ``approvals`` is the deprecated ``[approvals]`` section: its translated
    rules are PREPENDED to the operator's ``[[permissions.rules]]`` so the
    explicit block wins under last-match-wins (risk R7). Both rule sources
    pass through the same whitelist filter.
    """
    translated = _translated_approvals_rules(approvals)
    if section is None and not translated:
        return None
    out: dict[str, Any] = {}
    if section is not None:
        mode = _attr(section, "mode", None)
        if isinstance(mode, str) and mode.strip():
            out["mode"] = mode.strip()
        for key in ("strict", "last_match_wins", "external_tools_enforced"):
            value = _attr(section, key, None)
            if isinstance(value, bool):
                out[key] = value
        default_action = _attr(section, "default_action", None)
        if isinstance(default_action, str) and default_action.strip() in _PERMISSION_ACTIONS:
            out["default_action"] = default_action.strip()
    rules_raw = _attr(section, "rules", None) if section is not None else None
    combined: list[Any] = list(translated)
    if isinstance(rules_raw, (list, tuple)):
        combined.extend(rules_raw)
    if combined or rules_raw is not None:
        rules, dropped = _render_rule_list(combined)
        out["rules"] = rules
        if dropped:
            out["_dropped"] = dropped
    return out or None


def _render_voice(voice: Any) -> dict[str, Any] | None:
    """Render the ``[voice]`` block for the agent-process sidecar.

    Passed through nearly verbatim (including ``backends``, so a custom
    provider defined in the UI is usable by the tool, not just by the
    preview route). ``None`` when unset, which the reader treats as
    "leave the built-in defaults alone".
    """
    if voice is None:
        return None
    out: dict[str, Any] = {}
    for key in ("enabled", "backend", "voice", "model", "format", "instructions", "speed"):
        value = _attr(voice, key, None)
        if value not in (None, ""):
            out[key] = value
    backends = _attr(voice, "backends", None)
    if isinstance(backends, Mapping):
        out["backends"] = {str(k): dict(v) for k, v in backends.items() if isinstance(v, Mapping)}
    return out or None


def _render_qq_onebot_transports(
    qq: Any,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    instances = _attr(qq, "instances", None)
    if not isinstance(instances, Mapping):
        rendered = _render_qq_onebot(qq)
        return rendered, ({"default": rendered} if rendered is not None else {})

    rendered_instances: dict[str, dict[str, Any]] = {}
    for instance_id, config in instances.items():
        if _attr(config, "enabled", False) is not True:
            continue
        expected_uin = _attr(config, "expected_uin", None)
        # Canonical multi-account transports are not actionable until their
        # live OneBot identity has been pinned. The runtime overlay supplies
        # first-bind identities without writing credentials into TOML.
        if expected_uin in (None, ""):
            continue
        rendered = _render_qq_onebot(config)
        if rendered is None:
            continue
        rendered_instances[str(instance_id)] = {
            **rendered,
            "expected_uin": str(expected_uin),
        }
    default_instance = _attr(qq, "default_instance", None)
    default = rendered_instances.get(str(default_instance or ""))
    return default, rendered_instances


def _render_qq_onebot(qq: Any) -> dict[str, Any] | None:
    """Render the agent process's OneBot action transport settings.

    QZone tools execute in the separate agent process, so they cannot read the
    gateway's live QQ mapping directly. Keep this block narrow: only the WS
    action endpoint and access token are needed, and the existing sidecar is
    already the process boundary used for provider credentials.
    """
    if qq is None:
        return None
    ws_url = _attr(qq, "ws_url", None)
    access_token = _attr(qq, "access_token", None)
    if not access_token:
        # The QQ editor exposes this legacy key for NapCat deployments.
        access_token = _attr(qq, "napcat_access_token", None)
    if not ws_url:
        return None
    return {
        "ws_url": str(ws_url),
        "access_token": str(access_token) if access_token else None,
    }


def _iter_providers(cfg: Any) -> Iterable[tuple[str, Any]]:
    src = _attr(cfg, "providers", None)
    if src is None:
        return []
    if hasattr(src, "items") and callable(src.items):
        return list(src.items())
    if isinstance(src, Mapping):
        return list(src.items())
    # Try iterable of pairs.
    try:
        return [(n, e) for n, e in src]
    except TypeError:
        return []


def _iter_aliases(cfg: Any) -> Mapping[str, Any]:
    models = _attr(cfg, "models", None)
    aliases = _attr(models, "aliases", None) if models is not None else None
    if aliases is None:
        return {}
    if isinstance(aliases, Mapping):
        return aliases
    if hasattr(aliases, "items") and callable(aliases.items):
        return dict(aliases.items())
    return {}


def _render_alias(entry: Any) -> dict[str, Any] | None:
    # Shorthand: bare string.
    if isinstance(entry, str):
        return None
    provider = _attr(entry, "provider", None)
    model = _attr(entry, "model", None)
    # Provider-less full-form alias — same treatment as shorthand: omit.
    if not provider or not model:
        return None
    return {
        "provider": str(provider),
        "model": str(model),
        "params": _params_to_json(_attr(entry, "params", {})),
    }


def _render_embedding(emb: Any) -> dict[str, Any] | None:
    if emb is None:
        return None
    return {
        "provider": str(_attr(emb, "provider", "") or ""),
        "model": str(_attr(emb, "model", "") or ""),
        "dimension": int(_attr(emb, "dimension", 0) or 0),
        "enabled": bool(_attr(emb, "enabled", True)),
        "params": _params_to_json(_attr(emb, "params", {})),
    }


def _render_subagent(section: Any) -> dict[str, Any] | None:
    if section is None:
        return None
    rendered: dict[str, Any] = {}
    for key in (
        "max_concurrent_per_parent",
        "max_concurrent_per_tenant",
        "max_depth",
        "max_wall_seconds_ceiling",
    ):
        value = _attr(section, key, None)
        if isinstance(value, bool) or value is None:
            continue
        try:
            rendered[key] = int(value)
        except (TypeError, ValueError):
            continue
    return rendered or None


def _kind_for(name: str, entry: Any) -> str | None:
    explicit = _attr(entry, "kind", None)
    if explicit:
        kind = str(explicit)
        if kind in KNOWN_PROVIDER_KINDS:
            return kind
        # Unknown explicit kind — drop, matches the Rust ``None``-branch
        # behaviour.
        return None
    # Fallback: infer from the slot name. Mirrors
    # ``corlinman_core::config::Providers::kind_for`` for the first-party
    # names. Anything unrecognised drops out of the JSON.
    lowered = str(name).lower()
    if lowered in KNOWN_PROVIDER_KINDS:
        return lowered
    return None


def _resolve_secret(secret: Any) -> str | None:
    """Resolve a ``SecretRef``-shaped value.

    Accepts:

    * ``None`` → ``None``
    * a bare ``str`` → returned as-is (treated as a literal)
    * an object with ``env`` attr → ``os.environ.get(env)``
    * an object with ``value`` attr → ``str(value)``
    * a dict ``{"env": "..."}`` / ``{"value": "..."}`` — same shape as
      the Rust ``SecretRef`` tagged-enum serialization.
    """
    if secret is None:
        return None
    if isinstance(secret, str):
        return secret
    if isinstance(secret, Mapping):
        if "env" in secret:
            return os.environ.get(str(secret["env"]))
        if "value" in secret:
            value = secret["value"]
            return None if value is None else str(value)
        return None
    env = getattr(secret, "env", None)
    if env is not None:
        return os.environ.get(str(env))
    value = getattr(secret, "value", None)
    if value is not None:
        return str(value)
    return None


def _params_to_json(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, Mapping):
        return {str(k): _jsonable(v) for k, v in params.items()}
    if hasattr(params, "items") and callable(params.items):
        return {str(k): _jsonable(v) for k, v in params.items()}
    return {}


def _jsonable(value: Any) -> Any:
    """Pass-through coercion for ``serde_json::Value`` analogues.

    Pydantic models, dataclasses, and plain dict/list/str/int/float/bool
    are all already JSON-safe; anything else falls back to ``str()`` so
    the renderer can't 500 on an unexpected param value.
    """
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump()
    return str(value)


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Mapping-aware ``getattr`` — looks up ``name`` on attributes and
    ``Mapping`` keys, so dicts and Pydantic models both work."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_py_config_sync(
    cfg: Any,
    path: Path | str,
    *,
    qq_transport_overlay: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Synchronously render + atomically write the JSON drop.

    Atomicity: write to a sibling ``<path>.new``, then ``os.rename`` so
    the reader side (``corlinman_server.main._ReloadingProviderResolver``) sees a
    fully-formed file on every mtime bump.
    """
    target = Path(path)
    payload = render_py_config(cfg, qq_transport_overlay=qq_transport_overlay)
    body = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    # ``tempfile.NamedTemporaryFile`` keeps us safe from a half-written
    # file if the process dies mid-write; ``os.replace`` is atomic on
    # the same filesystem.
    fd, tmp = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".new",
        dir=str(target.parent) if target.parent.as_posix() else ".",
    )
    try:
        shared_gid_raw = os.environ.get("CORLINMAN_PY_CONFIG_GID")
        if shared_gid_raw:
            os.fchown(fd, -1, int(shared_gid_raw))
            os.fchmod(fd, 0o640)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        # Persist the directory entry as well as the file contents. This drop
        # carries provider and OneBot credentials, so after a successful return
        # it must survive a host crash without exposing the previous generation.
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        # Clean up the temp file on any failure; the rename window is
        # microscopic but we don't want to leak ``.new`` shards.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def write_py_config(
    cfg: Any,
    path: Path | str,
    *,
    qq_transport_overlay: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Async wrapper around :func:`write_py_config_sync`.

    The Rust version uses ``tokio::fs`` for the rename hop. We stay sync
    here — the JSON payload is small (single-digit kB at most for a
    realistic provider set), and a serial sync write inside the async
    flow doesn't block the loop noticeably. Wrapping in
    :func:`asyncio.to_thread` would only buy us pretend-concurrency.
    """
    write_py_config_sync(
        cfg,
        path,
        qq_transport_overlay=qq_transport_overlay,
    )


__all__ = [
    "DEFAULT_PY_CONFIG_FILENAME",
    "ENV_PY_CONFIG",
    "KNOWN_PROVIDER_KINDS",
    "default_py_config_path",
    "render_py_config",
    "write_py_config",
    "write_py_config_sync",
]
