"""W1.1 — tests for ``/admin/providers/{name}/test``, ``/{name}/models``,
and ``/kinds`` after the response-shape upgrade.

These tests sit beside :mod:`test_provider_model_discovery` (which
exercises the legacy ``_query_provider_models`` helper directly with
the flat-string shape). The W1.1 endpoints layer richer descriptors,
caching, hardcoded catalogs, and api-key redaction on top of that
helper — this file covers that new surface end-to-end through the
route handlers.

All network calls are mocked so the suite stays fully offline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from corlinman_server.gateway.routes_admin_b.config_admin import providers
from corlinman_server.gateway.routes_admin_b.config_admin.providers import (
    _MODELS_CACHE_TTL_SECONDS,
    _clear_models_cache,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures (mirror test_provider_model_discovery — kept local to avoid a
# cross-file conftest dance for the same plumbing).
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text("", encoding="utf-8")
    return p


@pytest.fixture()
def state_and_snapshot(
    temp_config_path: Path,
) -> Iterator[tuple[AdminState, dict[str, Any]]]:
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(config_loader=_loader, config_path=temp_config_path)
    configure_admin_auth(state)
    set_admin_state(state)
    # Always start with a clean cache so tests don't leak through the
    # module-level dict on the providers module.
    _clear_models_cache()
    try:
        yield state, snapshot
    finally:
        _clear_models_cache()
        set_admin_state(None)


@pytest.fixture()
def client(
    state_and_snapshot: tuple[AdminState, dict[str, Any]],
) -> TestClient:
    app = FastAPI()
    app.include_router(providers.router())
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_httpx_response(*, status_code: int = 200, json_body: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    return resp


def _mock_async_client(resp: MagicMock | Exception) -> AsyncMock:
    """Build an AsyncMock that acts like ``httpx.AsyncClient`` for tests.

    Pass a response mock to have ``.get`` resolve to it, or pass an
    exception instance to have ``.get`` raise it.
    """
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if isinstance(resp, Exception):
        client.get = AsyncMock(side_effect=resp)
    else:
        client.get = AsyncMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# POST /admin/providers/{name}/test
# ---------------------------------------------------------------------------


class TestTestEndpoint:
    def test_test_endpoint_mock_provider(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Mock kind is the fast-path — instant ok, models_count=1."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "echo": {"kind": "mock", "enabled": True},
            }
        })

        resp = client.post("/admin/providers/echo/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["latency_ms"] == 0
        assert body["models_count"] == 1

    def test_test_endpoint_openai_compatible_success(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """openai_compatible kind proxies /v1/models — 200 → ok=True + count."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "vllm": {
                    "kind": "openai_compatible",
                    "api_key": "sk-test-xxxxxx",
                    "base_url": "https://my.vllm",
                    "enabled": True,
                }
            }
        })

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "qwen-72b"}, {"id": "llama-70b"}]},
        )
        with patch("httpx.AsyncClient", return_value=_mock_async_client(mock_resp)):
            resp = client.post("/admin/providers/vllm/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["models_count"] == 2
        assert "error" not in body or body.get("error") is None

    def test_test_endpoint_openai_compatible_auth_fail(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """A 401 from upstream surfaces as ok=False with a redacted error."""
        _, snapshot = state_and_snapshot
        api_key = "sk-secret-DO-NOT-LEAK"
        snapshot.clear()
        snapshot.update({
            "providers": {
                "broken": {
                    "kind": "openai_compatible",
                    "api_key": api_key,
                    "base_url": "https://upstream",
                    "enabled": True,
                }
            }
        })

        mock_resp = _mock_httpx_response(status_code=401, json_body={"error": "nope"})
        with patch("httpx.AsyncClient", return_value=_mock_async_client(mock_resp)):
            resp = client.post("/admin/providers/broken/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        # Error message exists and does NOT echo the key.
        assert body.get("error")
        assert api_key not in body["error"]
        assert "401" in body["error"]

    def test_test_endpoint_timeout(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """httpx.TimeoutException at the network layer surfaces as ok=False.

        The helper catches every ``Exception`` and folds it into the
        ``error`` string; the W1.1 cap on top is the asyncio.wait_for
        5s wall (not exercised here — we want the underlying socket
        timeout path to be visible too).
        """
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "slow": {
                    "kind": "openai_compatible",
                    "api_key": "sk-test",
                    "base_url": "https://very.slow",
                    "enabled": True,
                }
            }
        })

        timeout_exc = httpx.TimeoutException("connect timed out")
        with patch("httpx.AsyncClient", return_value=_mock_async_client(timeout_exc)):
            resp = client.post("/admin/providers/slow/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        err = (body.get("error") or "").lower()
        assert "timeout" in err or "timed out" in err

    def test_test_endpoint_never_echoes_api_key(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Whatever happens, the api key must not appear in body or logs.

        Simulates a server-side exception whose stringified form embeds
        the key — the redactor must strip it on the way out.
        """
        _, snapshot = state_and_snapshot
        api_key = "sk-VERY-SECRET-KEY-abcdef123456"
        snapshot.clear()
        snapshot.update({
            "providers": {
                "leaky": {
                    "kind": "openai_compatible",
                    "api_key": api_key,
                    "base_url": "https://upstream",
                    "enabled": True,
                }
            }
        })

        # Force the helper's ``except Exception`` branch with a message
        # that quotes the key — the redactor is what saves us here.
        boom = RuntimeError(f"upstream failed; Authorization: Bearer {api_key}")
        caplog.set_level(logging.DEBUG)
        with patch("httpx.AsyncClient", return_value=_mock_async_client(boom)):
            resp = client.post("/admin/providers/leaky/test")

        assert resp.status_code == 200
        body = resp.json()
        assert api_key not in resp.text
        assert api_key not in str(body)
        # Logs captured during the call must also be free of the key.
        for record in caplog.records:
            assert api_key not in record.getMessage()


# ---------------------------------------------------------------------------
# GET /admin/providers/{name}/models
# ---------------------------------------------------------------------------


class TestModelsEndpoint:
    def test_probe_models_from_draft_openai_compatible_provider_without_persisting(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """The add-provider dialog can discover models before saving.

        This uses a draft body rather than a configured provider name and
        must not mutate the TOML-backed provider registry.
        """
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({"providers": {}})

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "relay-model-a"}, {"id": "relay-model-b"}]},
        )
        async_client = _mock_async_client(mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            resp = client.post(
                "/admin/providers/probe-models",
                json={
                    "kind": "openai_compatible",
                    "base_url": "https://relay.example/v1",
                    "api_key": {"value": "sk-test"},
                    "params": {},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert [m["id"] for m in body["models"]] == [
            "relay-model-a",
            "relay-model-b",
        ]
        assert snapshot == {"providers": {}}
        async_client.get.assert_awaited_once()

    def test_probe_models_reuses_existing_literal_key_for_edited_draft(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Edited drafts can probe changed fields without re-pasting a saved key."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "relay": {
                    "kind": "openai_compatible",
                    "api_key": {"value": "sk-saved"},
                    "base_url": "https://saved.example/v1",
                    "enabled": True,
                }
            }
        })

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "relay-model"}]},
        )
        async_client = _mock_async_client(mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            resp = client.post(
                "/admin/providers/probe-models",
                json={
                    "kind": "openai_compatible",
                    "base_url": "https://edited.example/v1",
                    "existing_name": "relay",
                    "params": {},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"][0]["id"] == "relay-model"
        async_client.get.assert_awaited_once()
        assert async_client.get.await_args.kwargs["headers"] == {
            "Authorization": "Bearer sk-saved"
        }

    def test_probe_models_uses_market_default_base_url_for_draft(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Vendor kinds should probe their adapter default, not OpenAI."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({"providers": {}})

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "llama-3.3-70b-versatile"}]},
        )
        async_client = _mock_async_client(mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            resp = client.post(
                "/admin/providers/probe-models",
                json={
                    "kind": "groq",
                    "api_key": {"value": "gsk-test"},
                    "params": {},
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"][0]["id"] == "llama-3.3-70b-versatile"
        async_client.get.assert_awaited_once()
        assert (
            async_client.get.await_args.args[0]
            == "https://api.groq.com/openai/v1/models"
        )

    def test_models_endpoint_openai_proxy(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """openai-shape providers proxy /v1/models and reshape to object items."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "myopenai": {
                    "kind": "openai",
                    "api_key": "sk-test",
                    "base_url": "https://api.openai.com",
                    "enabled": True,
                }
            }
        })

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={
                "data": [
                    {"id": "gpt-4o"},
                    {"id": "gpt-4o-mini"},
                    {"id": "gpt-3.5-turbo"},
                ]
            },
        )
        with patch("httpx.AsyncClient", return_value=_mock_async_client(mock_resp)):
            resp = client.get("/admin/providers/myopenai/models")

        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        ids = {m["id"] for m in body["models"]}
        assert {"gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"} <= ids
        # Each item is an object, not a bare string.
        for item in body["models"]:
            assert isinstance(item, dict)
            assert "id" in item
            assert "display_name" in item

    def test_models_endpoint_cache_30s(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """First call hits upstream; second within 30s does not."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "cached": {
                    "kind": "openai_compatible",
                    "api_key": "sk-test",
                    "base_url": "https://upstream",
                    "enabled": True,
                }
            }
        })

        mock_resp = _mock_httpx_response(
            status_code=200, json_body={"data": [{"id": "m1"}]}
        )
        async_client = _mock_async_client(mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            first = client.get("/admin/providers/cached/models")
            second = client.get("/admin/providers/cached/models")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
        # Upstream is hit exactly once even though the client called twice.
        assert async_client.get.await_count == 1
        # And the TTL constant matches the documented 30s window.
        assert _MODELS_CACHE_TTL_SECONDS == 30.0

    def test_models_endpoint_retries_transient_upstream_failures(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Transient 5xx should be retried and succeed without surfacing error."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "retryme": {
                    "kind": "openai_compatible",
                    "api_key": "sk-test",
                    "base_url": "https://unstable.example",
                    "enabled": True,
                }
            }
        })

        resp_503 = _mock_httpx_response(status_code=503, json_body={"error": "busy"})
        resp_200 = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "gpt-5"}]},
        )
        async_client = _mock_async_client(resp_200)
        async_client.get = AsyncMock(side_effect=[resp_503, resp_200])
        with patch("httpx.AsyncClient", return_value=async_client):
            resp = client.get("/admin/providers/retryme/models")

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"][0]["id"] == "gpt-5"
        assert "error" not in body
        assert async_client.get.await_count == 2

    def test_models_endpoint_falls_back_to_stale_cache_when_upstream_fails(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Expired cache is used as stale fallback when live probe keeps failing."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "cached": {
                    "kind": "openai_compatible",
                    "api_key": "sk-test",
                    "base_url": "https://unstable.example",
                    "enabled": True,
                }
            }
        })

        first_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "gpt-5"}]},
        )
        with patch("httpx.AsyncClient", return_value=_mock_async_client(first_resp)):
            first = client.get("/admin/providers/cached/models")
        assert first.status_code == 200
        assert first.json()["models"][0]["id"] == "gpt-5"

        # Force cache expiry while keeping cached payload for stale fallback.
        expiry, payload = providers._MODELS_CACHE["cached"]  # type: ignore[attr-defined]
        providers._MODELS_CACHE["cached"] = (-1.0, payload)  # type: ignore[attr-defined]
        assert expiry > 0

        timeout_exc = httpx.TimeoutException("connect timed out")
        async_client = _mock_async_client(timeout_exc)
        with patch("httpx.AsyncClient", return_value=async_client):
            second = client.get("/admin/providers/cached/models")

        assert second.status_code == 200
        body = second.json()
        assert body["models"][0]["id"] == "gpt-5"
        assert body.get("stale") is True
        assert isinstance(body.get("warning"), str) and body["warning"]
        assert async_client.get.await_count == 3

    def test_models_endpoint_hardcoded_anthropic(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Anthropic has no zero-cost models endpoint; serve hardcoded catalog."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "myanthropic": {
                    "kind": "anthropic",
                    "api_key": "sk-ant-xxx",
                    "enabled": True,
                }
            }
        })

        resp = client.get("/admin/providers/myanthropic/models")
        assert resp.status_code == 200
        body = resp.json()
        ids = [m["id"] for m in body["models"]]
        assert any(i.startswith("claude-") for i in ids)
        # No upstream was contacted — error key absent.
        assert "error" not in body

    def test_models_endpoint_mock_provider(
        self,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Mock kind always returns ``{models: [{id:'mock', ...}]}``."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {"echo": {"kind": "mock", "enabled": True}}
        })

        resp = client.get("/admin/providers/echo/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["models"][0]["id"] == "mock"

    @pytest.mark.parametrize(
        "kind",
        ["mistral", "cohere", "together", "replicate"],
    )
    def test_models_endpoint_market_openai_shape_kinds_proxy_v1_models(
        self,
        kind: str,
        client: TestClient,
        state_and_snapshot: tuple[AdminState, dict[str, Any]],
    ) -> None:
        """Market OpenAI-shape kinds should use the /v1/models probe path."""
        _, snapshot = state_and_snapshot
        snapshot.clear()
        snapshot.update({
            "providers": {
                "market": {
                    "kind": kind,
                    "api_key": "sk-test",
                    "base_url": "https://market.example",
                    "enabled": True,
                }
            }
        })

        mock_resp = _mock_httpx_response(
            status_code=200,
            json_body={"data": [{"id": "model-1"}]},
        )
        async_client = _mock_async_client(mock_resp)
        with patch("httpx.AsyncClient", return_value=async_client):
            resp = client.get("/admin/providers/market/models")

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"][0]["id"] == "model-1"
        assert async_client.get.await_count == 1


# ---------------------------------------------------------------------------
# GET /admin/providers/kinds
# ---------------------------------------------------------------------------


class TestKindsEndpoint:
    def test_kinds_endpoint_lists_registered(
        self,
        client: TestClient,
    ) -> None:
        """Returns at least 5 kinds including openai, anthropic, mock —
        each with the documented descriptor shape."""
        resp = client.get("/admin/providers/kinds")
        assert resp.status_code == 200
        body = resp.json()
        kinds = body["kinds"]
        assert len(kinds) >= 5
        ids = {k["kind"] for k in kinds}
        assert {"openai", "anthropic", "mock", "openai_compatible"} <= ids

        for item in kinds:
            assert {"kind", "label", "description", "params_schema"} <= item.keys()
            assert isinstance(item["label"], str) and item["label"]
            assert isinstance(item["params_schema"], dict)

    def test_kinds_endpoint_includes_params_schema(
        self,
        client: TestClient,
    ) -> None:
        """Each kind descriptor's ``params_schema`` is a JSON-schema-shaped dict."""
        resp = client.get("/admin/providers/kinds")
        assert resp.status_code == 200
        body = resp.json()
        for item in body["kinds"]:
            schema = item["params_schema"]
            assert isinstance(schema, dict)
            # We never assert on a particular schema shape (per-kind
            # adapter authors own that), but it must be a dict object.
            # ``type`` / ``properties`` are typical keys; we just check
            # the dict is non-None.
            assert schema is not None
