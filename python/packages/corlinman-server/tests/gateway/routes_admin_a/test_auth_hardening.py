"""SEC-011 (login timing oracle) + SEC-009 (Secure cookie flag) hardening.

SEC-011
-------
``/admin/login`` (auth.py) and the Basic-auth fallback in the central
``_auth_shim`` both compared the username with ``!=`` and ``or``-ed it
with the argon2 verify. The ``or`` short-circuits the moment the
username is wrong, so the expensive constant-time argon2 path never
runs — total response time then reveals whether the username was
correct (a timing oracle that lets an attacker enumerate the admin
username). The fix: compare the username with
:func:`hmac.compare_digest` and ALWAYS run a password hash verification
(a dummy hash when the username mismatches) so total work is
independent of username correctness.

SEC-009
-------
``_set_cookie_header`` emitted the session cookie without the ``Secure``
flag. The fix adds ``Secure`` *conditionally* — only when the request
arrived over https (``X-Forwarded-Proto: https`` or
``request.url.scheme == 'https'``) — so the documented upstream-TLS
deploy gets ``Secure`` while plain-http local/dev still works.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.routes_admin_a import auth as auth_mod
from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import router as auth_router
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def configured_state(tmp_path: Path) -> Iterator[AdminState]:
    """An ``AdminState`` with a real argon2 hash for ``admin``/``s3cret-pw``
    and ``must_change_password`` already cleared so the gate doesn't get in
    the way of the timing/cookie assertions."""
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=auth_mod.hash_password("s3cret-pw"),
        config_path=tmp_path / "config.toml",
        must_change_password=False,
        session_store=AdminSessionStore(ttl_seconds=3600),
        admin_write_lock=asyncio.Lock(),
    )
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


def _client(state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router())
    return TestClient(app)


# ---------------------------------------------------------------------------
# SEC-011 — login must not short-circuit the password hash on a wrong username
# ---------------------------------------------------------------------------


def test_login_wrong_username_still_runs_password_hash(
    configured_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong username must NOT skip the argon2 verify.

    Pre-fix the ``username != ... or not argon2_verify(...)`` expression
    short-circuits on the wrong username and ``argon2_verify`` is never
    called → timing oracle. Post-fix the verify always runs against a
    dummy hash so the work is constant regardless of username.
    """
    calls = {"n": 0}
    real_verify = auth_mod.argon2_verify

    def _spy(password: str, encoded: str) -> bool:
        calls["n"] += 1
        return real_verify(password, encoded)

    monkeypatch.setattr(auth_mod, "argon2_verify", _spy)

    client = _client(configured_state)
    resp = client.post(
        "/admin/login",
        json={"username": "not-the-admin", "password": "whatever"},
    )
    assert resp.status_code == 401, resp.text
    assert calls["n"] >= 1, (
        "argon2_verify was never invoked on the wrong-username path — "
        "the timing oracle (SEC-011) is still open"
    )


