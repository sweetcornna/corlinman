"""AWS Bedrock provider adapter.

Bedrock fronts several model families behind a single
``bedrock-runtime`` API. We target ``InvokeModelWithResponseStream`` — the
streaming counterpart of ``InvokeModel`` — and translate the AWS
event-stream framing into corlinman :class:`ProviderChunk` values.

Auth is **AWS SigV4** (request signing), implemented by hand against
:mod:`httpx` — see :mod:`corlinman_providers._aws_sigv4`. No ``boto3`` /
``aioboto3`` dependency: the SDK is multi-megabyte and we only need to sign
one request shape.

Scope: **Anthropic-on-Bedrock** model ids
(``anthropic.claude-*`` / cross-region ``*.anthropic.claude-*``). For those
the per-model request/response body is the Anthropic *Messages* API shape
with ``anthropic_version`` pinned in the body and the model id carried in
the URL path instead. The Bedrock chunk payloads are exactly the Anthropic
streaming SSE events (``content_block_start`` / ``content_block_delta`` /
``message_delta`` …), so the tool-call translation mirrors
:class:`corlinman_providers.anthropic_provider.AnthropicProvider`.

Config (``[providers.<name>]`` with ``kind = "bedrock"``):

* ``params.region`` *(required)* — AWS region, e.g. ``us-east-1``. Also
  read from ``AWS_REGION`` / ``AWS_DEFAULT_REGION``.
* credentials — either ``api_key`` formatted as ``"<access_key>:<secret>"``
  (optionally ``"<access_key>:<secret>:<session_token>"``), or the standard
  ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``
  environment variables.
* ``base_url`` *(optional)* — override the endpoint host (defaults to the
  regional ``bedrock-runtime.<region>.amazonaws.com``).
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
import structlog

from corlinman_providers._aws_eventstream import (
    EventStreamDecoder,
    EventStreamError,
)
from corlinman_providers._aws_sigv4 import AwsCredentials, sigv4_headers
from corlinman_providers.base import ProviderChunk
from corlinman_providers.failover import (
    AuthError,
    AuthPermanentError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    FormatError,
    ModelNotFoundError,
    OverloadedError,
    RateLimitError,
    TimeoutError,  # noqa: A004 — intentional shadowing; see failover.TimeoutError
)
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)

#: ``bedrock-runtime`` SigV4 service code.
_SERVICE = "bedrock"

#: ``anthropic_version`` value Bedrock expects in the request body for the
#: Anthropic Messages shape. This is the Bedrock-specific constant, not the
#: public-API date.
_BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"

#: Default request timeout (seconds) for the streaming POST.
_DEFAULT_TIMEOUT_S = 120.0


class BedrockProvider:
    """AWS Bedrock adapter — SigV4-signed ``InvokeModelWithResponseStream``."""

    name: ClassVar[str] = "bedrock"
    kind: ClassVar[ProviderKind] = ProviderKind.BEDROCK

    def __init__(
        self,
        *,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        region: str | None = None,
        base_url: str | None = None,
        instance_name: str | None = None,
    ) -> None:
        self._access_key_id = access_key_id or os.environ.get("AWS_ACCESS_KEY_ID")
        self._secret_access_key = secret_access_key or os.environ.get(
            "AWS_SECRET_ACCESS_KEY"
        )
        self._session_token = session_token or os.environ.get("AWS_SESSION_TOKEN")
        self._region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        self._base_url = base_url
        if instance_name:
            self.__dict__["name"] = instance_name

    @classmethod
    def build(cls, spec: ProviderSpec) -> BedrockProvider:
        """Construct from a :class:`ProviderSpec`.

        The region comes from ``params.region``; credentials come from
        ``api_key`` (``"<access>:<secret>[:<token>]"``) when present,
        otherwise from the standard ``AWS_*`` environment variables.
        """
        params = spec.params or {}
        region = params.get("region")
        access_key_id = secret_access_key = session_token = None
        if spec.api_key:
            access_key_id, secret_access_key, session_token = _parse_api_key(
                spec.api_key
            )
        return cls(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            region=region if isinstance(region, str) else None,
            base_url=spec.base_url,
            instance_name=spec.name,
        )

    def _credentials(self) -> AwsCredentials:
        """Resolve the AWS credential triple or raise a clear config error."""
        if not self._access_key_id or not self._secret_access_key:
            raise RuntimeError(
                "AWS credentials missing for Bedrock provider: set "
                "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (or provide "
                "api_key = '<access>:<secret>' in the provider config)"
            )
        return AwsCredentials(
            access_key_id=self._access_key_id,
            secret_access_key=self._secret_access_key,
            session_token=self._session_token,
        )

    def _endpoint(self) -> tuple[str, str]:
        """Return ``(host, scheme)`` for the bedrock-runtime endpoint."""
        if self._base_url:
            parts = urlsplit(self._base_url)
            host = parts.netloc or parts.path
            scheme = parts.scheme or "https"
            return host, scheme
        return f"bedrock-runtime.{self._region}.amazonaws.com", "https"

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
        """Stream a chat completion via ``InvokeModelWithResponseStream``.

        Raises :class:`RuntimeError` for missing config (credentials /
        region) and a :class:`CorlinmanError` subtype for any upstream
        failure.
        """
        if not self._region:
            raise RuntimeError(
                "AWS region missing for Bedrock provider: set params.region "
                "(or AWS_REGION / AWS_DEFAULT_REGION)"
            )
        if not _is_anthropic_model(model):
            raise FormatError(
                f"Bedrock adapter currently supports only Anthropic-on-Bedrock "
                f"model ids (got {model!r})",
                provider=self.name,
                model=model,
            )

        credentials = self._credentials()
        host, scheme = self._endpoint()
        # Bedrock model ids contain dots; the path segment must keep them
        # literal — the SigV4 path encoder preserves unreserved chars.
        path = f"/model/{model}/invoke-with-response-stream"

        body = _build_anthropic_body(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra,
        )
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")

        content_headers = {
            "content-type": "application/json",
            "accept": "application/vnd.amazon.eventstream",
        }
        signed = sigv4_headers(
            credentials=credentials,
            method="POST",
            service=_SERVICE,
            region=self._region,
            host=host,
            path=path,
            body=body_bytes,
            extra_headers=content_headers,
        )
        url = f"{scheme}://{host}{path}"

        try:
            async with (
                httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S) as client,
                client.stream(
                    "POST", url, headers=signed, content=body_bytes
                ) as response,
            ):
                if response.status_code >= 400:
                    raw = await response.aread()
                    raise _map_http_status(
                        response.status_code,
                        raw.decode("utf-8", "replace"),
                        provider=self.name,
                        model=model,
                    )
                async for chunk in _translate_event_stream(
                    response, provider=self.name, model=model
                ):
                    yield chunk
        except CorlinmanError:
            raise
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc), provider=self.name, model=model) from exc
        except httpx.HTTPError as exc:
            raise CorlinmanError(
                str(exc), provider=self.name, model=model
            ) from exc

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        raise NotImplementedError(
            "Bedrock embeddings (Titan / Cohere on Bedrock) land separately"
        )

    @classmethod
    def supports(cls, model: str) -> bool:
        # Bedrock model ids are explicit and operator-chosen; resolution
        # is always via an alias, never the legacy prefix fallback.
        return False


# --------------------------------------------------------------------------
# Request-body construction (Anthropic Messages shape on Bedrock)
# --------------------------------------------------------------------------


def _is_anthropic_model(model: str) -> bool:
    """True for ``anthropic.claude-*`` ids, including cross-region prefixes.

    Cross-region inference profiles prepend a geo prefix, e.g.
    ``us.anthropic.claude-3-5-sonnet-20241022-v2:0`` — we accept any id
    whose dot-segments contain ``anthropic``.
    """
    return "anthropic." in model or model.startswith("anthropic.")


def _build_anthropic_body(
    *,
    messages: Sequence[Any],
    tools: Sequence[dict[str, Any]] | None,
    temperature: float | None,
    max_tokens: int | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the Bedrock Anthropic-Messages request body.

    Bedrock carries the model id in the URL, so the body omits ``model``
    and instead pins ``anthropic_version``. ``system`` is a top-level
    field; ``max_tokens`` is mandatory.
    """
    system, chat = _split_system(messages)
    body: dict[str, Any] = {
        "anthropic_version": _BEDROCK_ANTHROPIC_VERSION,
        "messages": chat,
        "max_tokens": max_tokens if max_tokens else 1024,
    }
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = _normalise_tools(tools)
    if extra:
        # Drop a stray ``model`` — the URL path is authoritative.
        body.update({k: v for k, v in extra.items() if k != "model"})
    return body


