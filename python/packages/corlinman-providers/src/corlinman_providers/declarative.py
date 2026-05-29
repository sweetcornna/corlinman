"""Declarative TOML-driven provider scaffold.

Goal: let operators onboard a new LLM provider by dropping a ``*.toml`` file
into the ``spec/`` directory — no Python subclass of
:class:`CorlinmanProvider` required. Existing class-based providers
(``OpenAIProvider``, ``AnthropicProvider``, ``GoogleProvider``, …) are
unaffected; this module is purely additive.

Architecture
------------

* :class:`DeclarativeProviderSpec` — frozen dataclass capturing everything
  we need to talk to an upstream gateway: id, name, base URL, auth shape,
  request wire-format, and a catalogue of models.
* :class:`DeclarativeProvider` — runtime adapter that *composes* one of the
  existing vendor adapters (:class:`OpenAIProvider` /
  :class:`AnthropicProvider` / :class:`GoogleProvider`) chosen by the
  spec's ``request_format`` and delegates ``chat_stream`` / ``embed`` /
  ``supports`` to it. This keeps SSE parsing + tool_calls normalisation in
  one place (the vendor adapters) instead of duplicating the logic here.
* :func:`load_spec_from_toml` + :func:`load_all_specs` — parse TOML files
  into :class:`DeclarativeProviderSpec` instances.

Conflict policy (see :mod:`corlinman_providers.registry`):
  class-based provider specs win. A TOML spec whose ``id`` collides with a
  class-based provider that's already built is **dropped** with a WARNING —
  this prevents operators from accidentally shadowing vetted built-ins.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from corlinman_providers.anthropic_provider import AnthropicProvider
from corlinman_providers.base import CorlinmanProvider, ProviderChunk
from corlinman_providers.google_provider import GoogleProvider
from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind

logger = structlog.get_logger(__name__)


AuthKind = Literal["bearer_api_key", "header", "query_param", "none"]
RequestFormat = Literal["openai_compatible", "anthropic_compatible", "gemini_compatible"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Single row of a declarative provider's ``[models.*]`` table."""

    id: str
    """Upstream model id as sent on the wire (e.g. ``"moonshot-v1-32k"``)."""

    context_length: int
    """Maximum context window in tokens — advisory only, not validated here."""

    supports_tools: bool = False
    supports_vision: bool = False


@dataclass(frozen=True, slots=True)
class DeclarativeProviderSpec:
    """All the metadata needed to wire up a provider from TOML alone.

    The shape mirrors openclaw's ``defineSingleProviderPluginEntry({id,
    name, auth, catalog})`` — fill these fields and you have a working
    adapter without authoring a Python class.
    """

    id: str
    """Short id, matches the TOML filename stem (e.g. ``"moonshot"``). Also
    used as the :attr:`ProviderSpec.name` key in the registry."""

    name: str
    """Human-readable display name (e.g. ``"Moonshot (月之暗面)"``)."""

    base_url: str
    """Upstream base URL. Required even for ``none`` auth."""

    auth_kind: AuthKind
    auth_config: dict[str, Any]
    """Shape depends on :attr:`auth_kind`:

    * ``bearer_api_key`` → ``{"env_var": "FOO_API_KEY"}``
    * ``header``         → ``{"env_var": "...", "header_name": "X-API-Key",
                               "value_prefix": ""}``
    * ``query_param``    → ``{"env_var": "...", "param_name": "api_key"}``
    * ``none``           → ``{}``

    Honoured at runtime for ``request_format == "openai_compatible"``:
    ``bearer_api_key``, ``none``, and ``header`` (the key is sent in the
    declared ``header_name`` with an optional ``value_prefix``).
    ``query_param`` is accepted by the loader but rejected at build time —
    the inner OpenAI-wire client can't carry request-query auth, so it raises
    rather than silently bearer-authing (see :meth:`DeclarativeProvider._build_inner`).
    """

    request_format: RequestFormat
    chat_endpoint: str = "/chat/completions"
    models: dict[str, ModelSpec] = field(default_factory=dict)
    """Map of ``logical_name`` (e.g. ``"default"``, ``"long"``) →
    :class:`ModelSpec`. Logical names are the user-facing choice; callers
    resolve to :attr:`ModelSpec.id` before hitting the wire."""

    params: dict[str, Any] = field(default_factory=dict)
    """Provider-level default request params (same role as
    :attr:`ProviderSpec.params`)."""


