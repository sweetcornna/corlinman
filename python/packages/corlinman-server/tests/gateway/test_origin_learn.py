"""Tests for zero-config public-origin learning (gateway.origin_learn).

Covers the header→origin derivation (incl. reverse-proxy X-Forwarded-*),
the loopback/placeholder guard, atomic persist-on-change, and the ASGI
middleware's learn + on_learn callback + explicit-config stand-down.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.gateway.origin_learn import (
    OriginLearningMiddleware,
    load_remembered_origin,
    origin_from_headers,
    remember_origin,
    remembered_origin_path,
)


class TestOriginFromHeaders:
    def test_plain_host_http(self) -> None:
        assert origin_from_headers({"host": "bot.example.com"}) == (
            "http://bot.example.com"
        )

    def test_forwarded_proto_and_host_when_trusted(self) -> None:
        out = origin_from_headers(
            {
                "x-forwarded-proto": "https",
                "x-forwarded-host": "bot.example.com",
                "host": "127.0.0.1:6005",
            },
            use_forwarded=True,
        )
        assert out == "https://bot.example.com"

    def test_forwarded_host_ignored_when_not_trusted(self) -> None:
        out = origin_from_headers(
            {
                "x-forwarded-proto": "https",
                "x-forwarded-host": "attacker.example",
                "host": "bot.example.com",
            },
            allowed_public_origins=["http://bot.example.com"],
        )
        assert out == "http://bot.example.com"

    def test_forwarded_chain_takes_leftmost(self) -> None:
        out = origin_from_headers(
            {"x-forwarded-proto": "https, http", "host": "bot.example.com"},
            use_forwarded=True,
        )
        assert out == "https://bot.example.com"

    def test_strips_default_https_port(self) -> None:
        out = origin_from_headers(
            {"x-forwarded-proto": "https", "host": "bot.example.com:443"},
            use_forwarded=True,
        )
        assert out == "https://bot.example.com"

    def test_keeps_nonstandard_port(self) -> None:
        out = origin_from_headers({"host": "bot.example.com:8443"})
        assert out == "http://bot.example.com:8443"

    @pytest.mark.parametrize(
        "host",
        ["localhost", "127.0.0.1", "0.0.0.0", "testserver", "127.0.0.1:6005"],
    )
    def test_loopback_and_placeholder_hosts_ignored(self, host: str) -> None:
        assert origin_from_headers({"host": host}) is None

    def test_no_host_returns_none(self) -> None:
        assert origin_from_headers({}) is None

    def test_bogus_scheme_falls_back_to_http(self) -> None:
        out = origin_from_headers(
            {"x-forwarded-proto": "gopher", "host": "bot.example.com"},
            use_forwarded=True,
        )
        assert out == "http://bot.example.com"

    def test_allow_list_rejects_unknown_host(self) -> None:
        assert (
            origin_from_headers(
                {"host": "attacker.example"},
                allowed_public_origins=["https://bot.example.com"],
            )
            is None
        )

    def test_allow_list_accepts_configured_origin(self) -> None:
        assert (
            origin_from_headers(
                {"host": "bot.example.com"},
                "https",
                allowed_public_origins=["https://bot.example.com"],
            )
            == "https://bot.example.com"
        )


class TestPersistence:
    def test_remember_and_load_roundtrip(self, tmp_path: Path) -> None:
        assert remember_origin(tmp_path, "https://bot.example.com") is True
        assert load_remembered_origin(tmp_path) == "https://bot.example.com"

    def test_remember_is_noop_when_unchanged(self, tmp_path: Path) -> None:
        assert remember_origin(tmp_path, "https://bot.example.com") is True
        # Second identical write reports no change (debounce contract).
        assert remember_origin(tmp_path, "https://bot.example.com") is False

    def test_remember_overwrites_on_change(self, tmp_path: Path) -> None:
        remember_origin(tmp_path, "https://old.example.com")
        assert remember_origin(tmp_path, "https://new.example.com") is True
        assert load_remembered_origin(tmp_path) == "https://new.example.com"

    def test_empty_origin_not_persisted(self, tmp_path: Path) -> None:
        assert remember_origin(tmp_path, "") is False
        assert load_remembered_origin(tmp_path) == ""

    def test_none_data_dir_is_safe(self) -> None:
        assert remembered_origin_path(None) is None
        assert load_remembered_origin(None) == ""
        assert remember_origin(None, "https://x") is False

    def test_allowed_list_rejects_attacker_origin(self, tmp_path: Path) -> None:
        assert (
            remember_origin(
                tmp_path,
                "https://attacker.example",
                allowed_public_origins=["https://bot.example.com"],
            )
            is False
        )
        assert load_remembered_origin(tmp_path) == ""

    def test_allowed_list_accepts_configured_origin(self, tmp_path: Path) -> None:
        assert (
            remember_origin(
                tmp_path,
                "https://bot.example.com",
                allowed_public_origins=["https://bot.example.com"],
            )
            is True
        )
        assert load_remembered_origin(tmp_path) == "https://bot.example.com"


def _http_scope(
    headers: dict[str, str], client: tuple[str, int] = ("203.0.113.10", 12345)
) -> dict:
    return {
        "type": "http",
        "scheme": "http",
        "client": client,
        "headers": [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in headers.items()
        ],
    }


async def _noop_app(scope, receive, send) -> None:  # noqa: ANN001
    return None


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_learns_and_fires_callback(self, tmp_path: Path) -> None:
        learned: list[str] = []
        mw = OriginLearningMiddleware(
            _noop_app,
            data_dir=tmp_path,
            explicitly_configured=False,
            on_learn=learned.append,
            allowed_public_origins=["https://bot.example.com"],
        )
        await mw(
            _http_scope({"host": "bot.example.com"}) | {"scheme": "https"},
            None,
            None,
        )
        assert load_remembered_origin(tmp_path) == "https://bot.example.com"
        assert learned == ["https://bot.example.com"]

    @pytest.mark.asyncio
    async def test_stands_down_when_explicit(self, tmp_path: Path) -> None:
        learned: list[str] = []
        mw = OriginLearningMiddleware(
            _noop_app,
            data_dir=tmp_path,
            explicitly_configured=True,
            on_learn=learned.append,
            allowed_public_origins=["https://bot.example.com"],
        )
        await mw(
            _http_scope({"host": "bot.example.com"}),
            None,
            None,
        )
        # Explicit config wins: nothing learned, nothing persisted.
        assert load_remembered_origin(tmp_path) == ""
        assert learned == []

    @pytest.mark.asyncio
    async def test_loopback_request_not_learned(self, tmp_path: Path) -> None:
        learned: list[str] = []
        mw = OriginLearningMiddleware(
            _noop_app,
            data_dir=tmp_path,
            explicitly_configured=False,
            on_learn=learned.append,
            allowed_public_origins=["https://bot.example.com"],
        )
        await mw(_http_scope({"host": "127.0.0.1:6005"}), None, None)
        assert load_remembered_origin(tmp_path) == ""
        assert learned == []

    @pytest.mark.asyncio
    async def test_untrusted_forwarded_attacker_not_learned(
        self, tmp_path: Path
    ) -> None:
        learned: list[str] = []
        mw = OriginLearningMiddleware(
            _noop_app,
            data_dir=tmp_path,
            explicitly_configured=False,
            on_learn=learned.append,
            allowed_public_origins=["https://bot.example.com"],
            trusted_proxies=["127.0.0.1"],
        )
        await mw(
            _http_scope(
                {
                    "host": "127.0.0.1:6005",
                    "x-forwarded-proto": "https",
                    "x-forwarded-host": "attacker.example",
                },
                client=("203.0.113.10", 12345),
            ),
            None,
            None,
        )
        assert load_remembered_origin(tmp_path) == ""
        assert learned == []

    @pytest.mark.asyncio
    async def test_trusted_proxy_learns_allowed_public_origin(
        self, tmp_path: Path
    ) -> None:
        learned: list[str] = []
        mw = OriginLearningMiddleware(
            _noop_app,
            data_dir=tmp_path,
            explicitly_configured=False,
            on_learn=learned.append,
            allowed_public_origins=["https://bot.example.com"],
            trusted_proxies=["127.0.0.1"],
        )
        await mw(
            _http_scope(
                {
                    "host": "127.0.0.1:6005",
                    "x-forwarded-proto": "https",
                    "x-forwarded-host": "bot.example.com",
                },
                client=("127.0.0.1", 5555),
            ),
            None,
            None,
        )
        assert load_remembered_origin(tmp_path) == "https://bot.example.com"
        assert learned == ["https://bot.example.com"]

    @pytest.mark.asyncio
    async def test_callback_fires_once_per_change(self, tmp_path: Path) -> None:
        learned: list[str] = []
        mw = OriginLearningMiddleware(
            _noop_app,
            data_dir=tmp_path,
            explicitly_configured=False,
            on_learn=learned.append,
            allowed_public_origins=["https://bot.example.com"],
        )
        scope = _http_scope({"host": "bot.example.com"}) | {"scheme": "https"}
        await mw(scope, None, None)
        await mw(scope, None, None)  # identical — debounced
        assert learned == ["https://bot.example.com"]
