"""Tests for the ``qzone_publish`` builtin tool (W5).

Network is mocked via :class:`httpx.MockTransport` — separate transports
for the OneBot HTTP API and the QZone upload + publish endpoints. The
``generate`` path stubs ``dispatch_image_with_refs`` via a fake callable
so this file doesn't need to bring up the persona / image stack.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from corlinman_agent.onebot import OneBotClient, OneBotError
from corlinman_agent.qzone import (
    QZONE_PUBLISH_TOOL,
    dispatch_qzone_publish,
    qzone_publish_tool_schema,
)
from corlinman_agent.qzone.publish import (
    _build_richval,
    _compute_gtk,
    _extract_cookie_value,
    _parse_publish_response,
    _parse_upload_response,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


_QQ_UIN = "10001"
_QZONE_COOKIE = (
    f"uin=o{_QQ_UIN}; skey=@AbCdEf123; "
    "p_skey=PKEY_ABCDEFGHIJK; pt4_token=TOK"
)
# ``_compute_gtk("PKEY_ABCDEFGHIJK")`` — verified once locally, frozen
# here so a regression in the algo trips a clear assertion rather than
# producing nondescript "csrf mismatch" failures.
_EXPECTED_GTK = _compute_gtk("PKEY_ABCDEFGHIJK")

# Canonical mock PNG body — magic bytes plus filler so the upload form
# isn't trivially small.
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"FAKEIMAGEDATA" * 8


def _onebot_transport(
    *,
    fail_login: bool = False,
    fail_cookies: bool = False,
    return_empty_cookies: bool = False,
) -> httpx.MockTransport:
    """Build a OneBot HTTP transport with knobs for each failure mode."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/get_login_info"):
            if fail_login:
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "retcode": 1404,
                        "message": "QQ offline",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "user_id": int(_QQ_UIN),
                        "nickname": "Tester",
                    },
                },
            )
        if path.endswith("/get_cookies"):
            if fail_cookies:
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "retcode": 1500,
                        "message": "no qzone session",
                    },
                )
            cookies = "" if return_empty_cookies else _QZONE_COOKIE
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"cookies": cookies},
                },
            )
        if path.endswith("/get_csrf_token"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"token": _EXPECTED_GTK},
                },
            )
        return httpx.Response(404, text=f"unmocked onebot action: {path}")

    return httpx.MockTransport(handler)


