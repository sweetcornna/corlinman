"""Google (Gemini) provider adapter.

Wraps ``google.genai`` behind
:class:`corlinman_providers.base.CorlinmanProvider`.

Google's Gemini SDK exposes function calls as structured ``Part`` entries
inside each streamed chunk. Gemini usually delivers the whole parsed call
in one ``Part`` once, so the unified streaming translation is:

    * when a chunk carries a ``function_call`` part: emit
      ``tool_call_start`` + ``tool_call_delta`` (with ``json.dumps(args)``)
      + ``tool_call_end`` back-to-back (no partial aggregation needed);
    * text parts → ``token`` chunks.
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import structlog

from corlinman_providers._auth_refresh import (
    refresh_env_key_if_rotated,
    with_401_recovery,
)
from corlinman_providers.base import ProviderChunk
from corlinman_providers.failover import AuthError, CorlinmanError
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)

# Placeholder ``api_key`` used only to satisfy the google-genai ``Client``
# constructor when the real credential travels in a custom auth header
# (declarative ``auth_kind="header"``). The SDK *requires* a truthy
# ``api_key`` to construct (raises ``ValueError`` otherwise) and stamps it
# into the default ``x-goog-api-key`` header — so we feed this non-credential
# sentinel rather than the real key, and the secret rides the declared custom
# header via ``http_options.headers``. When the custom header IS
# ``x-goog-api-key`` the supplied header value overrides the sentinel.
# Mirrors :data:`openai_provider._HEADER_AUTH_SENTINEL`.
_HEADER_AUTH_SENTINEL = "header-auth-no-key"


class GoogleProvider:
    """Google Gemini adapter."""

    name: ClassVar[str] = "google"
    kind: ClassVar[ProviderKind] = ProviderKind.GOOGLE

    GOOGLE_API_KEY_ENV: ClassVar[str] = "GOOGLE_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get(self.GOOGLE_API_KEY_ENV) or None
        # Static headers forwarded on every request. Used by declarative
        # providers whose ``auth_kind == "header"`` carry their credential in
        # a custom header instead of the default ``x-goog-api-key`` auth — see
        # :func:`declarative._build_inner`. ``None`` keeps the historic
        # api_key-only behaviour.
        self._default_headers = default_headers

    async def _refresh_credential(self) -> bool:
        """Reactive 401 path: re-read ``GOOGLE_API_KEY`` and update ``_api_key``.

        Returns ``True`` when the env var carries a non-empty value that
        differs from the in-process key; :func:`with_401_recovery` then
        retries the open phase against Gemini with the new key.
        """
        def _set(new_value: str) -> None:
            self._api_key = new_value

        return await refresh_env_key_if_rotated(
            env_name=self.GOOGLE_API_KEY_ENV,
            current=self._api_key,
            on_update=_set,
        )

    @classmethod
    def build(cls, spec: ProviderSpec) -> GoogleProvider:
        return cls(api_key=spec.api_key)

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """Per-request params accepted by the Gemini generate_content API.

        Note: google-genai maps ``top_p`` to ``top_p`` inside its
        ``GenerateContentConfig`` — we forward it verbatim via ``extra``.
        ``safety_settings`` is the Gemini-specific escape hatch; declared as
        a free-form object because the SDK validates its own shape.
        """
        return _GOOGLE_PARAMS_SCHEMA

    async def chat_stream(
        self,
        *,
        model: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        # Custom-header auth carries the credential in ``_default_headers``
        # rather than ``_api_key``, so a missing ``_api_key`` is only an error
        # when no header credential is configured either.
        if not self._api_key and not self._default_headers:
            raise RuntimeError("API key missing: set GOOGLE_API_KEY")

        from google import genai  # type: ignore[import-not-found]
        from google.genai import types  # type: ignore[import-not-found]

        # Translate the unified message list into Gemini's structured
        # ``Content`` turns. ``content`` may be a plain string (text-only
        # callers) or an OpenAI-shaped content-parts list (``[{"type":
        # "text", ...}, {"type": "image_url", ...}]`` produced by
        # ``reasoning_loop._inject_attachments``). Building real ``Part``
        # objects keeps images as inline media instead of repr-flattening
        # the list into a string, and maps roles so multi-turn history
        # round-trips (Gemini uses ``user`` / ``model``).
        contents = _build_contents(messages, types)

        config: dict[str, Any] = {}
        if temperature is not None:
            config["temperature"] = temperature
        if max_tokens:
            config["max_output_tokens"] = max_tokens
        if tools:
            config["tools"] = _normalise_tools(tools)
        if extra:
            config.update(extra)

        async def _open() -> Any:
            """Build the Gemini client + open the streaming generator.

            Wrapped by :func:`with_401_recovery` so a stale ``GOOGLE_API_KEY``
            (rotated outside the process between adapter construction and
            this request) is re-read and the open is retried once.
            """
            if self._default_headers:
                # Custom-header auth: the real credential rides in the declared
                # header via ``http_options.headers``. The SDK requires a
                # truthy ``api_key``, so pass the non-credential sentinel — the
                # default ``x-goog-api-key`` then carries the sentinel, never
                # the secret. (When the custom header IS ``x-goog-api-key`` the
                # supplied header value overrides the sentinel.)
                client = genai.Client(
                    api_key=self._api_key or _HEADER_AUTH_SENTINEL,
                    http_options=types.HttpOptions(
                        headers=dict(self._default_headers)
                    ),
                )
            else:
                client = genai.Client(api_key=self._api_key)
            try:
                return await client.aio.models.generate_content_stream(
                    model=model,
                    contents=contents,
                    # google-genai accepts a plain dict at runtime but declares
                    # a stricter ``GenerateContentConfig | GenerateContentConfigDict``
                    # in its stubs; M3 will switch to the typed config builder.
                    config=config or None,  # type: ignore[arg-type]
                )
            except CorlinmanError:
                raise
            except Exception as exc:
                raise _map_google_error(exc, model=model) from exc

        try:
            gen = await with_401_recovery(
                _open, refresh=self._refresh_credential, provider=self.name
            )
            finish = "stop"
            synthetic_call_index = 0
            async for chunk in gen:
                text = getattr(chunk, "text", None) or ""
                if text:
                    yield ProviderChunk(kind="token", text=text)
                for function_call in _iter_function_calls(chunk):
                    finish = "tool_calls"
                    call_id = _get(function_call, "id")
                    if not call_id:
                        call_id = f"call_{synthetic_call_index}"
                        synthetic_call_index += 1
                    name = _get(function_call, "name") or ""
                    args = _get(function_call, "args") or {}
                    yield ProviderChunk(
                        kind="tool_call_start",
                        tool_call_id=call_id,
                        tool_name=name,
                    )
                    yield ProviderChunk(
                        kind="tool_call_delta",
                        tool_call_id=call_id,
                        arguments_delta=json.dumps(_jsonable(args)),
                    )
                    yield ProviderChunk(kind="tool_call_end", tool_call_id=call_id)
            yield ProviderChunk(kind="done", finish_reason=finish)
        except CorlinmanError:
            raise
        except Exception as exc:
            raise CorlinmanError(str(exc), provider="google", model=model) from exc

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        raise NotImplementedError("Google embeddings land with the RAG pipeline in M3")

    @classmethod
    def supports(cls, model: str) -> bool:
        return model.startswith("gemini-")


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _get_any(obj: Any, *keys: str) -> Any:
    for key in keys:
        value = _get(obj, key)
        if value is not None:
            return value
    return None


def _gemini_role(role: str | None) -> str:
    """Map a unified message role to a Gemini ``Content`` role.

    Gemini only recognises ``user`` and ``model``. ``system`` turns are
    folded into the ``user`` stream (the dedicated system-instruction
    config slot is wired separately via ``system_prompt`` in ``extra``);
    everything that isn't an explicit user turn (assistant / tool) maps to
    ``model`` so multi-turn history keeps alternating correctly.
    """
    if role in (None, "user", "system"):
        return "user"
    return "model"


def _build_contents(messages: Sequence[Any], types: Any) -> list[Any]:
    """Translate unified messages into structured Gemini ``Content`` turns.

    Each turn's ``content`` is either a plain string (one text part) or an
    OpenAI-shaped content-parts list. ``image_url`` parts become real inline
    image ``Part``s (``data:`` URLs are decoded to bytes; remote URLs use
    ``Part.from_uri``); text parts become text ``Part``s. Empty turns are
    dropped so we never hand Gemini a content-less ``Content``.
    """
    contents: list[Any] = []
    for m in messages:
        role = _gemini_role(_get(m, "role"))
        content = _get(m, "content")
        parts = _content_to_parts(content, types)
        if parts:
            contents.append(types.Content(role=role, parts=parts))
    return contents


def _content_to_parts(content: Any, types: Any) -> list[Any]:
    """Build a list of Gemini ``Part`` objects from unified message content."""
    if content is None:
        return []
    if isinstance(content, str):
        return [types.Part.from_text(text=content)] if content else []
    if not isinstance(content, list):
        text = str(content)
        return [types.Part.from_text(text=text)] if text else []

    parts: list[Any] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        ptype = raw.get("type")
        if ptype == "text":
            text = raw.get("text") or ""
            if text:
                parts.append(types.Part.from_text(text=text))
        elif ptype == "image_url":
            url = (raw.get("image_url") or {}).get("url") or ""
            part = _image_part_from_url(url, types)
            if part is not None:
                parts.append(part)
        elif ptype == "file":
            part = _file_part_from_file(raw.get("file") or {}, types)
            if part is not None:
                parts.append(part)
        # Other unknown part types are skipped — dropping beats failing
        # the whole request.
    return parts


def _file_part_from_file(f: dict[str, Any], types: Any) -> Any | None:
    """Build a Gemini ``Part`` from an OpenAI ``file`` part.

    Gemini accepts PDFs / audio / video as inline bytes, so a part that
    carries an inline ``file_data`` data URL maps straight to
    ``Part.from_bytes``. Parts without inline data (url-only) degrade to
    a text part naming the attachment, so the model can tell the user it
    can't fetch it (gateway-private urls are unreachable from Google).
    """
    name = str(f.get("file_name") or f.get("filename") or "attachment")
    data_url = str(f.get("file_data") or "")
    if data_url.startswith("data:") and ";base64," in data_url:
        header, b64 = data_url.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "application/octet-stream"
        try:
            data = base64.b64decode(b64)
        except (binascii.Error, ValueError):
            return None
        return types.Part.from_bytes(data=data, mime_type=mime)
    return types.Part.from_text(
        text=(
            f"[attachment {name!r} was provided but could not be passed "
            "to this model]"
        )
    )


def _image_part_from_url(url: str, types: Any) -> Any | None:
    """Build a Gemini image ``Part`` from an OpenAI ``image_url`` value.

    ``data:<mime>;base64,<payload>`` URIs are decoded to inline bytes
    (``Part.from_bytes``); remote ``http(s)`` URLs are passed through as a
    file-URI part. Returns ``None`` for an empty or malformed url so the
    caller skips it rather than emitting a junk part.
    """
    if not url:
        return None
    if url.startswith("data:") and ";base64," in url:
        header, b64 = url.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "image/jpeg"
        try:
            data = base64.b64decode(b64)
        except (binascii.Error, ValueError):
            return None
        return types.Part.from_bytes(data=data, mime_type=mime)
    # ``from_uri`` requires a mime_type (google-genai raises without
    # one) — infer from the url's extension and fall back to JPEG, the
    # most common remote-image case.
    guessed = mimetypes.guess_type(url)[0]
    return types.Part.from_uri(
        file_uri=url, mime_type=guessed or "image/jpeg"
    )


def _iter_function_calls(chunk: Any) -> list[Any]:
    direct_calls = getattr(chunk, "function_calls", None)
    if direct_calls:
        return list(direct_calls)

    calls: list[Any] = []
    parts = getattr(chunk, "parts", None)
    if parts is None:
        parts = []
        for candidate in getattr(chunk, "candidates", None) or []:
            content = _get(candidate, "content")
            parts.extend(_get(content, "parts") or [])

    for part in parts:
        function_call = _get_any(part, "function_call", "functionCall")
        if function_call is not None:
            calls.append(function_call)
    return calls


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _map_google_error(exc: Exception, *, model: str) -> CorlinmanError:
    """Best-effort 401 detection for the Gemini SDK.

    google-genai surfaces failures as a small family of typed
    exceptions (``ClientError`` / ``ServerError``) plus the lower-level
    ``google.api_core`` ones. We sniff for the documented HTTP status
    via the SDK's ``code`` / ``status_code`` attributes and fall back
    to a substring scan on the message so the reactive auth refresh
    path catches 401s without taking a hard dependency on every
    version of every Google library.

    All non-401 errors stay generic :class:`CorlinmanError` to keep
    behavioural parity with the pre-refresh adapter.
    """
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    if status_int in (401, 403):
        return AuthError(str(exc), status_code=status_int, provider="google", model=model)
    msg = str(exc).lower()
    if "api key" in msg and ("invalid" in msg or "expired" in msg or "unauthorized" in msg):
        return AuthError(str(exc), status_code=401, provider="google", model=model)
    if "401" in msg or "unauthenticated" in msg or "permission_denied" in msg:
        return AuthError(str(exc), status_code=401, provider="google", model=model)
    return CorlinmanError(str(exc), provider="google", model=model)


def _normalise_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    passthrough: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict):
            passthrough.append(tool)
            continue

        declaration: dict[str, Any] = {"name": function.get("name", "")}
        if function.get("description"):
            declaration["description"] = function["description"]
        parameters = function.get("parameters")
        if parameters:
            declaration["parameters"] = parameters
        declarations.append(declaration)

    normalised = list(passthrough)
    if declarations:
        normalised.append({"function_declarations": declarations})
    return normalised


# Hand-authored JSON Schema (draft 2020-12). ``safety_settings`` is
# free-form: the google-genai SDK validates its internal shape and we don't
# want to duplicate that here — declare as an object with no constraints.
_GOOGLE_PARAMS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "temperature": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 2.0,
            "description": "Sampling temperature.",
        },
        "top_p": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Nucleus sampling probability mass.",
        },
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "description": "max_output_tokens in Gemini terminology.",
        },
        "system_prompt": {
            "type": "string",
            "maxLength": 16000,
            "description": "System instruction; concatenated with any history.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "description": "Client-side request timeout in milliseconds.",
        },
        "safety_settings": {
            "type": "object",
            "additionalProperties": True,
            "description": "Forwarded verbatim to google-genai (shape validated by SDK).",
        },
    },
}
