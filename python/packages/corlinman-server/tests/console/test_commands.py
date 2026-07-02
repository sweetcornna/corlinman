"""Slash command registry + dispatch against a stub app."""

from __future__ import annotations

import io
from typing import Any

from corlinman_server.console.brain import BrainSession
from corlinman_server.console.commands import dispatch, registry
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter
from rich.console import Console


class _IdleBrain:
    descriptor = "stub brain"

    def run_turn(self, **_kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("commands must not run turns")

    async def aclose(self) -> None:  # pragma: no cover - unused
        pass


class StubApp:
    """The slice of ConsoleApp the command handlers touch."""

    def __init__(self) -> None:
        self.session = BrainSession(brain=_IdleBrain(), model="big")
        self.renderer = Renderer(Console(file=io.StringIO(), force_terminal=False))
        self.router = ModelRouter(
            default_model="big", small_fast_model="small", auto_route=False
        )
        self.running = True
        self._models = ["alias-a", "alias-b"]

    def known_models(self) -> list[str]:
        return self._models

    async def list_sessions(self) -> list[str] | None:
        return ["console:aaa  (4 msgs)  hello"]

    async def resume_session(self, key: str) -> int | None:
        self.session.reset(session_key=key)
        return 2


async def test_help_lists_every_command() -> None:
    app = StubApp()
    text = await dispatch(app, "/help") or ""
    for cmd in registry():
        assert f"/{cmd.name}" in text


async def test_unknown_command_is_a_hint_not_a_crash() -> None:
    app = StubApp()
    text = await dispatch(app, "/nope") or ""
    assert "unknown command" in text


async def test_bare_slash_shows_help() -> None:
    app = StubApp()
    text = await dispatch(app, "/") or ""
    assert "commands:" in text


async def test_model_switch_updates_session_and_router() -> None:
    app = StubApp()
    await dispatch(app, "/model gpt-x")
    assert app.session.model == "gpt-x"
    assert app.router.default_model == "gpt-x"


async def test_model_show_includes_known_aliases() -> None:
    app = StubApp()
    text = await dispatch(app, "/model") or ""
    assert "big" in text and "alias-a" in text


async def test_new_rotates_session() -> None:
    app = StubApp()
    old = app.session.session_key
    text = await dispatch(app, "/new") or ""
    assert app.session.session_key != old
    assert app.session.session_key in text


async def test_resume_switches_key() -> None:
    app = StubApp()
    text = await dispatch(app, "/resume console:bbb") or ""
    assert app.session.session_key == "console:bbb"
    assert "2 message(s) replayed" in text


async def test_resume_requires_key() -> None:
    app = StubApp()
    text = await dispatch(app, "/resume") or ""
    assert "usage:" in text


async def test_progress_validates_mode() -> None:
    app = StubApp()
    bad = await dispatch(app, "/progress nope") or ""
    assert "usage:" in bad
    await dispatch(app, "/progress verbose")
    assert app.renderer.tool_progress == "verbose"


async def test_verbose_toggles() -> None:
    app = StubApp()
    await dispatch(app, "/verbose")
    assert app.renderer.tool_progress == "verbose"
    await dispatch(app, "/verbose")
    assert app.renderer.tool_progress == "new"


async def test_quit_stops_the_loop() -> None:
    app = StubApp()
    assert await dispatch(app, "/quit") is None
    assert app.running is False


async def test_alias_dispatch() -> None:
    app = StubApp()
    assert await dispatch(app, "/q") is None
    assert app.running is False


async def test_status_mentions_brain_and_model() -> None:
    app = StubApp()
    text = await dispatch(app, "/status") or ""
    assert "stub brain" in text and "big" in text


async def test_init_returns_turn_request_with_codebase_analysis_prompt() -> None:
    """/init resolves to a TurnRequest (run through the brain), not printed text,
    instructing the agent to analyze the repo and write CORLINMAN.md."""
    from corlinman_server.console.commands import TurnRequest

    app = StubApp()
    reply = await dispatch(app, "/init")
    assert isinstance(reply, TurnRequest)
    assert "CORLINMAN.md" in reply.content
    assert "build" in reply.content.lower() and "test" in reply.content.lower()


async def test_cost_command_renders_breakdown() -> None:
    app = StubApp()
    text = await dispatch(app, "/cost") or ""
    assert "session cost" in text
    assert "model:" in text and "tokens:" in text and "cost:" in text


def test_estimate_session_cost_known_vs_unknown() -> None:
    from corlinman_server.console.commands import _estimate_session_cost_usd

    # An unknown model has no pricing → None (surfaced as "unavailable").
    assert _estimate_session_cost_usd("totally-unknown-model-xyz", 1000, 1000) is None
    # A known model + tokens → a positive USD estimate.
    cost = _estimate_session_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost is not None and cost > 0


class _PermBrain(_IdleBrain):
    """Brain stub with the permission surface (embedded full-agent shape)."""

    def __init__(self) -> None:
        self.mode = "default"

    def get_permission_mode(self) -> str:
        return self.mode

    def set_permission_mode(self, mode: str) -> str:
        self.mode = mode
        return mode


def _perm_app() -> StubApp:
    app = StubApp()
    app.session.brain = _PermBrain()
    return app


async def test_permissions_shows_current_mode() -> None:
    app = _perm_app()
    text = await dispatch(app, "/permissions") or ""
    assert "permission mode: default" in text
    assert "acceptEdits" in text and "plan" in text and "bypass" in text


async def test_permissions_sets_mode_case_insensitive() -> None:
    app = _perm_app()
    text = await dispatch(app, "/permissions acceptedits") or ""
    assert "permission mode: acceptEdits" in text
    assert app.session.brain.mode == "acceptEdits"


async def test_permissions_typo_does_not_change_mode() -> None:
    """Safety: a typo must NOT coerce to default — from plan mode that would
    silently re-enable mutations."""
    app = _perm_app()
    await dispatch(app, "/permissions plan")
    text = await dispatch(app, "/permissions palm") or ""
    assert "unknown mode" in text and "unchanged" in text
    assert app.session.brain.mode == "plan"  # still plan


async def test_permissions_bypass_warns() -> None:
    app = _perm_app()
    text = await dispatch(app, "/permissions bypass") or ""
    assert "bypass" in text and "⚠" in text


async def test_plan_toggles_and_exits() -> None:
    app = _perm_app()
    text = await dispatch(app, "/plan") or ""
    assert "permission mode: plan" in text
    text = await dispatch(app, "/plan off") or ""
    assert "permission mode: default" in text


async def test_permissions_unavailable_without_surface() -> None:
    app = StubApp()  # _IdleBrain: no permission methods (attach-mode shape)
    text = await dispatch(app, "/permissions") or ""
    assert "unavailable" in text


class _FuzzyApp(StubApp):
    """StubApp with the fuzzy session matcher (Dim 11)."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__()
        self._keys = keys
        self.resumed: list[str] = []

    async def match_session_keys(self, fragment: str) -> list[str]:
        if fragment in self._keys:
            return [fragment]
        return [k for k in self._keys if fragment in k]

    async def resume_session(self, key: str) -> int | None:
        self.resumed.append(key)
        self.session.reset(session_key=key)
        return 1


async def test_resume_fuzzy_unique_substring_resolves() -> None:
    app = _FuzzyApp(["console:abc123", "tg:1:99"])
    text = await dispatch(app, "/resume abc") or ""
    assert app.resumed == ["console:abc123"]
    assert "console:abc123" in text


async def test_resume_fuzzy_ambiguous_lists_matches_without_resuming() -> None:
    app = _FuzzyApp(["console:abc1", "console:abc2"])
    text = await dispatch(app, "/resume abc") or ""
    assert app.resumed == []  # did NOT guess
    assert "ambiguous" in text and "console:abc1" in text and "console:abc2" in text


async def test_resume_exact_key_wins_over_substring() -> None:
    app = _FuzzyApp(["console:abc", "console:abcd"])
    await dispatch(app, "/resume console:abc")
    assert app.resumed == ["console:abc"]  # exact hit, not ambiguous


async def test_resume_no_match_falls_through_to_named_session() -> None:
    """Zero matches keep today's semantics: /resume can start a fresh named
    session under the given key."""
    app = _FuzzyApp(["console:xyz"])
    await dispatch(app, "/resume brand-new-key")
    assert app.resumed == ["brand-new-key"]
