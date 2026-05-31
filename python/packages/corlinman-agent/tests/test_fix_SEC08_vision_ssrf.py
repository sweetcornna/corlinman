"""SEC-08 repro: vision_analyze forwards a URL with no SSRF check and
accepts http:// despite an https-only schema.

The url branch only checks ``startswith(("http://", "https://"))`` then
forwards the URL verbatim to the provider, which downloads it server-side —
a textbook SSRF to the cloud metadata endpoint. The fix runs is_safe_host
(which also rejects non-https) before forwarding.
"""

from __future__ import annotations

import json
import socket

import pytest
from corlinman_agent.image.analyze import dispatch_vision_analyze

_PUBLIC_TEST_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a hostname to a known PUBLIC ip so is_safe_host's DNS path is
    deterministic offline (mirrors test_web_tools._fake_dns)."""
    real = socket.getaddrinfo

    def _fake(host: str, *args, **kw):  # type: ignore[no-untyped-def]
        if host and host.endswith("example.com"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_TEST_IP, 0))]
        return real(host, *args, **kw)

    from corlinman_agent.web import _common as wc

    monkeypatch.setattr(wc.socket, "getaddrinfo", _fake)


def test_metadata_https_url_refused() -> None:
    # https metadata endpoint must be refused by the SSRF guard (not by the
    # https-only check), proving is_safe_host actually runs on forwarding.
    out = dispatch_vision_analyze(
        args_json=json.dumps({"url": "https://169.254.169.254/latest/meta-data/"})
    )
    # Before the fix this returns a list[dict] (the forwarded image_url block).
    assert isinstance(out, str), f"SSRF url was forwarded, got {out!r}"
    env = json.loads(out)
    assert env["ok"] is False
    assert env["error"] == "unsafe_host"
    # Metadata endpoint should be the reason.
    assert "169.254" in env["message"] or "metadata" in env["message"].lower()


def test_plain_http_url_refused_https_only() -> None:
    out = dispatch_vision_analyze(
        args_json=json.dumps({"url": "http://example.com/pic.png"})
    )
    assert isinstance(out, str), f"http url was forwarded, got {out!r}"
    env = json.loads(out)
    assert env["ok"] is False


def test_https_public_url_still_forwarded() -> None:
    out = dispatch_vision_analyze(
        args_json=json.dumps({"url": "https://example.com/pic.png"})
    )
    # A safe public https URL must still be forwarded as an image block.
    assert isinstance(out, list)
    assert out[0]["type"] == "image_url"
    assert out[0]["image_url"]["url"] == "https://example.com/pic.png"