class DeclarativeProvider:
    """Runtime adapter built from a :class:`DeclarativeProviderSpec`.

    Composes one of the existing vendor adapters under the hood
    (selected by :attr:`DeclarativeProviderSpec.request_format`) and
    delegates the streaming + embedding contract to it. This way every
    declarative provider inherits the vendor adapter's battle-tested
    SSE parsing, tool_calls normalisation, and error mapping.

    :attr:`kind` is stamped as ``ProviderKind.OPENAI_COMPATIBLE`` for all
    declarative providers — the admin UI should distinguish "declarative"
    from "class-based" via the presence of a :class:`DeclarativeProviderSpec`,
    not via :class:`ProviderKind`.
    """

    # Instance attribute, not ClassVar — each provider carries its own id.
    kind = ProviderKind.OPENAI_COMPATIBLE

    def __init__(
        self,
        spec: DeclarativeProviderSpec,
        api_key: str | None = None,
    ) -> None:
        self._spec = spec
        self.name = spec.id
        self._api_key = self._resolve_api_key(spec, api_key)
        # _inner satisfies the CorlinmanProvider Protocol (runtime-checkable)
        # so we can type it uniformly even though the three candidates have
        # no shared nominal base class.
        self._inner: CorlinmanProvider = self._build_inner(spec, self._api_key)

    # -- Public surface (CorlinmanProvider Protocol) ------------------------

    def chat_stream(
        self,
        *,
        model: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        """Delegate to the composed vendor adapter — matches the Protocol
        signature (declared ``def`` because the vendor method is an async
        generator function; see :class:`CorlinmanProvider` docstring)."""
        return self._inner.chat_stream(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra,
        )

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        return await self._inner.embed(model=model, inputs=inputs, extra=extra)

    @classmethod
    def supports(cls, model: str) -> bool:
        """Declarative providers never claim a raw model id via the legacy
        prefix table — they're always addressed by explicit alias."""
        return False

    # -- Declarative-only helpers ------------------------------------------

    def list_models(self) -> list[ModelSpec]:
        """Return every :class:`ModelSpec` declared by the underlying spec."""
        return list(self._spec.models.values())

    @property
    def spec(self) -> DeclarativeProviderSpec:
        return self._spec

    # -- Internals ---------------------------------------------------------

    @staticmethod
    def _resolve_api_key(
        spec: DeclarativeProviderSpec,
        explicit: str | None,
    ) -> str | None:
        """Explicit arg wins, then ``auth_config.env_var``, then ``None``.

        ``auth_kind == "none"`` short-circuits to ``None`` (some local
        gateways accept unauthenticated requests).
        """
        if explicit:
            return explicit
        if spec.auth_kind == "none":
            return None
        env_var = spec.auth_config.get("env_var")
        if isinstance(env_var, str) and env_var:
            return os.environ.get(env_var)
        return None

    @staticmethod
    def _build_inner(
        spec: DeclarativeProviderSpec,
        api_key: str | None,
    ) -> CorlinmanProvider:
        """Dispatch ``request_format`` to the matching vendor adapter.

        Auth wiring (``openai_compatible`` only):

        * ``bearer_api_key`` / ``none`` → the key is forwarded as
          ``Authorization: Bearer`` (or omitted) via the OpenAI SDK.
        * ``header`` → the key is sent in the declared custom header
          (``auth_config.header_name``, optional ``value_prefix``) instead of
          as a bearer; the inner :class:`OpenAIProvider` is given
          ``default_headers`` and no bearer api_key.
        * ``query_param`` → not yet supported by the inner OpenAI-wire client;
          we raise here rather than silently bearer-authing so the operator
          knows their declared auth shape isn't honoured.

        Note: vendor adapters declare ``name`` as ``ClassVar[str]`` while
        the :class:`CorlinmanProvider` Protocol declares an instance
        ``name``; mypy flags the mismatch even though the attribute is
        structurally present. We ``cast`` to shut this up — runtime
        isinstance-against-Protocol still works because the Protocol is
        ``runtime_checkable``.
        """
        if spec.request_format == "openai_compatible":
            if spec.auth_kind == "header":
                return cast(
                    CorlinmanProvider,
                    OpenAIProvider(
                        api_key=None,
                        base_url=spec.base_url,
                        default_headers=_build_custom_auth_headers(spec, api_key),
                    ),
                )
            if spec.auth_kind == "query_param":
                raise ValueError(
                    f"{spec.id}: query_param auth not yet supported by the "
                    "openai_compatible inner client (the OpenAI SDK has no "
                    "request-query auth hook); use bearer_api_key or header."
                )
            return cast(
                CorlinmanProvider,
                OpenAIProvider(api_key=api_key, base_url=spec.base_url),
            )
        if spec.request_format == "anthropic_compatible":
            if spec.base_url:
                logger.warning(
                    "declarative.base_url_ignored",
                    id=spec.id,
                    request_format=spec.request_format,
                    base_url=spec.base_url,
                )
            return cast(CorlinmanProvider, AnthropicProvider(api_key=api_key))
        if spec.request_format == "gemini_compatible":
            if spec.base_url:
                logger.warning(
                    "declarative.base_url_ignored",
                    id=spec.id,
                    request_format=spec.request_format,
                    base_url=spec.base_url,
                )
            return cast(CorlinmanProvider, GoogleProvider(api_key=api_key))
        # Literal type keeps the compiler honest; runtime check guards
        # malformed TOML that bypasses the Literal via an untyped load.
        raise ValueError(f"unknown request_format: {spec.request_format!r}")


def _build_custom_auth_headers(
    spec: DeclarativeProviderSpec,
    api_key: str | None,
) -> dict[str, str]:
    """Build the ``default_headers`` mapping for ``auth_kind == "header"``.

    Reads ``header_name`` (required) and optional ``value_prefix`` from
    ``spec.auth_config`` and emits ``{header_name: f"{value_prefix}{api_key}"}``.
    Raises ``ValueError`` on a missing/blank ``header_name`` or a missing
    ``api_key`` so the misconfiguration surfaces at build time rather than as
    a silent unauthenticated request.
    """
    header_name = spec.auth_config.get("header_name")
    if not isinstance(header_name, str) or not header_name:
        raise ValueError(
            f"{spec.id}: auth_kind='header' requires a non-empty "
            "auth_config.header_name"
        )
    if not api_key:
        raise ValueError(
            f"{spec.id}: auth_kind='header' requires an api_key (set "
            f"{spec.auth_config.get('env_var') or 'the configured env_var'})"
        )
    prefix = spec.auth_config.get("value_prefix", "")
    if not isinstance(prefix, str):
        prefix = ""
    return {header_name: f"{prefix}{api_key}"}


# ---- TOML loading ---------------------------------------------------------


_VALID_AUTH_KINDS: set[str] = {"bearer_api_key", "header", "query_param", "none"}
_VALID_REQUEST_FORMATS: set[str] = {
    "openai_compatible",
    "anthropic_compatible",
    "gemini_compatible",
}


def load_spec_from_toml(path: Path) -> DeclarativeProviderSpec:
    """Parse one TOML file into a :class:`DeclarativeProviderSpec`.

    Raises ``ValueError`` (or ``tomllib.TOMLDecodeError``) on malformed
    input — caller decides whether to fail-fast or skip.
    """
    with path.open("rb") as f:
        data = tomllib.load(f)

    missing = [k for k in ("id", "name", "base_url", "auth_kind", "request_format") if k not in data]
    if missing:
        raise ValueError(f"{path.name}: missing required keys: {missing}")

    auth_kind = data["auth_kind"]
    if auth_kind not in _VALID_AUTH_KINDS:
        raise ValueError(f"{path.name}: invalid auth_kind {auth_kind!r}")
    request_format = data["request_format"]
    if request_format not in _VALID_REQUEST_FORMATS:
        raise ValueError(f"{path.name}: invalid request_format {request_format!r}")

    models_raw = data.get("models") or {}
    if not isinstance(models_raw, dict):
        raise ValueError(f"{path.name}: [models] must be a table")
    models: dict[str, ModelSpec] = {}
    for logical_name, row in models_raw.items():
        if not isinstance(row, dict):
            raise ValueError(f"{path.name}: models.{logical_name} must be a table")
        if "id" not in row:
            raise ValueError(f"{path.name}: models.{logical_name} missing 'id'")
        if "context_length" not in row:
            raise ValueError(f"{path.name}: models.{logical_name} missing 'context_length'")
        models[logical_name] = ModelSpec(
            id=str(row["id"]),
            context_length=int(row["context_length"]),
            supports_tools=bool(row.get("supports_tools", False)),
            supports_vision=bool(row.get("supports_vision", False)),
        )

    auth_config_raw = data.get("auth_config") or {}
    if not isinstance(auth_config_raw, dict):
        raise ValueError(f"{path.name}: [auth_config] must be a table")

    params_raw = data.get("params") or {}
    if not isinstance(params_raw, dict):
        raise ValueError(f"{path.name}: [params] must be a table")

    return DeclarativeProviderSpec(
        id=str(data["id"]),
        name=str(data["name"]),
        base_url=str(data["base_url"]),
        auth_kind=auth_kind,  # type: ignore[arg-type]  # Literal narrowed by the set check above
        auth_config=dict(auth_config_raw),
        request_format=request_format,  # type: ignore[arg-type]  # same
        chat_endpoint=str(data.get("chat_endpoint", "/chat/completions")),
        models=models,
        params=dict(params_raw),
    )


def load_all_specs(spec_dir: Path) -> list[DeclarativeProviderSpec]:
    """Walk ``spec_dir`` and parse every top-level ``*.toml`` file.

    Missing directory → ``[]`` (no specs declared, not an error). Files
    that fail to parse are logged and skipped so one bad spec doesn't
    take down the whole registry.

    (Parameter is named ``spec_dir`` rather than ``dir`` so the signature
    doesn't shadow the Python builtin — the task skeleton used ``dir`` but
    the project's ruff config forbids it.)
    """
    if not spec_dir.exists() or not spec_dir.is_dir():
        return []
    specs: list[DeclarativeProviderSpec] = []
    for path in sorted(spec_dir.glob("*.toml")):
        try:
            specs.append(load_spec_from_toml(path))
        except Exception as exc:
            logger.warning("declarative.spec_parse_failed", path=str(path), error=str(exc))
    return specs


__all__ = [
    "AuthKind",
    "DeclarativeProvider",
    "DeclarativeProviderSpec",
    "ModelSpec",
    "RequestFormat",
    "load_all_specs",
    "load_spec_from_toml",
]
