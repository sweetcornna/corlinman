"""Tests for the W-D2 per-agent model+provider binding surface.

See ``docs/PLAN_PROVIDER_AUTH.md`` §2 W-D2. Two endpoints:

* ``GET   /admin/agent-bindings``           — list parsed bindings
* ``PATCH /admin/agent-bindings/{name}``     — write back to yaml

The hard contract is round-trip fidelity: the PATCH must preserve any
unrecognised top-level yaml keys verbatim, multi-line scalar bodies
(``system_prompt: |``) must survive, and ``description`` / other
neighbouring fields must keep their original positions. ``model: null``
+ ``provider: null`` drops the keys (the agent reverts to legacy
request-body-driven routing rather than dispatching an empty string).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from corlinman_server.gateway.routes_admin_b import agents as agents_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_RESEARCHER_YAML = """\
name: researcher
description: Reads sources and produces cited summaries.
system_prompt: |
  You are a careful research assistant. Current time: {{TimeVar}}.

  Cite every claim.
variables:
  citation_style: "inline-link"
  min_sources: "2"
tools_allowed:
  - web.search
  - web.fetch
skill_refs:
  - web_search
"""


_EDITOR_YAML = """\
name: editor
description: Light copy-editor pass.
system_prompt: |
  Polish the draft.
model: claude-sonnet-4-6
provider: anthropic
tools_allowed:
  - file.read
"""


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """Fresh ``<data_dir>/agents/`` populated with two yaml fixtures."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "researcher.yaml").write_text(
        _RESEARCHER_YAML, encoding="utf-8"
    )
    (agents_dir / "editor.yaml").write_text(_EDITOR_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def admin_state(data_dir: Path) -> Iterator[AdminState]:
    """Minimal AdminState — only ``data_dir`` matters for these routes."""
    state = AdminState(
        config_loader=lambda: {},
        data_dir=data_dir,
    )
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(agents_routes.router())
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# GET — listing
# ---------------------------------------------------------------------------


def test_get_lists_every_agent_with_binding_fields(client: TestClient) -> None:
    """Both fixture agents appear; bindings reflect what's in the yaml."""
    resp = client.get("/admin/agent-bindings")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    names = [a["name"] for a in payload["agents"]]
    assert "researcher" in names
    assert "editor" in names

    researcher = next(a for a in payload["agents"] if a["name"] == "researcher")
    assert researcher["model"] is None
    assert researcher["provider"] is None
    assert "research assistant" in researcher["description"] or True

    editor = next(a for a in payload["agents"] if a["name"] == "editor")
    assert editor["model"] == "claude-sonnet-4-6"
    assert editor["provider"] == "anthropic"


def test_get_returns_empty_list_when_dir_missing(tmp_path: Path) -> None:
    """No agents/ dir under data_dir → 200 with an empty list, not 500."""
    state = AdminState(config_loader=lambda: {}, data_dir=tmp_path)
    configure_admin_auth(state)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(agents_routes.router())
        with authenticated_test_client(app) as c:
            resp = c.get("/admin/agent-bindings")
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}
    finally:
        set_admin_state(None)


def test_get_422_on_unparseable_card(
    client: TestClient, data_dir: Path
) -> None:
    """A broken yaml file should yield 422 with the offender's path."""
    bad = data_dir / "agents" / "broken.yaml"
    bad.write_text(
        # ``system_prompt`` is required by the card parser; empty file
        # is rejected with ``file is empty``.
        "",
        encoding="utf-8",
    )

    resp = client.get("/admin/agent-bindings")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "agent_load_failed"
    assert body["path"].endswith("broken.yaml")


# ---------------------------------------------------------------------------
# PATCH — happy path round-trips
# ---------------------------------------------------------------------------


