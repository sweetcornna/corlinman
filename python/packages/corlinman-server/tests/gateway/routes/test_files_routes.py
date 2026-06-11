"""Coverage for the ``/v1/files`` web-chat file store (W2).

Auth is middleware-layer (``ApiKeyAuthMiddleware`` + the admin-session
bridge) and covered by ``test_admin_session_bridge.py`` — these tests
mount the bare router and exercise the storage contract itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.gateway.routes import files as files_route
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(files_route.router())
    return TestClient(app)


def _upload(
    client: TestClient,
    *,
    name: str = "pic.png",
    mime: str = "image/png",
    body: bytes = b"\x89PNG fake bytes",
) -> dict[str, object]:
    resp = client.post("/v1/files", files={"file": (name, body, mime)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_upload_download_roundtrip(client: TestClient, tmp_path: Path) -> None:
    body = b"\x89PNG fake bytes"
    meta = _upload(client, body=body)

    assert meta["name"] == "pic.png"
    assert meta["mime"] == "image/png"
    assert meta["size"] == len(body)
    assert meta["url"] == f"/v1/files/{meta['file_id']}"
    # Blob + sidecar both on disk under <data_dir>/files/.
    assert (tmp_path / "files" / f"{meta['file_id']}.blob").is_file()
    assert (tmp_path / "files" / f"{meta['file_id']}.json").is_file()

    got = client.get(str(meta["url"]))
    assert got.status_code == 200
    assert got.content == body
    assert got.headers["content-type"].startswith("image/png")
    assert got.headers["content-disposition"].startswith("inline")


def test_non_image_served_as_attachment(client: TestClient) -> None:
    meta = _upload(
        client, name="notes.pdf", mime="application/pdf", body=b"%PDF-1.7"
    )
    got = client.get(str(meta["url"]))
    assert got.status_code == 200
    assert got.headers["content-disposition"].startswith("attachment")


def test_svg_never_served_inline(client: TestClient) -> None:
    """SVG is a script container — inline rendering from the gateway
    origin would be stored XSS with the admin cookie in scope."""
    meta = _upload(
        client,
        name="evil.svg",
        mime="image/svg+xml",
        body=b"<svg onload='alert(1)'/>",
    )
    got = client.get(str(meta["url"]))
    assert got.status_code == 200
    assert got.headers["content-disposition"].startswith("attachment")


def test_cjk_filename_survives_roundtrip(client: TestClient) -> None:
    meta = _upload(client, name="截图 2026.png")
    got = client.get(str(meta["url"]))
    assert got.status_code == 200
    # RFC 5987 form carries the UTF-8 name; plain token is ASCII-safe.
    cd = got.headers["content-disposition"]
    assert "filename*=UTF-8''" in cd
    assert "%E6%88%AA%E5%9B%BE" in cd  # "截图"


def test_unknown_id_404(client: TestClient) -> None:
    assert client.get("/v1/files/" + "0" * 26).status_code == 404


def test_traversal_ids_rejected(client: TestClient, tmp_path: Path) -> None:
    # Plant a file outside files/ that a traversal would reach.
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    for evil in ("..%2Fsecret", "../secret", "a" * 25 + "/", "A" * 26):
        resp = client.get(f"/v1/files/{evil}")
        assert resp.status_code == 404, evil


def test_empty_file_rejected(client: TestClient) -> None:
    resp = client.post("/v1/files", files={"file": ("e.txt", b"", "text/plain")})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_file"


def test_oversize_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_FILES_MAX_BYTES", "8")
    resp = client.post(
        "/v1/files", files={"file": ("big.bin", b"123456789", "application/x-bin")}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"


def test_upload_without_data_dir_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_DATA_DIR", raising=False)
    monkeypatch.setattr(files_route, "_data_dir", lambda: None)
    app = FastAPI()
    app.include_router(files_route.router())
    resp = TestClient(app).post(
        "/v1/files", files={"file": ("a.txt", b"x", "text/plain")}
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "storage_unavailable"


def test_configured_data_dir_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot-resolved dir (``--data-dir`` / ``[server].data_dir``,
    stamped via ``configure_data_dir``) must beat the env fallback so
    chat files land in the same tree as the journal/session stores."""
    env_dir = tmp_path / "env-tree"
    boot_dir = tmp_path / "boot-tree"
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(env_dir))
    files_route.configure_data_dir(boot_dir)
    try:
        app = FastAPI()
        app.include_router(files_route.router())
        client = TestClient(app)
        meta = _upload(client)
        assert (boot_dir / "files" / f"{meta['file_id']}.blob").is_file()
        assert not (env_dir / "files").exists()
        # Serve path resolves from the same configured tree.
        assert client.get(str(meta["url"])).status_code == 200
    finally:
        files_route.configure_data_dir(None)


def test_files_prefix_requires_chat_scope() -> None:
    """SEC-09 parity: the attachment store carries the same required
    scope as /v1/chat so a narrower key can't read/plant attachments."""
    from corlinman_server.gateway.middleware.auth import (
        DEFAULT_REQUIRED_SCOPES,
    )

    assert ("/v1/files", "chat") in DEFAULT_REQUIRED_SCOPES