def test_login_uses_compare_digest_for_username(
    configured_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The username comparison must go through :func:`hmac.compare_digest`,
    not a plain ``==``/``!=`` whose runtime depends on the prefix match."""
    import hmac as _hmac

    seen: list[tuple[str, str]] = []
    real_cd = _hmac.compare_digest

    def _spy_cd(a, b):  # type: ignore[no-untyped-def]
        if isinstance(a, str) and isinstance(b, str):
            seen.append((a, b))
        return real_cd(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", _spy_cd)

    client = _client(configured_state)
    resp = client.post(
        "/admin/login",
        json={"username": "not-the-admin", "password": "whatever"},
    )
    assert resp.status_code == 401, resp.text
    assert any(
        "not-the-admin" in pair or "admin" in pair for pair in seen
    ), (
        "hmac.compare_digest was not used to compare the username — "
        "SEC-011 timing oracle on the username comparison"
    )


def test_login_correct_credentials_still_succeed(
    configured_state: AdminState,
) -> None:
    """Regression guard: the hardening must not break the happy path."""
    client = _client(configured_state)
    resp = client.post(
        "/admin/login",
        json={"username": "admin", "password": "s3cret-pw"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_in"] > 0
    assert resp.cookies.get(SESSION_COOKIE_NAME)


def test_login_correct_username_wrong_password_fails(
    configured_state: AdminState,
) -> None:
    """Right username, wrong password → still 401 invalid_credentials."""
    client = _client(configured_state)
    resp = client.post(
        "/admin/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert resp.status_code == 401, resp.text
    detail = resp.json().get("detail", resp.json())
    assert detail.get("error") == "invalid_credentials"


# ---------------------------------------------------------------------------
# SEC-009 — Secure flag is conditional on https
# ---------------------------------------------------------------------------


def test_set_cookie_has_secure_when_forwarded_proto_https(
    configured_state: AdminState,
) -> None:
    """``X-Forwarded-Proto: https`` → Set-Cookie carries ``Secure``."""
    client = _client(configured_state)
    resp = client.post(
        "/admin/login",
        json={"username": "admin", "password": "s3cret-pw"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Secure" in set_cookie, (
        f"Set-Cookie missing Secure under https (SEC-009): {set_cookie!r}"
    )


def test_set_cookie_omits_secure_for_plain_http(
    configured_state: AdminState,
) -> None:
    """Plain http (the documented upstream-TLS deploy) → no ``Secure`` so
    the cookie is still set over the loopback hop."""
    client = _client(configured_state)
    resp = client.post(
        "/admin/login",
        json={"username": "admin", "password": "s3cret-pw"},
    )
    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Secure" not in set_cookie, (
        f"Set-Cookie must omit Secure over plain http: {set_cookie!r}"
    )
    # Sanity: the other hardening attributes survive.
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie


# ---------------------------------------------------------------------------
# Login failure throttling — fixed-window limiter keyed by IP + username
# ---------------------------------------------------------------------------


def _install_fake_login_limiter(
    state: AdminState, *, limit: int = 3, window_seconds: int = 10
) -> dict[str, float]:
    clock = {"now": 1_000.0}
    state.login_failure_store = auth_mod.AdminLoginFailureStore(
        limit=limit,
        window_seconds=window_seconds,
        now=lambda: clock["now"],
    )
    return clock


def test_login_wrong_passwords_trigger_429(
    configured_state: AdminState,
) -> None:
    """Consecutive bad passwords for the same client IP + username hit 429."""
    _install_fake_login_limiter(configured_state)
    client = _client(configured_state)
    headers = {"X-Forwarded-For": "203.0.113.10"}

    for _ in range(2):
        resp = client.post(
            "/admin/login",
            json={"username": "admin", "password": "wrong-password"},
            headers=headers,
        )
        assert resp.status_code == 401, resp.text

    limited = client.post(
        "/admin/login",
        json={"username": "admin", "password": "wrong-password"},
        headers=headers,
    )
    assert limited.status_code == 429, limited.text
    assert limited.headers["Retry-After"] == "10"
    detail = limited.json().get("detail", limited.json())
    assert detail["error"] == "too_many_login_attempts"


def test_login_limiter_allows_retry_after_window(
    configured_state: AdminState,
) -> None:
    """Once the fixed window expires, the same pair can try again."""
    clock = _install_fake_login_limiter(configured_state)
    client = _client(configured_state)
    headers = {"X-Forwarded-For": "203.0.113.20"}

    for _ in range(3):
        resp = client.post(
            "/admin/login",
            json={"username": "admin", "password": "wrong-password"},
            headers=headers,
        )
    assert resp.status_code == 429, resp.text

    clock["now"] += 11
    retry = client.post(
        "/admin/login",
        json={"username": "admin", "password": "wrong-password"},
        headers=headers,
    )
    assert retry.status_code == 401, retry.text
    detail = retry.json().get("detail", retry.json())
    assert detail["error"] == "invalid_credentials"


def test_login_success_resets_failure_count(
    configured_state: AdminState,
) -> None:
    """A successful login clears the IP/username failure counter."""
    _install_fake_login_limiter(configured_state)
    client = _client(configured_state)
    headers = {"X-Forwarded-For": "203.0.113.30"}

    for _ in range(2):
        resp = client.post(
            "/admin/login",
            json={"username": "admin", "password": "wrong-password"},
            headers=headers,
        )
        assert resp.status_code == 401, resp.text

    ok = client.post(
        "/admin/login",
        json={"username": "admin", "password": "s3cret-pw"},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text

    for _ in range(2):
        resp = client.post(
            "/admin/login",
            json={"username": "admin", "password": "wrong-password"},
            headers=headers,
        )
        assert resp.status_code == 401, resp.text

    limited = client.post(
        "/admin/login",
        json={"username": "admin", "password": "wrong-password"},
        headers=headers,
    )
    assert limited.status_code == 429, limited.text