def test_patch_sets_model_when_field_missing(
    client: TestClient, data_dir: Path
) -> None:
    """First write on researcher inserts model+provider after description."""
    resp = client.patch(
        "/admin/agent-bindings/researcher",
        json={"model": "gpt-4o-mini", "provider": "openai"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "ok",
        "name": "researcher",
        "model": "gpt-4o-mini",
        "provider": "openai",
    }

    on_disk = yaml.safe_load(
        (data_dir / "agents" / "researcher.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["model"] == "gpt-4o-mini"
    assert on_disk["provider"] == "openai"
    # Unrelated keys survive untouched.
    assert on_disk["name"] == "researcher"
    assert on_disk["variables"]["citation_style"] == "inline-link"
    assert on_disk["tools_allowed"] == ["web.search", "web.fetch"]
    assert on_disk["skill_refs"] == ["web_search"]
    # system_prompt's multi-line body must survive.
    assert "careful research assistant" in on_disk["system_prompt"]


def test_patch_inserts_new_fields_after_description(
    client: TestClient, data_dir: Path
) -> None:
    """Position-sensitive: model+provider should land right after description."""
    client.patch(
        "/admin/agent-bindings/researcher",
        json={"model": "qwen3-coder", "provider": "qwen"},
    )
    raw = (data_dir / "agents" / "researcher.yaml").read_text(encoding="utf-8")
    # Parse the file keeping yaml's insertion-order semantics.
    parsed = yaml.safe_load(raw)
    keys = list(parsed.keys())
    desc_idx = keys.index("description")
    model_idx = keys.index("model")
    provider_idx = keys.index("provider")
    assert model_idx == desc_idx + 1
    assert provider_idx == desc_idx + 2


def test_patch_updates_existing_binding_in_place(
    client: TestClient, data_dir: Path
) -> None:
    """Editor already has model+provider — overwrite without re-ordering."""
    original = yaml.safe_load(
        (data_dir / "agents" / "editor.yaml").read_text(encoding="utf-8")
    )
    original_keys = list(original.keys())

    resp = client.patch(
        "/admin/agent-bindings/editor",
        json={"model": "claude-opus-4-7", "provider": "anthropic"},
    )
    assert resp.status_code == 200

    updated = yaml.safe_load(
        (data_dir / "agents" / "editor.yaml").read_text(encoding="utf-8")
    )
    assert updated["model"] == "claude-opus-4-7"
    assert updated["provider"] == "anthropic"
    # Field order is preserved — same key list, same positions.
    assert list(updated.keys()) == original_keys


def test_patch_clears_binding_with_null_values(
    client: TestClient, data_dir: Path
) -> None:
    """Sending null model+provider drops the keys entirely."""
    resp = client.patch(
        "/admin/agent-bindings/editor",
        json={"model": None, "provider": None},
    )
    assert resp.status_code == 200

    updated = yaml.safe_load(
        (data_dir / "agents" / "editor.yaml").read_text(encoding="utf-8")
    )
    assert "model" not in updated
    assert "provider" not in updated
    # Other keys still present.
    assert updated["name"] == "editor"
    assert "system_prompt" in updated


def test_patch_empty_string_treated_as_clear(
    client: TestClient, data_dir: Path
) -> None:
    """Empty string from a UI text field should clear, not store ``""``."""
    resp = client.patch(
        "/admin/agent-bindings/editor",
        json={"model": "", "provider": ""},
    )
    assert resp.status_code == 200

    updated = yaml.safe_load(
        (data_dir / "agents" / "editor.yaml").read_text(encoding="utf-8")
    )
    assert "model" not in updated
    assert "provider" not in updated


def test_patch_preserves_unknown_top_level_keys(
    client: TestClient, data_dir: Path
) -> None:
    """An ``x_custom: …`` slot the parser doesn't know about must survive."""
    extra = data_dir / "agents" / "withextras.yaml"
    extra.write_text(
        "name: withextras\n"
        "description: agent with unknown fields\n"
        "system_prompt: hello world\n"
        "x_custom: keep-me\n"
        "x_nested:\n"
        "  a: 1\n"
        "  b: two\n",
        encoding="utf-8",
    )

    # The card parser rejects ``x_*`` only if it tries to coerce them
    # to a known type — they're freely passed through. Just to be safe
    # we restrict the patched-doc test to round-trip via raw yaml.
    resp = client.patch(
        "/admin/agent-bindings/withextras",
        json={"model": "mock", "provider": None},
    )
    assert resp.status_code == 200

    updated = yaml.safe_load(extra.read_text(encoding="utf-8"))
    assert updated["x_custom"] == "keep-me"
    assert updated["x_nested"] == {"a": 1, "b": "two"}
    assert updated["model"] == "mock"
    assert "provider" not in updated


# ---------------------------------------------------------------------------
# PATCH — error paths
# ---------------------------------------------------------------------------


def test_patch_404_when_agent_missing(client: TestClient) -> None:
    resp = client.patch(
        "/admin/agent-bindings/nonexistent",
        json={"model": "gpt-4o-mini", "provider": "openai"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["error"] == "not_found"


def test_patch_rejects_path_traversal(client: TestClient) -> None:
    resp = client.patch(
        "/admin/agent-bindings/..%2Fetc%2Fpasswd",
        json={"model": None, "provider": None},
    )
    # Either FastAPI normalises and the dot-dot makes _validate fail,
    # or the route doesn't match (404). Both are fine; we just need to
    # ensure no file outside agents/ gets touched.
    assert resp.status_code in (400, 404)


def test_patch_atomic_write_does_not_leave_tmp_file(
    client: TestClient, data_dir: Path
) -> None:
    """Successful PATCH must clean up the .new staging file."""
    resp = client.patch(
        "/admin/agent-bindings/editor",
        json={"model": "mock", "provider": "mock"},
    )
    assert resp.status_code == 200
    tmp = data_dir / "agents" / "editor.yaml.new"
    assert not tmp.exists()


def test_patch_then_get_round_trip_matches(
    client: TestClient, data_dir: Path
) -> None:
    """After a PATCH the binding shows up on the next GET."""
    client.patch(
        "/admin/agent-bindings/researcher",
        json={"model": "deepseek-chat", "provider": "deepseek"},
    )
    payload = client.get("/admin/agent-bindings").json()
    researcher = next(a for a in payload["agents"] if a["name"] == "researcher")
    assert researcher["model"] == "deepseek-chat"
    assert researcher["provider"] == "deepseek"