def _qzone_transport(
    *,
    upload_ret: int = 0,
    upload_album: str = "ALB-1",
    upload_photo: str = "PHO-2",
    publish_payload: dict | None = None,
    publish_status: int = 200,
    upload_calls: list[httpx.Request] | None = None,
    publish_calls: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Build a QZone transport that emulates both upload + publish.

    The defaults model the happy path: ``ret=0`` upload + ``code=0`` +
    a ``tid`` on publish. Override individual knobs to exercise the
    failure branches.
    """

    if publish_payload is None:
        publish_payload = {"code": 0, "subcode": 0, "tid": "FEED_TID_42"}

    upload_body = {
        "ret": upload_ret,
        "msg": "ok" if upload_ret == 0 else "upload broke",
        "data": {
            "albumid": upload_album,
            "lloc": upload_photo,
            "sloc": upload_photo,
            "type": 0,
            "width": 64,
            "height": 64,
            "url": "https://qpic.cn/" + upload_photo,
        },
    }
    upload_body_text = (
        "frameElement.callback(" + json.dumps(upload_body) + ");"
    )
    publish_body_text = "_Callback(" + json.dumps(publish_payload) + ");"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cgi_upload_image" in url:
            if upload_calls is not None:
                upload_calls.append(request)
            return httpx.Response(
                200,
                content=upload_body_text.encode("utf-8"),
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        if "emotion_cgi_publish_v6" in url:
            if publish_calls is not None:
                publish_calls.append(request)
            return httpx.Response(
                publish_status,
                content=publish_body_text.encode("utf-8")
                if publish_status < 400
                else b"QZone said no",
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404, text=f"unmocked qzone url: {url}")

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport, base_url: str = "http://onebot.local") -> OneBotClient:
    return OneBotClient(base_url=base_url, transport=transport)


class _EffectStore:
    def __init__(self, *, fail_complete: bool = False) -> None:
        self.prepared: list[dict[str, Any]] = []
        self.completed: list[tuple[int, dict[str, Any]]] = []
        self.fail_complete = fail_complete

    async def prepare_effect(self, **kwargs: Any) -> Any:
        self.prepared.append(kwargs)
        return type("Effect", (), {"id": 1})()

    async def complete_effect(self, effect_id: int, **kwargs: Any) -> Any:
        if self.fail_complete:
            raise RuntimeError("receipt write failed")
        self.completed.append((effect_id, kwargs))
        return type("Effect", (), {"id": effect_id})()


_EFFECT_CONTEXT = {
    "source_system": "external",
    "source_job_id": "job-1",
    "occurrence_key": "external:job-1:1234",
}


# ---------------------------------------------------------------------------
# Schema / wire stability
# ---------------------------------------------------------------------------


def test_tool_name_wire_stable() -> None:
    assert QZONE_PUBLISH_TOOL == "qzone_publish"


def test_schema_shape() -> None:
    schema = qzone_publish_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "qzone_publish"
    props = schema["function"]["parameters"]["properties"]
    assert "text" in props
    assert "images" in props
    assert "generate" in props
    # The schema must NOT mark anything as required — text alone is
    # enough (so is images alone, or generate alone).
    assert schema["function"]["parameters"]["required"] == []


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_compute_gtk_deterministic() -> None:
    # Spot-check the QZone DJB hash with a known input.
    assert _compute_gtk("PKEY_ABCDEFGHIJK") == _EXPECTED_GTK
    # Different inputs → different tokens.
    assert _compute_gtk("PKEY_OTHER") != _EXPECTED_GTK


def test_extract_cookie_value_finds_p_skey() -> None:
    assert _extract_cookie_value(_QZONE_COOKIE, "p_skey") == "PKEY_ABCDEFGHIJK"
    assert _extract_cookie_value(_QZONE_COOKIE, "skey") == "@AbCdEf123"
    assert _extract_cookie_value(_QZONE_COOKIE, "missing") is None


def test_build_richval_tab_joined() -> None:
    rv = _build_richval(
        [
            {"albumid": "A1", "lloc": "L1", "sloc": "S1", "type": 0, "height": 100, "width": 200},
            {"albumid": "A2", "lloc": "L2", "sloc": "S2", "type": 0, "height": 50, "width": 25},
        ]
    )
    assert "\t" in rv
    segs = rv.split("\t")
    assert len(segs) == 2
    assert "A1" in segs[0] and "L1" in segs[0]
    assert "A2" in segs[1] and "L2" in segs[1]


def test_parse_publish_response_accepts_both_code_and_ret() -> None:
    classic = _parse_publish_response(json.dumps({"ret": 0, "tid": "T1"}))
    assert classic["ok"] is True
    assert classic["tid"] == "T1"
    newer = _parse_publish_response(json.dumps({"code": 0, "tid": "T2"}))
    assert newer["ok"] is True
    assert newer["tid"] == "T2"
    failure = _parse_publish_response(json.dumps({"ret": 4001, "msg": "no"}))
    assert failure["ok"] is False
    assert failure["code"] == 4001
    assert failure["error"] == "ret=4001, code=None, subcode=0"


def test_parse_upload_response_handles_jsonp_wrapper() -> None:
    body = "frameElement.callback({\"ret\":0,\"data\":{\"albumid\":\"X\",\"photoid\":\"Y\",\"width\":1,\"height\":1}});"
    parsed = _parse_upload_response(body)
    assert parsed["ok"] is True
    assert parsed["pic"]["albumid"] == "X"
    assert parsed["pic"]["lloc"] == "Y"


# ---------------------------------------------------------------------------
# OneBot HTTP client
# ---------------------------------------------------------------------------


def test_onebot_base_url_precedence_explicit_wins(monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_NAPCAT_HTTP_URL", "http://from-env.example")
    c = OneBotClient(
        base_url="http://explicit.example",
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert c.base_url == "http://explicit.example"


def test_onebot_base_url_from_ws_url_derivation(monkeypatch) -> None:
    monkeypatch.delenv("CORLINMAN_NAPCAT_HTTP_URL", raising=False)
    c = OneBotClient(
        ws_url="ws://napcat:6700/onebot",
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert c.base_url == "http://napcat:6700"


def test_onebot_base_url_wss_derives_https(monkeypatch) -> None:
    monkeypatch.delenv("CORLINMAN_NAPCAT_HTTP_URL", raising=False)
    c = OneBotClient(
        ws_url="wss://napcat.example/onebot/v11",
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert c.base_url == "https://napcat.example"


def test_onebot_missing_config_raises(monkeypatch) -> None:
    monkeypatch.delenv("CORLINMAN_NAPCAT_HTTP_URL", raising=False)
    try:
        OneBotClient()
    except OneBotError as exc:
        assert "not configured" in str(exc).lower()
    else:
        raise AssertionError("expected OneBotError")


async def test_onebot_websocket_fallback_matches_echo(monkeypatch) -> None:
    class _WebSocket:
        async def send(self, raw: str) -> None:
            request = json.loads(raw)
            self.response = json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 42, "nickname": "bot"},
                    "echo": request["echo"],
                }
            )

        def __aiter__(self):
            async def _items():
                yield json.dumps({"post_type": "meta_event"})
                yield self.response

            return _items()

    class _Connect:
        async def __aenter__(self) -> _WebSocket:
            self.ws = _WebSocket()
            return self.ws

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        "corlinman_agent.onebot.client.ws_connect",
        lambda *args, **kwargs: _Connect(),
    )
    client = OneBotClient(
        ws_url="ws://napcat.test:3001",
        transport=httpx.MockTransport(lambda _: httpx.Response(426)),
    )
    try:
        info = await client.fetch_login_info()
    finally:
        await client.aclose()
    assert info["qq"] == "42"


async def test_onebot_websocket_fallback_uses_unique_echoes(monkeypatch) -> None:
    echoes: list[str] = []

    class _WebSocket:
        async def send(self, raw: str) -> None:
            request = json.loads(raw)
            echoes.append(request["echo"])
            self.response = json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 42, "nickname": "bot"},
                    "echo": request["echo"],
                }
            )

        def __aiter__(self):
            async def _items():
                yield self.response

            return _items()

    class _Connect:
        async def __aenter__(self) -> _WebSocket:
            return _WebSocket()

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        "corlinman_agent.onebot.client.ws_connect",
        lambda *args, **kwargs: _Connect(),
    )
    client = OneBotClient(
        ws_url="ws://napcat.test:3001",
        transport=httpx.MockTransport(lambda _: httpx.Response(426)),
    )
    try:
        await client.fetch_login_info()
        await client.fetch_login_info()
    finally:
        await client.aclose()
    assert len(echoes) == 2
    assert len(set(echoes)) == 2


async def test_onebot_sidecar_supplies_ws_and_token(
    tmp_path: Path, monkeypatch
) -> None:
    sidecar = tmp_path / "py-config.json"
    sidecar.write_text(
        json.dumps(
            {
                "qq_onebot": {
                    "ws_url": "ws://sidecar.test:3001",
                    "access_token": "sidecar-token",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(sidecar))
    monkeypatch.delenv("CORLINMAN_NAPCAT_HTTP_URL", raising=False)
    client = OneBotClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    try:
        assert client.base_url == "http://sidecar.test:3001"
        assert client._ws_url == "ws://sidecar.test:3001"
        assert client._token == "sidecar-token"
    finally:
        await client.aclose()


async def test_onebot_fetch_login_info_happy(monkeypatch) -> None:
    client = _client(_onebot_transport())
    try:
        info = await client.fetch_login_info()
        assert info["qq"] == _QQ_UIN
        assert info["nickname"] == "Tester"
    finally:
        await client.aclose()


async def test_onebot_fetch_cookies_empty_raises() -> None:
    client = _client(_onebot_transport(return_empty_cookies=True))
    try:
        try:
            await client.fetch_cookies()
        except OneBotError as exc:
            assert "empty cookie" in str(exc).lower()
        else:
            raise AssertionError("expected OneBotError on empty cookies")
    finally:
        await client.aclose()


async def test_onebot_fetch_csrf_token_returns_int() -> None:
    client = _client(_onebot_transport())
    try:
        token = await client.fetch_csrf_token()
        assert isinstance(token, int)
        assert token == _EXPECTED_GTK
    finally:
        await client.aclose()


async def test_onebot_envelope_failed_surfaces_message() -> None:
    client = _client(_onebot_transport(fail_login=True))
    try:
        try:
            await client.fetch_login_info()
        except OneBotError as exc:
            assert "QQ offline" in str(exc)
        else:
            raise AssertionError("expected OneBotError on failed envelope")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# qzone_publish dispatcher
# ---------------------------------------------------------------------------


def _seed_workspace_image(monkeypatch, tmp_path: Path, name: str = "hello.png") -> Path:
    """Drop a PNG into the agent workspace + point DATA_DIR there."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    path = ws / name
    path.write_bytes(_FAKE_PNG)
    return path


async def test_dispatch_requires_text_or_image() -> None:
    out = json.loads(
        await dispatch_qzone_publish(args_json=json.dumps({}).encode())
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_dispatch_invalid_images_type() -> None:
    out = json.loads(
        await dispatch_qzone_publish(
            policy_resolver=lambda: False,
            args_json=json.dumps(
                {"text": "hi", "images": 42}
            ).encode()
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_dispatch_too_many_images(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    paths = []
    for i in range(11):
        p = _seed_workspace_image(monkeypatch, tmp_path, f"img{i}.png")
        paths.append(str(p))
    out = json.loads(
        await dispatch_qzone_publish(
            policy_resolver=lambda: False,
            args_json=json.dumps({"text": "hi", "images": paths}).encode()
        )
    )
    assert out["ok"] is False
    assert out["error"] == "too_many_images"


async def test_dispatch_image_not_found(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    out = json.loads(
        await dispatch_qzone_publish(
            policy_resolver=lambda: False,
            args_json=json.dumps(
                {"text": "hi", "images": ["nope.png"]}
            ).encode()
        )
    )
    assert out["ok"] is False
    assert out["error"] == "image_not_found"


async def test_dispatch_happy_path_text_plus_one_image(monkeypatch, tmp_path) -> None:
    """End-to-end: text + 1 image → upload → publish → tid in envelope."""
    _seed_workspace_image(monkeypatch, tmp_path, "kawaii.png")
    onebot = _client(_onebot_transport())
    upload_calls: list[httpx.Request] = []
    publish_calls: list[httpx.Request] = []
    qz_transport = _qzone_transport(
        upload_calls=upload_calls, publish_calls=publish_calls
    )
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps(
                    {
                        "text": "今天的猫猫",
                        "images": ["kawaii.png"],
                    }
                ).encode(),
                onebot_client=onebot,
                http_transport=qz_transport,
            )
        )
    finally:
        await onebot.aclose()
    assert out["ok"] is True, out
    assert out["tid"] == "FEED_TID_42"
    assert out["qzone_url"] == f"https://user.qzone.qq.com/{_QQ_UIN}/mood/FEED_TID_42"
    assert out["uin"] == _QQ_UIN
    assert out["images"] == 1
    assert out["generated"] is False

    # Sanity check the wire side: exactly one upload + one publish, and
    # both carry the g_tk computed from our seeded p_skey.
    assert len(upload_calls) == 1
    assert len(publish_calls) == 1
    assert f"g_tk={_EXPECTED_GTK}" in str(upload_calls[0].url)
    assert f"g_tk={_EXPECTED_GTK}" in str(publish_calls[0].url)

    # The publish form body must carry the user-supplied text — bytes
    # get url-encoded, so check the parsed form.
    publish_form = urllib.parse.parse_qs(publish_calls[0].content.decode("utf-8"))
    assert publish_form["con"][0] == "今天的猫猫"
    assert publish_form["hostuin"][0] == _QQ_UIN
    assert publish_form["richtype"][0] == "1"


async def test_dispatch_live_scheduler_publish_records_effect_receipt(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    onebot = _client(_onebot_transport())
    store = _EffectStore()
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps({"text": "今天很好"}).encode(),
                onebot_client=onebot,
                http_transport=_qzone_transport(),
                scheduler_store=store,
                effect_context=_EFFECT_CONTEXT,
            )
        )
    finally:
        await onebot.aclose()
    assert out["ok"] is True
    assert store.prepared == [
        {
            **_EFFECT_CONTEXT,
            "effect_kind": "qzone.publish",
            "effect_target": f"account:{_QQ_UIN}",
        }
    ]
    assert store.completed == [
        (
            1,
            {
                "state": "sent",
                "receipt": {
                    "tid": "FEED_TID_42",
                    "qzone_url": (
                        f"https://user.qzone.qq.com/{_QQ_UIN}/mood/FEED_TID_42"
                    ),
                    "uin": _QQ_UIN,
                },
                "error_code": None,
            },
        )
    ]


async def test_dispatch_publish_receipt_failure_blocks_resend(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    onebot = _client(_onebot_transport())
    store = _EffectStore(fail_complete=True)
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps({"text": "今天很好"}).encode(),
                onebot_client=onebot,
                http_transport=_qzone_transport(),
                scheduler_store=store,
                effect_context=_EFFECT_CONTEXT,
            )
        )
    finally:
        await onebot.aclose()
    assert out["ok"] is False
    assert out["error"] == "scheduler_effect_receipt_unknown"
    assert out["tid"] == "FEED_TID_42"


async def test_dispatch_onebot_failure_returns_envelope(monkeypatch, tmp_path) -> None:
    _seed_workspace_image(monkeypatch, tmp_path, "x.png")
    onebot = _client(_onebot_transport(fail_login=True))
    qz_transport = _qzone_transport()
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps(
                    {"text": "hi", "images": ["x.png"]}
                ).encode(),
                onebot_client=onebot,
                http_transport=qz_transport,
            )
        )
    finally:
        await onebot.aclose()
    assert out["ok"] is False
    assert out["error"] == "onebot_failed"
    assert "QQ offline" in out["message"]


