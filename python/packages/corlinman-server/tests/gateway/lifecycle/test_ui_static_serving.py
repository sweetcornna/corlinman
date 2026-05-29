"""Regression lock for the gateway's nested static-route serving.

Context
-------
The admin UI is a Next.js static export. In prod the gateway serves the
baked ``ui-static`` dir via the ``_NextStaticFiles`` mount in
:mod:`corlinman_server.gateway.lifecycle.entrypoint`. That mount resolves
an extensionless URL such as ``/channels/qq`` by appending ``.html`` and,
when the target is absent, falling through to ``404.html``.

The "Telegram/QQ channel pages cannot be accessed" incident's primary cause
was a stale deployed bundle missing ``channels/qq.html`` — so the resolver
fell through to the 404 shell. The build-side guard
(``ui/scripts/assert-routes-built.mjs``) catches that missing-file half.
This module locks the *serving* half: that ``_NextStaticFiles`` resolves a
NESTED extensionless route ``dir/leaf -> dir/leaf.html`` and serves the
``404.html`` body for a genuinely-absent nested route.

The incident had a SECOND, independent cause: the api-key auth middleware
(installed in ``build_app``; commit ``3baaae5`` / #R2-001) originally gated
the bare ``/channels/`` prefix with ``path.startswith("/channels/")``, which
also matched the UI page routes (``/channels/qq`` …) and returned 401 before
the static mount was ever reached. That prefix has since been narrowed to the
specific Telegram webhook alias (``/channels/telegram/webhook``) so the page
routes are no longer gated while the only real bearer API under ``/channels/``
stays protected. The fixture still exercises the nested resolver through a
non-gated ``/media/...`` route (it must hold regardless of auth), and the
tests below lock BOTH halves of the #R2-001 fix: ``/channels/qq`` now serves
its page, and ``/channels/telegram/webhook`` remains gated.

Mirrors the fixture style of ``test_build_app_serves_next_export_*`` in
``test_entrypoint.py``: point ``CORLINMAN_UI_DIR`` at a tmp export dir and
drive it through the real app via ``TestClient``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from corlinman_server.gateway.lifecycle.entrypoint import (  # noqa: E402
    build_app,
)
from fastapi.testclient import TestClient  # noqa: E402

_NESTED_SENTINEL = "<main data-test='nested-shell'>nested channel-style page</main>"
_NOT_FOUND_SENTINEL = "<main data-test='not-found-shell'>not found page</main>"


def _make_export(root: Path) -> Path:
    """Write a minimal Next.js static export under ``root``.

    Includes a nested route (``media/page.html``) and a distinct
    ``404.html`` shell so the two halves of the contract are
    distinguishable by body. ``media/`` is used because it shares the
    nested-route resolution path with ``channels/`` but is *not* behind the
    api-key gate, isolating the static resolver under test.
    """
    ui_dir = root / "ui-static"
    (ui_dir / "media").mkdir(parents=True)
    # Also lay down the real channel page so a future un-gating of
    # ``/channels/`` (or a token-bearing client) hits a present file rather
    # than an unrelated missing-route failure.
    (ui_dir / "channels").mkdir(parents=True)
    (ui_dir / "media" / "page.html").write_text(_NESTED_SENTINEL, encoding="utf-8")
    (ui_dir / "channels" / "qq.html").write_text(_NESTED_SENTINEL, encoding="utf-8")
    (ui_dir / "404.html").write_text(_NOT_FOUND_SENTINEL, encoding="utf-8")
    return ui_dir


def test_nested_route_resolves_to_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested extensionless route serves ``<dir>/<leaf>.html`` (the
    sentinel body), not the 404 shell.

    This is the exact ``_NextStaticFiles`` resolution that the QQ/Telegram
    channel pages depend on (``channels/qq -> channels/qq.html``), exercised
    through a non-gated prefix so the static mount — not the api-key gate —
    is what answers.
    """
    ui_dir = _make_export(tmp_path)
    monkeypatch.setenv("CORLINMAN_UI_DIR", str(ui_dir))

    app = build_app(config_path=None, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        resp = client.get("/media/page")

    assert resp.status_code == 200, resp.text
    assert _NESTED_SENTINEL in resp.text
    # Guard against a silent fall-through to the 404 shell.
    assert _NOT_FOUND_SENTINEL not in resp.text


def test_missing_nested_route_serves_404_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely-absent nested route falls through to the ``404.html``
    body (the prod incident's symptom — locked here as the expected
    behaviour for routes that truly do not exist)."""
    ui_dir = _make_export(tmp_path)
    monkeypatch.setenv("CORLINMAN_UI_DIR", str(ui_dir))

    app = build_app(config_path=None, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        resp = client.get("/media/does-not-exist")

    assert _NOT_FOUND_SENTINEL in resp.text


def test_channels_page_route_is_not_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/channels/qq`` *page* route is reachable without a bearer token.

    Regression lock for the #R2-001 prefix narrowing: the api-key gate no
    longer swallows the ``/channels/`` UI namespace, so a browser GET of the
    channel admin page resolves through ``_NextStaticFiles`` to
    ``channels/qq.html`` (200 + page body) instead of 401. If someone re-broadens
    the protected prefix back to a bare ``/channels/``, this flips loudly.
    """
    ui_dir = _make_export(tmp_path)
    monkeypatch.setenv("CORLINMAN_UI_DIR", str(ui_dir))

    app = build_app(config_path=None, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        resp = client.get("/channels/qq")

    assert resp.status_code == 200, resp.text
    assert _NESTED_SENTINEL in resp.text
    assert "unauthorized" not in resp.text


def test_telegram_webhook_alias_stays_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only real bearer API under ``/channels/`` — the legacy Telegram
    webhook alias — stays protected after the prefix narrowing (preserves the
    #R2-001 security fix). An unauthenticated request must NOT be served the
    static shell; it is rejected by the api-key gate (401)."""
    ui_dir = _make_export(tmp_path)
    monkeypatch.setenv("CORLINMAN_UI_DIR", str(ui_dir))

    app = build_app(config_path=None, data_dir=tmp_path / "data")

    with TestClient(app) as client:
        resp = client.post("/channels/telegram/webhook", json={})

    assert resp.status_code == 401, resp.text
    assert "unauthorized" in resp.text
