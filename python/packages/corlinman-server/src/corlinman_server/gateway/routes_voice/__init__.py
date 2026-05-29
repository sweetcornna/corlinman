"""``corlinman_server.gateway.routes_voice`` — Python port of
``rust/crates/corlinman-gateway/src/routes/voice/``.

This subpackage mirrors the Rust file layout one-for-one:

* :mod:`.framing` — wire-format primitives (subprotocol, audio frames,
  control-frame JSON). Pure logic, no I/O.
* :mod:`.cost`    — per-tenant daily voice-spend bookkeeping, session
  meter, and budget-check arithmetic.
* :mod:`.budget`  — mid-session :class:`BudgetEnforcer` (composes
  :mod:`.cost`) with delta-only checkpointing into the spend store.
* :mod:`.approval`— :class:`VoiceApprovalBridge` mapping
  ``VoiceEvent::ToolCall`` onto :class:`ApprovalStore` from
  ``corlinman_providers.plugins``.
* :mod:`.persistence` — :class:`VoiceSessionStore` trait (with an
  in-memory impl), audio-path helpers, transcript sink.
* :mod:`.provider`— async provider protocol + a mock provider.
* :mod:`.provider_openai` — the real OpenAI Realtime adapter
  (:class:`OpenAIRealtimeProvider`), selected by :mod:`.mod` whenever an
  OpenAI API key is configured; the mock is the fallback.
* :mod:`.mod`     — FastAPI ``APIRouter`` factory + WebSocket session
  driver (``run_voice_session``). The router is the public-facing
  entry point; everything else is wiring.

Hard-rules adhered to (per port spec):

* No edits outside this subpackage.
* :mod:`fastapi` is imported lazily inside :mod:`.mod` so the pure
  framing / cost / budget / approval modules are usable in
  environments without FastAPI installed.
"""

from __future__ import annotations

from corlinman_server.gateway.routes_voice.approval import (
    APPROVAL_DENIED_TEXT,
    APPROVAL_RESUME_TEXT,
    APPROVAL_TIMEOUT_TEXT,
    VOICE_TOOL_PLUGIN,
    ApprovalOutcome,
    VoiceApprovalBridge,
)
from corlinman_server.gateway.routes_voice.budget import (
    BudgetEnforcer,
    BudgetTickAction,
    terminate_reason_to_code,
    terminate_reason_to_end_reason,
    terminate_reason_to_message,
)
from corlinman_server.gateway.routes_voice.cost import (
    CLOSE_CODE_BUDGET,
    CLOSE_CODE_MAX_SESSION,
    BudgetDecision,
    BudgetDenyReason,
    DaySpend,
    InMemoryVoiceSpend,
    MeterTick,
    SessionMeter,
    TerminateReason,
    VoiceConfig,
    VoiceSpend,
    evaluate_budget,
    next_utc_midnight,
    utc_day_epoch,
)
from corlinman_server.gateway.routes_voice.framing import (
    MAX_AUDIO_FRAME_BYTES,
    MIN_AUDIO_FRAME_BYTES,
    SUBPROTOCOL,
    SUBPROTOCOLS,
    AudioFrame,
    AudioFrameError,
    ClientControl,
    ControlParseError,
    ServerControl,
    SubprotocolDecision,
    accept_subprotocol,
    encode_server_control,
    parse_audio_frame,
    parse_client_control,
)
from corlinman_server.gateway.routes_voice.mod import (
    CLOSE_CODE_NORMAL,
    CLOSE_CODE_PROTOCOL_ERROR,
    CLOSE_CODE_PROVIDER_ERROR,
    CLOSE_CODE_VOICE_DISABLED,
    DEFAULT_START_TIMEOUT_SECONDS,
    DEFAULT_TICK_INTERVAL_SECONDS,
    VoiceRouterConfig,
    VoiceState,
    build_voice_state_from_app,
    resolve_voice_provider,
    router,
    run_voice_session,
)
from corlinman_server.gateway.routes_voice.persistence import (
    MemoryTranscriptSink,
    MemoryVoiceSessionStore,
    TranscriptedTurn,
    VoiceEndReason,
    VoiceSessionEnd,
    VoiceSessionRow,
    VoiceSessionStart,
    VoiceSessionStore,
    VoiceStoreError,
    VoiceTranscriptSink,
    audio_path_for,
    tts_audio_path_for,
)
from corlinman_server.gateway.routes_voice.provider import (
    DEFAULT_PROVIDER_CHANNEL_CAPACITY,
    MockVoiceProvider,
    ProviderCommand,
    ProviderEndReason,
    VoiceEvent,
    VoiceProvider,
    VoiceProviderSession,
    VoiceSessionStartParams,
)
from corlinman_server.gateway.routes_voice.provider_openai import (
    DEFAULT_REALTIME_MODEL,
    OPENAI_REALTIME_URL,
    OpenAIRealtimeProvider,
    OpenAIRealtimeSession,
)