async def test_dispatch_image_upload_rejected_skips_publish(monkeypatch, tmp_path) -> None:
    _seed_workspace_image(monkeypatch, tmp_path, "x.png")
    onebot = _client(_onebot_transport())
    publish_calls: list[httpx.Request] = []
    qz_transport = _qzone_transport(
        upload_ret=8001, publish_calls=publish_calls
    )
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps(
                    {"text": "hi", "images": ["x.png"]}
                ).encode(),
                onebot_client=onebot,
                http_transport=qz_transport,
            )
        )
    finally:
        await onebot.aclose()
    assert out["ok"] is False
    assert out["error"] == "image_upload_failed"
    assert "upload broke" not in out["message"]
    assert "ret=8001" in out["message"]
    # Publish endpoint must NOT have been touched after the upload fail.
    assert publish_calls == []


async def test_dispatch_qzone_nonzero_retcode_propagates(monkeypatch, tmp_path) -> None:
    _seed_workspace_image(monkeypatch, tmp_path, "x.png")
    onebot = _client(_onebot_transport())
    qz_transport = _qzone_transport(
        publish_payload={"code": 4002, "subcode": 1, "msg": "blocked by rc"}
    )
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps(
                    {"text": "hi", "images": ["x.png"]}
                ).encode(),
                onebot_client=onebot,
                http_transport=qz_transport,
            )
        )
    finally:
        await onebot.aclose()
    assert out["ok"] is False
    assert out["error"] == "qzone_rejected"
    assert out.get("code") == 4002
    assert "blocked by rc" not in out["message"]
    assert "code=4002" in out["message"]