def _split_system(
    messages: Sequence[Any],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Lift ``role="system"`` turns into a top-level ``system`` string."""
    system_parts: list[str] = []
    chat: list[dict[str, Any]] = []
    for m in messages:
        role = _get(m, "role")
        content = _get(m, "content")
        if role == "system":
            text = content if isinstance(content, str) else str(content or "")
            if text:
                system_parts.append(text)
        else:
            anth_role = "user" if role in ("user", "tool", None) else "assistant"
            chat.append({"role": anth_role, "content": content or ""})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def _normalise_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate OpenAI-shape tool specs to Anthropic ``tools`` entries."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict):
            # Already in Anthropic shape (or unknown) — forward verbatim.
            out.append(tool)
            continue
        entry: dict[str, Any] = {"name": function.get("name", "")}
        if function.get("description"):
            entry["description"] = function["description"]
        params = function.get("parameters")
        entry["input_schema"] = params if params else {"type": "object"}
        out.append(entry)
    return out


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# --------------------------------------------------------------------------
# Event-stream → ProviderChunk translation
# --------------------------------------------------------------------------


async def _translate_event_stream(
    response: httpx.Response,
    *,
    provider: str,
    model: str,
) -> AsyncIterator[ProviderChunk]:
    """Decode the Bedrock event-stream body and yield :class:`ProviderChunk`.

    Each ``chunk`` event carries a base64-wrapped Anthropic SSE event; we
    accumulate per-content-block state exactly as the native Anthropic
    adapter does and emit ``token`` / ``tool_call_*`` / ``done`` chunks.
    """
    decoder = EventStreamDecoder()
    open_tool_ids: dict[int, str] = {}
    finish_reason = "stop"

    try:
        async for raw in response.aiter_bytes():
            for message in decoder.feed(raw):
                event = _decode_bedrock_event(message, provider=provider, model=model)
                if event is None:
                    continue
                etype = event.get("type")

                if etype == "content_block_start":
                    block = event.get("content_block") or {}
                    idx = event.get("index", 0)
                    if block.get("type") == "tool_use":
                        call_id = block.get("id") or ""
                        open_tool_ids[idx] = call_id
                        yield ProviderChunk(
                            kind="tool_call_start",
                            tool_call_id=call_id,
                            tool_name=block.get("name") or "",
                        )
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    idx = event.get("index", 0)
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            yield ProviderChunk(kind="token", text=text)
                    elif dtype == "input_json_delta":
                        call_id = open_tool_ids.get(idx, "")
                        if call_id:
                            yield ProviderChunk(
                                kind="tool_call_delta",
                                tool_call_id=call_id,
                                arguments_delta=delta.get("partial_json") or "",
                            )
                elif etype == "content_block_stop":
                    idx = event.get("index", 0)
                    call_id = open_tool_ids.pop(idx, None)
                    if call_id:
                        yield ProviderChunk(
                            kind="tool_call_end", tool_call_id=call_id
                        )
                elif etype == "message_delta":
                    stop = (event.get("delta") or {}).get("stop_reason")
                    if stop:
                        finish_reason = _map_stop_reason(stop)
                # ``message_start`` / ``message_stop`` / ``ping`` carry no
                # chunk-relevant data — pure accounting.
    except EventStreamError as exc:
        raise FormatError(
            f"Bedrock event-stream decode failed: {exc}",
            provider=provider,
            model=model,
        ) from exc

    yield ProviderChunk(kind="done", finish_reason=finish_reason)


def _decode_bedrock_event(
    message: Any,
    *,
    provider: str,
    model: str,
) -> dict[str, Any] | None:
    """Unwrap one event-stream message into the inner Anthropic SSE event.

    Bedrock's ``chunk`` payload is ``{"bytes": "<base64-of-json>"}``. An
    ``exception`` message-type carries an error JSON payload directly —
    those are mapped to a :class:`CorlinmanError`.
    """
    if message.message_type == "exception" or message.exception_type:
        detail = message.payload.decode("utf-8", "replace")
        exc_name = message.exception_type or "BedrockException"
        raise _map_bedrock_exception(
            exc_name, detail, provider=provider, model=model
        )

    if message.event_type != "chunk":
        return None

    try:
        outer = json.loads(message.payload.decode("utf-8"))
        inner_b64 = outer.get("bytes")
        if not inner_b64:
            return None
        inner = base64.b64decode(inner_b64)
        event: dict[str, Any] = json.loads(inner.decode("utf-8"))
        return event
    except (ValueError, KeyError) as exc:
        raise FormatError(
            f"malformed Bedrock chunk payload: {exc}",
            provider=provider,
            model=model,
        ) from exc


def _map_stop_reason(reason: str | None) -> str:
    """Map an Anthropic ``stop_reason`` to the normalised finish set."""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    return mapping.get(reason or "", "stop")


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------


def _map_http_status(
    status: int,
    body: str,
    *,
    provider: str,
    model: str,
) -> CorlinmanError:
    """Map a non-2xx Bedrock HTTP response to a :class:`CorlinmanError`."""
    ctx: dict[str, Any] = {"provider": provider, "model": model}
    low = body.lower()
    if status in (401,):
        return AuthError(body or "unauthorized", status_code=status, **ctx)
    if status in (403,):
        # Bedrock returns 403 for both bad signatures and un-enabled
        # model access — treat as a permanent auth failure.
        return AuthPermanentError(body or "forbidden", status_code=status, **ctx)
    if status == 404:
        return ModelNotFoundError(body or "not found", status_code=status, **ctx)
    if status == 429 or "throttl" in low:
        return RateLimitError(body or "throttled", status_code=status, **ctx)
    if status in (503, 529):
        return OverloadedError(body or "overloaded", status_code=status, **ctx)
    if status == 400:
        if "context" in low or "too long" in low or "maximum" in low:
            return ContextOverflowError(body, status_code=status, **ctx)
        return FormatError(body or "bad request", status_code=status, **ctx)
    if status == 402 or "quota" in low or "billing" in low:
        return BillingError(body or "billing", status_code=status, **ctx)
    return CorlinmanError(body or f"HTTP {status}", status_code=status, **ctx)


def _map_bedrock_exception(
    exc_name: str,
    detail: str,
    *,
    provider: str,
    model: str,
) -> CorlinmanError:
    """Map an in-band event-stream ``exception`` frame to a typed error."""
    ctx: dict[str, Any] = {"provider": provider, "model": model}
    name = exc_name.lower()
    if "throttl" in name:
        return RateLimitError(detail or exc_name, status_code=429, **ctx)
    if "modelnotready" in name or "serviceunavailable" in name:
        return OverloadedError(detail or exc_name, status_code=503, **ctx)
    if "validation" in name:
        return FormatError(detail or exc_name, status_code=400, **ctx)
    if "accessdenied" in name:
        return AuthPermanentError(detail or exc_name, status_code=403, **ctx)
    if "modelstream" in name or "internalserver" in name:
        return CorlinmanError(detail or exc_name, status_code=500, **ctx)
    return CorlinmanError(detail or exc_name, **ctx)


def _parse_api_key(api_key: str) -> tuple[str | None, str | None, str | None]:
    """Split a ``"<access>:<secret>[:<token>]"`` credential string.

    A single-token value (no colon) is treated as no usable credential —
    Bedrock has no single-string auth, so resolution falls through to the
    ``AWS_*`` env vars.
    """
    parts = api_key.split(":")
    if len(parts) == 2:
        return parts[0], parts[1], None
    if len(parts) >= 3:
        # A session token may itself contain ``:`` — rejoin the tail.
        return parts[0], parts[1], ":".join(parts[2:])
    return None, None, None


__all__ = ["BedrockProvider"]
