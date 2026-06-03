"""Close-code / timeout constants + router-level config model for the
voice surface.

Extracted verbatim from
:mod:`corlinman_server.gateway.routes_voice.mod` as part of a
behaviour-preserving god-file split. This module MUST NOT import the
source ``mod`` module (no cycle): it only depends on the cost layer's
:class:`VoiceConfig` dataclass.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from corlinman_server.gateway.routes_voice.cost import VoiceConfig

# ---------------------------------------------------------------------------
# Close codes
# ---------------------------------------------------------------------------

CLOSE_CODE_NORMAL: int = 1000
"""RFC 6455 normal closure (graceful end)."""

CLOSE_CODE_PROTOCOL_ERROR: int = 1002
"""RFC 6455 protocol error — bad subprotocol, missing ``start`` frame,
or an unrecoverable control-frame parse failure."""

CLOSE_CODE_VOICE_DISABLED: int = 4000
"""Application-level close code: ``[voice] enabled = false`` at the
moment the upgrade completed. Pre-upgrade this is surfaced as an HTTP
503; mid-upgrade only used if a hot-reload flips the flag between
accept and the budget check."""

CLOSE_CODE_AUTH_DENIED: int = 4401
"""Application-level close code: the WebSocket handshake carried no
valid tenant API key. The HTTP :class:`ApiKeyAuthMiddleware` cannot run
for WebSocket scopes, so the ``/v1/voice`` handler enforces the same
:meth:`AdminDb.verify_api_key` check itself and closes with this
``4401`` (the WS analogue of HTTP 401) before any provider session is
opened or any per-tenant budget is touched."""

CLOSE_CODE_PROVIDER_ERROR: int = 4003
"""Application-level close code: the upstream provider failed to start
or terminated with an error mid-session."""

DEFAULT_TICK_INTERVAL_SECONDS: float = 1.0
"""Per-design tick cadence for the budget enforcer. Once per second is
the same as the Rust implementation."""

DEFAULT_START_TIMEOUT_SECONDS: float = 5.0
"""How long to wait for the client's first ``start`` control frame
before treating the session as a protocol error and closing 1002.
Matches the Rust route handler's 5-second timeout."""


# ---------------------------------------------------------------------------
# Router-level config + state
# ---------------------------------------------------------------------------


class VoiceRouterConfig(BaseModel):
    """Pydantic v2 carrier for the live ``[voice]`` config snapshot.

    The route handler reads a snapshot per request so a hot-reload that
    flips ``enabled`` (or any of the budget / sample-rate knobs) takes
    effect on the next connect without rebuilding the router.

    Mirrors :class:`corlinman_server.gateway.routes_voice.cost.VoiceConfig`
    one-for-one but as a Pydantic model so callers wiring this from
    ``config.toml`` get validation for free. :meth:`to_cost_config`
    projects back onto the frozen dataclass the cost / budget layers
    consume.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    enabled: bool = False
    budget_minutes_per_tenant_per_day: int = Field(default=0, ge=0)
    max_session_seconds: int = Field(default=0, ge=0)
    provider_alias: str = ""
    sample_rate_hz_in: int = Field(default=16_000, gt=0)
    sample_rate_hz_out: int = Field(default=24_000, gt=0)
    retain_audio: bool = False
    default_tenant: str = "default"

    def to_cost_config(self) -> VoiceConfig:
        return VoiceConfig(
            enabled=self.enabled,
            budget_minutes_per_tenant_per_day=self.budget_minutes_per_tenant_per_day,
            max_session_seconds=self.max_session_seconds,
            provider_alias=self.provider_alias,
            sample_rate_hz_in=self.sample_rate_hz_in,
            sample_rate_hz_out=self.sample_rate_hz_out,
            retain_audio=self.retain_audio,
        )


ConfigLoader = Callable[[], VoiceRouterConfig]
"""Live ``[voice]`` config snapshot loader. The handler calls this on
every connect — wire a closure that reads the current ``ArcSwap`` /
``RWLock`` / ``contextvar`` shaped snapshot."""


WS_TOKEN_SUBPROTOCOL_PREFIX: str = "corlinman.voice.token."
"""Prefix for the browser-compatible token-carrying subprotocol.

The browser ``WebSocket`` API cannot set request headers, so a browser
client passes its tenant API key as a *second* offered subprotocol —
``new WebSocket(url, ["corlinman.voice.v1", "corlinman.voice.token." +
token])``. The server reads the token off this entry but echoes back
only the canonical :data:`SUBPROTOCOL` (never the token entry), so the
secret rides the ``Sec-WebSocket-Protocol`` request header — which
uvicorn's access log does NOT record — instead of the query string,
which is logged verbatim on every connect. See R5-S1 / the
``?api_key=`` leak this replaces.
"""