async def test_dispatch_http_error_does_not_echo_response_body(
    monkeypatch, tmp_path
) -> None:
    _seed_workspace_image(monkeypatch, tmp_path, "x.png")
    onebot = _client(_onebot_transport())
    marker = "PRIVATE_QZONE_RESPONSE"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cgi_upload_image"):
            body = "frameElement.callback(" + json.dumps({"ret": 0, "data": {}}) + ");"
            return httpx.Response(200, text=body)
        return httpx.Response(502, text=marker)

    try:
        out = json.loads(
            await dispatch_qzone_publish(
                policy_resolver=lambda: False,
                args_json=json.dumps({"text": "hi", "images": ["x.png"]}).encode(),
                onebot_client=onebot,
                http_transport=httpx.MockTransport(handler),
            )
        )
    finally:
        await onebot.aclose()
    assert out["error"] == "qzone_publish_failed"
    assert marker not in out["message"]
    assert "HTTP 502" in out["message"]


async def test_dispatch_stale_login_missing_p_skey(monkeypatch, tmp_path) -> None:
    _seed_workspace_image(monkeypatch, tmp_path, "x.png")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/get_login_info"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": int(_QQ_UIN), "nickname": "Tester"},
                },
            )
        if path.endswith("/get_cookies"):
            # ``p_skey`` deliberately absent — simulates a half-fresh
            # login where the QZone token hasn't been negotiated yet.
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"cookies": "uin=o10001; skey=X"},
                },
            )
        return httpx.Response(404, text="unmocked")

    onebot = _client(httpx.MockTransport(handler))
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps(
                    {"text": "hi", "images": ["x.png"]}
                ).encode(),
                onebot_client=onebot,
                http_transport=_qzone_transport(),
            )
        )
    finally:
        await onebot.aclose()
    assert out["ok"] is False
    assert out["error"] == "qzone_cookie_stale"


