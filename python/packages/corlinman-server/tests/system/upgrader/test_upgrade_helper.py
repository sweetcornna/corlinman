"""Unit tests for ``docker/upgrade_helper.py``.

The helper is stdlib-only and lives outside the package (it is baked into
the image and run in a throwaway container), so we load it by path.

Focus: the Engine-API-version negotiation. A hard-pinned API version
(the helper used to pin ``/v1.41``) is rejected by modern daemons —
Docker 25+/29 sets ``MinAPIVersion 1.44`` and 400s an older client with
"client version X is too old". A real-engine E2E run caught this; these
tests lock the negotiation so it can't regress.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HELPER_PATH = (
    Path(__file__).resolve().parents[6] / "docker" / "upgrade_helper.py"
)


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "corlinman_upgrade_helper", _HELPER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def helper():
    return _load_helper()


def test_helper_file_exists() -> None:
    assert _HELPER_PATH.is_file(), f"helper missing at {_HELPER_PATH}"


def test_api_prefix_defaults_to_unversioned(helper) -> None:
    """Never a hard pin at import time — unversioned is served by the
    daemon's own current version and is accepted by every engine."""
    assert helper.API == ""


def test_negotiate_pins_daemon_api_version(helper, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_engine(method, path, *a, **kw):
        calls.append((method, path))
        return {"ApiVersion": "1.53"}

    monkeypatch.setattr(helper, "_engine", fake_engine)
    helper._negotiate_api_version()

    assert helper.API == "/v1.53"
    assert ("GET", "/version") in calls


def test_negotiate_falls_back_to_unversioned_on_error(
    helper, monkeypatch
) -> None:
    def boom(*a, **kw):
        raise helper.EngineError(500, "daemon exploded")

    monkeypatch.setattr(helper, "_engine", boom)
    helper.API = "/vstale"  # simulate a prior value
    helper._negotiate_api_version()

    # Fell back to unversioned rather than raising or keeping a bad pin.
    assert helper.API == ""


def test_negotiate_ignores_a_missing_version_field(
    helper, monkeypatch
) -> None:
    monkeypatch.setattr(helper, "_engine", lambda *a, **kw: {})
    helper.API = ""
    helper._negotiate_api_version()
    assert helper.API == ""  # unchanged, still safe
