"""Tests for the standalone server's observability-bridge wiring (half A).

Production runs the gateway and the agent server as TWO processes; the
gateway-lifespan emitter wiring never reaches the servicer constructed
in :func:`corlinman_server.main._serve`. The bridge fix builds a
:class:`JournalBackedEmitter` over the SAME shared agent journal the
servicer's lazy ``_get_journal`` resolves and passes it as
``event_emitter=`` — best-effort, ``None`` on any failure so boot never
crashes.

Covers:

* ``_build_event_emitter`` opens ``<data_dir>/agent_journal.sqlite``
  (the servicer-side resolution) and returns a live emitter + journal;
* a journal-open failure degrades to ``(None, None)`` with a warning;
* ``_serve`` constructs the servicer with a non-None ``event_emitter``
  when the journal opens (grpc server / shutdown / auto-resume stubbed
  out, same monkeypatch style as the other ``test_main_*`` suites).
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.observability import JournalBackedEmitter

server_main = import_module("corlinman_server.main")


# ---------------------------------------------------------------------------
# _build_event_emitter
# ---------------------------------------------------------------------------


async def test_build_event_emitter_opens_shared_journal(tmp_path: Path) -> None:
    """The emitter wraps a journal opened at the SAME path the servicer's
    lazy ``_get_journal`` resolves (``<data_dir>/agent_journal.sqlite``),
    so both processes share one DB file. (``CORLINMAN_DATA_DIR`` is
    pinned to ``tmp_path`` by the autouse conftest fixture.)"""
    emitter, journal = await server_main._build_event_emitter()
    try:
        assert isinstance(emitter, JournalBackedEmitter)
        assert journal is not None
        assert (tmp_path / "agent_journal.sqlite").exists()
        # Same resolution as agent_servicer._get_journal.
        assert journal._path == tmp_path / "agent_journal.sqlite"
    finally:
        if journal is not None:
            await journal.close()


async def test_build_event_emitter_degrades_to_none_on_open_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A journal-open failure must NOT crash boot — the helper logs and
    returns ``(None, None)`` so the servicer runs emitter-less."""

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("journal exploded")

    monkeypatch.setattr(server_main.AgentJournal, "open_from_env", _boom)
    emitter, journal = await server_main._build_event_emitter()
    assert emitter is None
    assert journal is None


# ---------------------------------------------------------------------------
# _serve wiring
# ---------------------------------------------------------------------------


async def test_serve_passes_event_emitter_to_servicer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_serve must construct the Agent servicer with a non-None
    ``event_emitter`` when the journal opens. Everything heavyweight
    (grpc server, signal wait, boot auto-resume, telemetry) is stubbed;
    the journal + emitter construction is REAL so this pins the wiring,
    not a mock of it."""
    captured: dict[str, Any] = {}

    class _StubServicer:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            pass

    class _StubGrpcServer:
        def add_insecure_port(self, _bind: str) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self, grace: float) -> None:  # noqa: ARG002
            pass

    class _StubShutdown:
        def request(self, _name: str) -> None:
            pass

        async def wait(self) -> str:
            return "SIGTERM"  # exit immediately

    async def _noop_resume() -> None:
        pass

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    # Mock-provider branch: no provider resolver, simplest construction.
    monkeypatch.setenv("CORLINMAN_TEST_MOCK_PROVIDER", "1")
    monkeypatch.delenv("CORLINMAN_PY_CONFIG", raising=False)
    monkeypatch.setattr(server_main, "CorlinmanAgentServicer", _StubServicer)
    monkeypatch.setattr(server_main, "GracefulShutdown", _StubShutdown)
    monkeypatch.setattr(
        server_main.grpc.aio, "server", lambda *a, **k: _StubGrpcServer()
    )
    monkeypatch.setattr(
        server_main.agent_pb2_grpc,
        "add_AgentServicer_to_server",
        lambda _servicer, _server: None,
    )
    monkeypatch.setattr(server_main, "_run_boot_auto_resume", _noop_resume)
    monkeypatch.setattr(server_main, "init_telemetry", lambda: None)
    monkeypatch.setattr(server_main, "shutdown_telemetry", lambda: None)

    code = await server_main._serve()

    assert code == 143  # the stubbed SIGTERM path
    assert isinstance(captured.get("event_emitter"), JournalBackedEmitter)
    # The shared file landed where the servicer's lazy open resolves it.
    assert (tmp_path / "agent_journal.sqlite").exists()
