"""Tests for ``corlinman_channels.persona_inject`` — the W7 shared
persona system_prompt + emoji-block injector.

The end-to-end flow (humanlike toggle → injected system message → chat
backend sees it) is exercised against every humanlike-capable channel in
:mod:`tests.test_service`; this module pins the pure composer in
isolation so a regression in the block shape surfaces immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels.persona_inject import (
    compose_persona_emoji_block,
    inject_persona_if_enabled,
)


@dataclass(slots=True)
class _FakeAssetRecord:
    """Stand-in for ``corlinman_server.persona.AssetRecord`` — only the
    attributes the composer reads."""

    label: str
    path: str


class _FakeAssetStore:
    """In-memory asset-store double — captures the calls + lets each test
    seed an explicit asset list. ``path_for`` returns the canned path
    string verbatim so assertions can match exact lines."""

    def __init__(
        self,
        records: list[_FakeAssetRecord] | None = None,
        *,
        raise_on_list: bool = False,
    ) -> None:
        self._records = list(records or [])
        self._raise_on_list = raise_on_list
        self.list_calls: list[tuple[str, str | None]] = []

    async def list(
        self, persona_id: str, *, kind: str | None = None
    ) -> list[_FakeAssetRecord]:
        self.list_calls.append((persona_id, kind))
        if self._raise_on_list:
            raise RuntimeError("simulated asset-store failure")
        return list(self._records)

    def path_for(self, record: _FakeAssetRecord) -> str:
        return record.path


class _FakePersonaStore:
    """Persona-store double that returns canned rows by id."""

    def __init__(self, rows: dict[str, Any]) -> None:
        self._rows = rows
        self.get_calls: list[str] = []

    async def get(self, persona_id: str) -> Any:
        self.get_calls.append(persona_id)
        return self._rows.get(persona_id)


def _make_persona(system_prompt: str = "be friendly") -> SimpleNamespace:
    return SimpleNamespace(
        id="grantley",
        display_name="Grantley",
        short_summary="",
        system_prompt=system_prompt,
        is_builtin=False,
    )


def _make_request(user_text: str = "hi") -> SimpleNamespace:
    msg = SimpleNamespace(role="user", content=user_text)
    return SimpleNamespace(messages=[msg])


# ---------------------------------------------------------------------------
# compose_persona_emoji_block
# ---------------------------------------------------------------------------


class TestComposePersonaEmojiBlock:
    @pytest.mark.asyncio
    async def test_returns_none_when_asset_store_missing(self) -> None:
        block = await compose_persona_emoji_block("grantley", None)
        assert block is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_emoji_assets(self) -> None:
        store = _FakeAssetStore(records=[])
        block = await compose_persona_emoji_block("grantley", store)
        assert block is None
        # Verify we asked for the emoji bucket specifically.
        assert store.list_calls == [("grantley", "emoji")]

    @pytest.mark.asyncio
    async def test_block_contains_header_intro_and_lines(self) -> None:
        store = _FakeAssetStore(
            records=[
                _FakeAssetRecord(
                    label="happy", path="/abs/path/to/aaa.png"
                ),
                _FakeAssetRecord(
                    label="angry", path="/abs/path/to/bbb.png"
                ),
                _FakeAssetRecord(
                    label="sad", path="/abs/path/to/ccc.png"
                ),
            ],
        )
        block = await compose_persona_emoji_block("grantley", store)
        assert block is not None
        # Header + per-asset lines.
        assert block.startswith("## Available emoji\n")
        assert "send_attachment" in block
        # Each emoji label + absolute path appears on its own line.
        assert "- happy: /abs/path/to/aaa.png" in block
        assert "- angry: /abs/path/to/bbb.png" in block
        assert "- sad: /abs/path/to/ccc.png" in block
        # Spec line example: ``- happy: /abs/path``.
        for line in (
            "- happy: /abs/path/to/aaa.png",
            "- angry: /abs/path/to/bbb.png",
            "- sad: /abs/path/to/ccc.png",
        ):
            assert any(candidate == line for candidate in block.split("\n"))

    @pytest.mark.asyncio
    async def test_returns_none_on_store_failure(self) -> None:
        store = _FakeAssetStore(raise_on_list=True)
        block = await compose_persona_emoji_block("grantley", store)
        # Best-effort: a broken asset store must not silence the chat
        # path — return None so the persona body is still injected.
        assert block is None


# ---------------------------------------------------------------------------
# inject_persona_if_enabled — no-op gates
# ---------------------------------------------------------------------------


class TestInjectorGates:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self) -> None:
        req = _make_request()
        await inject_persona_if_enabled(
            req,
            humanlike_enabled=False,
            persona_id="grantley",
            persona_store=_FakePersonaStore(
                {"grantley": _make_persona()}
            ),
        )
        assert [m.role for m in req.messages] == ["user"]

    @pytest.mark.asyncio
    async def test_noop_when_persona_id_missing(self) -> None:
        req = _make_request()
        await inject_persona_if_enabled(
            req,
            humanlike_enabled=True,
            persona_id=None,
            persona_store=_FakePersonaStore(
                {"grantley": _make_persona()}
            ),
        )
        assert [m.role for m in req.messages] == ["user"]

    @pytest.mark.asyncio
    async def test_noop_when_persona_store_missing(self) -> None:
        req = _make_request()
        await inject_persona_if_enabled(
            req,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=None,
        )
        assert [m.role for m in req.messages] == ["user"]

    @pytest.mark.asyncio
    async def test_noop_when_persona_row_missing(self) -> None:
        req = _make_request()
        await inject_persona_if_enabled(
            req,
            humanlike_enabled=True,
            persona_id="ghost",
            persona_store=_FakePersonaStore({}),
        )
        assert [m.role for m in req.messages] == ["user"]


# ---------------------------------------------------------------------------
# inject_persona_if_enabled — injection happy path
# ---------------------------------------------------------------------------


class TestInjectorActive:
    @pytest.mark.asyncio
    async def test_persona_body_prepended_without_emoji(self) -> None:
        req = _make_request()
        await inject_persona_if_enabled(
            req,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=_FakePersonaStore(
                {"grantley": _make_persona("PERSONA-BODY-MARK")}
            ),
        )
        assert len(req.messages) == 2
        assert req.messages[0].role == "system"
        assert "PERSONA-BODY-MARK" in req.messages[0].content
        # No emoji block when no asset store.
        assert "## Available emoji" not in req.messages[0].content

    @pytest.mark.asyncio
    async def test_persona_body_and_emoji_block_present(self) -> None:
        req = _make_request()
        store = _FakeAssetStore(
            records=[
                _FakeAssetRecord(label="happy", path="/x/happy.png"),
            ],
        )
        await inject_persona_if_enabled(
            req,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=_FakePersonaStore(
                {"grantley": _make_persona("PERSONA-BODY-MARK")}
            ),
            asset_store=store,
        )
        sys_content = req.messages[0].content
        assert "PERSONA-BODY-MARK" in sys_content
        assert "## Available emoji" in sys_content
        assert "- happy: /x/happy.png" in sys_content

    @pytest.mark.asyncio
    async def test_resolver_wins_over_static_fields(self) -> None:
        req = _make_request()
        store = _FakePersonaStore({"kitty": _make_persona("MEOW")})
        await inject_persona_if_enabled(
            req,
            humanlike_enabled=False,  # static says off
            persona_id=None,
            persona_store=store,
            humanlike_resolver=lambda: (True, "kitty"),  # live says on
        )
        assert req.messages[0].role == "system"
        assert "MEOW" in req.messages[0].content
