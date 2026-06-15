from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.gateway.oauth import codex_external
from corlinman_server.gateway.routes_admin_b._oauth_lib import _codex_status_row
from corlinman_server.gateway.routes_admin_b.state import AdminState


def test_codex_status_falls_back_to_cli_auth_when_app_auth_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path | None] = []

    def fake_read_codex_status(path: Path | None = None) -> codex_external.CodexStatus | None:
        calls.append(path)
        if path is not None:
            return None
        return codex_external.CodexStatus(
            detected=True,
            account_id="operator@example.com",
            expires_at_ms=1_900_000_000_000,
        )

    monkeypatch.setattr(codex_external, "read_codex_status", fake_read_codex_status)

    row = _codex_status_row(AdminState(data_dir=tmp_path))

    assert calls == [tmp_path / ".codex" / "auth.json", None]
    assert row.id == "codex"
    assert row.source == "external-cli"
    assert row.username == "operator@example.com"


def test_codex_status_does_not_hide_present_but_invalid_app_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path | None] = []

    def fake_read_codex_status(path: Path | None = None) -> codex_external.CodexStatus | None:
        calls.append(path)
        if path is not None:
            return codex_external.CodexStatus(detected=False)
        return codex_external.CodexStatus(
            detected=True,
            account_id="operator@example.com",
        )

    monkeypatch.setattr(codex_external, "read_codex_status", fake_read_codex_status)

    row = _codex_status_row(AdminState(data_dir=tmp_path))

    assert calls == [tmp_path / ".codex" / "auth.json"]
    assert row.id == "codex"
    assert row.source == "none"
