"""SEC-011 hardening for the central admin-auth shim (Basic-auth path).

The Basic-auth fallback in ``_auth_shim.authenticate_admin_request``
compared the username with ``!=`` and ``or``-ed it with
``_verify_password(...)``. On a wrong username the ``or`` short-circuits
and the argon2 verify never runs, so total time reveals whether the
username was correct (timing oracle, SEC-011).

These tests drive ``authenticate_admin_request`` directly with a hand-
built request + a stub state. They patch ``argon2_verify`` (which
``_verify_password`` delegates to) with a counting spy and assert it
fires even when the Basic-auth username is wrong, and that the username
comparison goes through ``hmac.compare_digest``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_a import _auth_shim as shim
from corlinman_server.gateway.routes_admin_a import auth as auth_mod


@dataclass
class _StubState:
    """Minimal duck-typed admin state for the shim."""

    admin_username: str = "admin"
    admin_password_hash: str = field(default_factory=lambda: auth_mod.hash_password("s3cret-pw"))
    session_store: Any | None = None
    must_change_password: bool = False


class _FakeState:
    """Mutable holder for ``request.state.*`` writes the shim performs."""


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    """Just enough of Starlette's ``Request`` for the shim."""

    def __init__(self, *, username: str, password: str, path: str = "/admin/api_keys") -> None:
        raw = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self.headers = {"authorization": f"Basic {raw}"}
        self.cookies: dict[str, str] = {}
        self.url = _FakeURL(path)
        self.state = _FakeState()


# ---------------------------------------------------------------------------
# SEC-011 — wrong-username Basic-auth must still run the password hash
# ---------------------------------------------------------------------------


def test_shim_wrong_username_still_runs_password_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong Basic-auth username must NOT skip the argon2 verify."""
    calls = {"n": 0}
    real_verify = auth_mod.argon2_verify

    def _spy(password: str, encoded: str) -> bool:
        calls["n"] += 1
        return real_verify(password, encoded)

    monkeypatch.setattr(auth_mod, "argon2_verify", _spy)

    req = _FakeRequest(username="not-the-admin", password="whatever")
    with pytest.raises(shim.HTTPException) as exc:
        shim.authenticate_admin_request(req, _StubState())  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    assert calls["n"] >= 1, (
        "argon2_verify never ran on the wrong-username Basic-auth path — "
        "the SEC-011 timing oracle is still open in _auth_shim"
    )


def test_shim_uses_compare_digest_for_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim's username comparison must use ``hmac.compare_digest``."""
    import hmac as _hmac

    seen: list[tuple[str, str]] = []
    real_cd = _hmac.compare_digest

    def _spy_cd(a, b):  # type: ignore[no-untyped-def]
        if isinstance(a, str) and isinstance(b, str):
            seen.append((a, b))
        return real_cd(a, b)

    monkeypatch.setattr(shim.hmac, "compare_digest", _spy_cd)

    req = _FakeRequest(username="not-the-admin", password="whatever")
    with pytest.raises(shim.HTTPException):
        shim.authenticate_admin_request(req, _StubState())  # type: ignore[arg-type]
    assert any(
        "not-the-admin" in pair or "admin" in pair for pair in seen
    ), "hmac.compare_digest was not used for the username comparison (SEC-011)"


def test_shim_correct_basic_auth_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: right credentials still authenticate."""
    req = _FakeRequest(username="admin", password="s3cret-pw")
    principal = shim.authenticate_admin_request(req, _StubState())  # type: ignore[arg-type]
    assert principal == "admin"
    assert req.state.admin_user == "admin"  # type: ignore[attr-defined]


def test_shim_correct_username_wrong_password_fails() -> None:
    """Right username, wrong password → still 401 invalid_credentials."""
    req = _FakeRequest(username="admin", password="wrong-password")
    with pytest.raises(shim.HTTPException) as exc:
        shim.authenticate_admin_request(req, _StubState())  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    assert exc.value.detail["reason"] == "invalid_credentials"  # type: ignore[index]