__all__ = [
    # approval
    "APPROVAL_DENIED_TEXT",
    "APPROVAL_RESUME_TEXT",
    "APPROVAL_TIMEOUT_TEXT",
    # cost
    "CLOSE_CODE_BUDGET",
    "CLOSE_CODE_MAX_SESSION",
    # mod (FastAPI router + session driver)
    "CLOSE_CODE_NORMAL",
    "CLOSE_CODE_PROTOCOL_ERROR",
    "CLOSE_CODE_PROVIDER_ERROR",
    "CLOSE_CODE_VOICE_DISABLED",
    # provider
    "DEFAULT_PROVIDER_CHANNEL_CAPACITY",
    # provider_openai (real OpenAI Realtime adapter)
    "DEFAULT_REALTIME_MODEL",
    "DEFAULT_START_TIMEOUT_SECONDS",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    # framing
    "MAX_AUDIO_FRAME_BYTES",
    "MIN_AUDIO_FRAME_BYTES",
    "OPENAI_REALTIME_URL",
    "SUBPROTOCOL",
    "SUBPROTOCOLS",
    "VOICE_TOOL_PLUGIN",
    "ApprovalOutcome",
    "AudioFrame",
    "AudioFrameError",
    "BudgetDecision",
    "BudgetDenyReason",
    # budget
    "BudgetEnforcer",
    "BudgetTickAction",
    "ClientControl",
    "ControlParseError",
    "DaySpend",
    "InMemoryVoiceSpend",
    # persistence
    "MemoryTranscriptSink",
    "MemoryVoiceSessionStore",
    "MeterTick",
    "MockVoiceProvider",
    "OpenAIRealtimeProvider",
    "OpenAIRealtimeSession",
    "ProviderCommand",
    "ProviderEndReason",
    "ServerControl",
    "SessionMeter",
    "SubprotocolDecision",
    "TerminateReason",
    "TranscriptedTurn",
    "VoiceApprovalBridge",
    "VoiceConfig",
    "VoiceEndReason",
    "VoiceEvent",
    "VoiceProvider",
    "VoiceProviderSession",
    "VoiceRouterConfig",
    "VoiceSessionEnd",
    "VoiceSessionRow",
    "VoiceSessionStart",
    "VoiceSessionStartParams",
    "VoiceSessionStore",
    "VoiceSpend",
    "VoiceState",
    "VoiceStoreError",
    "VoiceTranscriptSink",
    "accept_subprotocol",
    "audio_path_for",
    "build_voice_state_from_app",
    "encode_server_control",
    "evaluate_budget",
    "next_utc_midnight",
    "parse_audio_frame",
    "parse_client_control",
    "resolve_voice_provider",
    "router",
    "run_voice_session",
    "terminate_reason_to_code",
    "terminate_reason_to_end_reason",
    "terminate_reason_to_message",
    "tts_audio_path_for",
    "utc_day_epoch",
]
