"""Server→client inbound frame router (Dim 5: sampling + list_changed base).

The bespoke JSON-RPC client used to demux every inbound frame by response
``id`` and drop anything unmatched — so a server-initiated request
(``sampling/createMessage``) or notification (``tools/list_changed``) went
nowhere. These tests pin the new router: classify request/notification/
response, dispatch to handlers, reply to unhandled requests with
METHOD_NOT_FOUND so a server never hangs, and leave the response path
untouched.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_mcp_server import McpClient
from corlinman_mcp_server.types import classify_inbound, error_codes

# ---------------------------------------------------------------------------
# classify_inbound — pure unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage"}, "request"),
        ({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}, "notification"),
        # Explicit "id": null with a method is a REQUEST per JSON-RPC §4
        # (id present, even null, expects a reply) — not a notification.
        ({"jsonrpc": "2.0", "id": None, "method": "sampling/createMessage"}, "request"),
        # A true notification omits the id member entirely.
        ({"jsonrpc": "2.0", "method": "notifications/x"}, "notification"),
        ({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}, "response"),
        ({"jsonrpc": "2.0", "id": 7, "error": {"code": -1, "message": "x"}}, "response"),
        ({"jsonrpc": "2.0", "id": "abc", "method": ""}, "response"),  # empty method → not a request
    ],
)
def test_classify_inbound(frame: dict, expected: str) -> None:
    assert classify_inbound(frame) == expected


# ---------------------------------------------------------------------------
# Reader-loop routing (stdio, real subprocess)
# ---------------------------------------------------------------------------


async def _client_from_script(script: str) -> McpClient:
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-u",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return await McpClient.connect_with_process(proc)


@pytest.mark.asyncio
async def test_server_request_routed_to_handler() -> None:
    # Child emits a server-initiated request, then stays alive.
    script = (
        "import sys,json,time\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':'srv-1',"
        "'method':'sampling/createMessage','params':{'maxTokens':10}})+chr(10))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    client = await _client_from_script(script)
    seen: asyncio.Future = asyncio.get_event_loop().create_future()

    async def handler(method: str, params: dict):
        if not seen.done():
            seen.set_result((method, params))
        return {"role": "assistant", "content": {"type": "text", "text": "hi"}}, None

    client.on_server_request = handler
    try:
        method, params = await asyncio.wait_for(seen, timeout=5)
        assert method == "sampling/createMessage"
        assert params == {"maxTokens": 10}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_notification_routed_to_handler() -> None:
    script = (
        "import sys,json,time\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0',"
        "'method':'notifications/tools/list_changed'})+chr(10))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    client = await _client_from_script(script)
    seen: asyncio.Future = asyncio.get_event_loop().create_future()

    async def handler(method: str, params: dict):
        if not seen.done():
            seen.set_result(method)

    client.on_notification = handler
    try:
        method = await asyncio.wait_for(seen, timeout=5)
        assert method == "notifications/tools/list_changed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unhandled_server_request_gets_method_not_found() -> None:
    # An unset on_server_request must reply METHOD_NOT_FOUND so a compliant
    # server never hangs. Capture the enqueued reply directly (race-free).
    client = await McpClient.connect_stdio("cat", [])
    captured: list[dict] = []

    async def fake_enqueue(frame: dict) -> None:
        captured.append(frame)

    client._enqueue = fake_enqueue  # type: ignore[method-assign]
    try:
        await client._dispatch_server_request(
            {"jsonrpc": "2.0", "id": "srv-9", "method": "sampling/createMessage", "params": {}}
        )
        assert len(captured) == 1
        assert captured[0]["id"] == "srv-9"
        assert captured[0]["error"]["code"] == error_codes.METHOD_NOT_FOUND
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_handler_result_and_error_replies() -> None:
    client = await McpClient.connect_stdio("cat", [])
    captured: list[dict] = []
    client._enqueue = lambda frame: _record(captured, frame)  # type: ignore[method-assign]

    async def ok_handler(method, params):
        return {"text": "done"}, None

    async def err_handler(method, params):
        return None, {"code": error_codes.RATE_LIMITED, "message": "slow down"}

    try:
        client.on_server_request = ok_handler
        await client._dispatch_server_request({"jsonrpc": "2.0", "id": 1, "method": "x", "params": {}})
        assert captured[-1] == {"jsonrpc": "2.0", "id": 1, "result": {"text": "done"}}

        client.on_server_request = err_handler
        await client._dispatch_server_request({"jsonrpc": "2.0", "id": 2, "method": "x", "params": {}})
        assert captured[-1]["error"]["code"] == error_codes.RATE_LIMITED
    finally:
        await client.close()


async def _record(bucket: list, frame: dict) -> None:
    bucket.append(frame)


@pytest.mark.asyncio
async def test_response_path_unchanged_with_handlers_set() -> None:
    # A normal request/response still resolves even with inbound handlers wired.
    script = (
        "import sys,json\n"
        "for line in sys.stdin:\n"
        "    obj=json.loads(line)\n"
        "    if obj.get('id') is not None and obj.get('method'):\n"
        "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':obj['id'],"
        "'result':{'pong':True}})+chr(10))\n"
        "        sys.stdout.flush()\n"
    )
    client = await _client_from_script(script)
    client.on_server_request = lambda m, p: _noop_request()
    client.on_notification = lambda m, p: _noop()
    try:
        result = await asyncio.wait_for(client.call("ping", None), timeout=5)
        assert result == {"pong": True}
    finally:
        await client.close()


async def _noop_request():
    return None, {"code": error_codes.METHOD_NOT_FOUND, "message": "no"}


async def _noop():
    return None