# ---------------------------------------------------------------------------
# ``generate`` parameter
# ---------------------------------------------------------------------------


async def test_dispatch_generate_prepends_image(monkeypatch, tmp_path) -> None:
    """A ``generate`` arg should run image_with_refs first, write a PNG
    to the workspace, and the resulting path should be prepended to
    ``images`` before the upload step runs."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    # Also seed a user-supplied image so we can assert ordering.
    user_image = ws / "user.png"
    user_image.write_bytes(_FAKE_PNG)

    seen_gen_calls: list[dict] = []
    generated_path = ws / "generated" / "stub.png"
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    generated_path.write_bytes(_FAKE_PNG[::-1])

    async def fake_image_with_refs(*, args_json, **kwargs):
        seen_gen_calls.append(json.loads(args_json))
        return json.dumps(
            {
                "ok": True,
                "path": str(generated_path),
                "mime": "image/png",
                "chars_used": ["front"],
                "chars_missing": [],
                "persona_id": "kawaii",
                "size_bytes": len(_FAKE_PNG),
            }
        )

    onebot = _client(_onebot_transport())
    upload_calls: list[httpx.Request] = []
    qz_transport = _qzone_transport(upload_calls=upload_calls)
    try:
        out = json.loads(
            await dispatch_qzone_publish(
            policy_resolver=lambda: False,
                args_json=json.dumps(
                    {
                        "text": "猫猫合照",
                        "images": ["user.png"],
                        "generate": {
                            "prompt": "cat sipping tea",
                            "characters": ["front"],
                            "persona_id": "kawaii",
                        },
                    }
                ).encode(),
                onebot_client=onebot,
                http_transport=qz_transport,
                image_with_refs_dispatcher=fake_image_with_refs,
                image_with_refs_kwargs={
                    "provider": object(),
                    "persona_store": object(),
                    "asset_store": object(),
                    "bound_persona_id": None,
                },
            )
        )
    finally:
        await onebot.aclose()

    assert out["ok"] is True, out
    assert out["generated"] is True
    assert out["images"] == 2  # generated + user
    # The fake image_with_refs was invoked once, with the inner args
    # we asked the dispatcher to forward.
    assert len(seen_gen_calls) == 1
    assert seen_gen_calls[0]["prompt"] == "cat sipping tea"
    assert seen_gen_calls[0]["characters"] == ["front"]
    # Both uploads happened — order is generated-first.
    assert len(upload_calls) == 2
    # The upload form carries the filename of each image. Decode the
    # form to confirm the first call uploaded the generated PNG and
    # the second uploaded the user PNG.
    first_form = urllib.parse.parse_qs(upload_calls[0].content.decode("utf-8"))
    second_form = urllib.parse.parse_qs(upload_calls[1].content.decode("utf-8"))
    assert first_form["filename"][0] == "stub.png"
    assert second_form["filename"][0] == "user.png"


async def test_dispatch_generate_failure_returns_envelope(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    async def failing_image_with_refs(*, args_json, **kwargs):
        return json.dumps(
            {
                "ok": False,
                "error": "provider_unavailable",
                "message": "no api key",
            }
        )

    out = json.loads(
        await dispatch_qzone_publish(
            policy_resolver=lambda: False,
            args_json=json.dumps(
                {
                    "text": "hi",
                    "generate": {
                        "prompt": "x",
                        "characters": ["front"],
                    },
                }
            ).encode(),
            image_with_refs_dispatcher=failing_image_with_refs,
            image_with_refs_kwargs={},
        )
    )
    assert out["ok"] is False
    assert out["error"] == "image_with_refs_failed"
    assert "provider_unavailable" in out["message"]


async def test_dispatch_generate_without_dispatcher_errors(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    out = json.loads(
        await dispatch_qzone_publish(
            policy_resolver=lambda: False,
            args_json=json.dumps(
                {
                    "text": "hi",
                    "generate": {"prompt": "x", "characters": ["front"]},
                }
            ).encode(),
        )
    )
    assert out["ok"] is False
    assert out["error"] == "image_with_refs_unavailable"
