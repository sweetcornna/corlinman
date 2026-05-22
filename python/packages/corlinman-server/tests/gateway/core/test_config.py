"""Tests for ``gateway.core.config`` — P0 config loader.

Covers :func:`load_from_path`, :func:`parse_config`, and
:func:`resolve_env_refs` against the shapes ``docs/contracts/runtime-wiring.md``
§1 defines.

* valid TOML → resolved dict with all expected sections
* ``{ env = "X" }`` ref resolved when env var is set
* ``{ env = "X", default = "Y" }`` falls to default when env var is unset
* ``{ env = "X" }`` → ``None`` when env var unset and no default
* an empty-string env var is treated as *present* (mirrors Rust behaviour)
* multi-key table containing an ``env`` key is **not** an env-ref
* missing file raises ``FileNotFoundError``
* malformed TOML raises ``tomllib.TOMLDecodeError``
* nested sections are resolved recursively (lists, nested dicts)
* ``parse_config`` is equivalent to ``load_from_path`` but takes a string
* ``resolve_env_refs`` is the standalone recursive resolver
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from corlinman_server.gateway.core.config import (
    load_from_path,
    parse_config,
    resolve_env_refs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_TOML = """\
[server]
host = "0.0.0.0"
port = 8080

[admin]
user = "admin"
"""

_FULL_TOML = """\
[server]
host = "127.0.0.1"
port = 8080

[admin]
user = "admin"

[providers.openai]
kind = "openai"
api_key = {env = "OPENAI_API_KEY"}

[providers.proxy]
kind = "openai_compatible"
base_url = "http://localhost:9999/v1"
api_key = {env = "PROXY_KEY", default = "no-auth"}

[models]
default = "gpt-4o-mini"

[models.aliases.gpt-4o-mini]
provider = "openai"
model = "gpt-4o-mini"

[channels.qq]
enabled = true
ws_url = {env = "QQ_WS_URL", default = "ws://localhost:3001"}
self_ids = [123]
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# load_from_path — valid TOML
# ---------------------------------------------------------------------------


def test_load_minimal_returns_dict(tmp_path: Path) -> None:
    p = _write(tmp_path, _MINIMAL_TOML)
    cfg = load_from_path(p)
    assert isinstance(cfg, dict)
    assert cfg["server"]["host"] == "0.0.0.0"
    assert cfg["server"]["port"] == 8080
    assert cfg["admin"]["user"] == "admin"


def test_load_accepts_str_path(tmp_path: Path) -> None:
    p = _write(tmp_path, _MINIMAL_TOML)
    cfg = load_from_path(str(p))  # str, not Path
    assert isinstance(cfg, dict)
    assert "server" in cfg


def test_load_all_sections_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("QQ_WS_URL", "ws://napcat:3001")
    p = _write(tmp_path, _FULL_TOML)
    cfg = load_from_path(p)
    assert "server" in cfg
    assert "admin" in cfg
    assert "providers" in cfg
    assert "models" in cfg
    assert "channels" in cfg


# ---------------------------------------------------------------------------
# load_from_path — errors
# ---------------------------------------------------------------------------


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_from_path(tmp_path / "nonexistent.toml")


def test_load_malformed_toml_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("not valid toml !!!= [\n", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_from_path(p)


# ---------------------------------------------------------------------------
# parse_config — string-level interface
# ---------------------------------------------------------------------------


def test_parse_config_minimal() -> None:
    cfg = parse_config(_MINIMAL_TOML)
    assert isinstance(cfg, dict)
    assert cfg["server"]["host"] == "0.0.0.0"


def test_parse_config_malformed_raises() -> None:
    with pytest.raises(tomllib.TOMLDecodeError):
        parse_config("bad = [\n{}")


def test_parse_config_empty_document() -> None:
    cfg = parse_config("")
    assert cfg == {}


def test_parse_config_resolves_env_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", "abc123")
    cfg = parse_config('[p]\nkey = {env = "MY_SECRET"}\n')
    assert cfg["p"]["key"] == "abc123"


# ---------------------------------------------------------------------------
# resolve_env_refs — scalar pass-through
# ---------------------------------------------------------------------------


def test_resolve_env_refs_scalar_passthrough() -> None:
    assert resolve_env_refs(42) == 42
    assert resolve_env_refs("hello") == "hello"
    assert resolve_env_refs(None) is None
    assert resolve_env_refs(True) is True


def test_resolve_env_refs_non_env_dict_passthrough() -> None:
    original = {"key": "value", "num": 1}
    result = resolve_env_refs(original)
    assert result == original


def test_resolve_env_refs_list_passthrough() -> None:
    lst = [1, "two", 3]
    assert resolve_env_refs(lst) == lst


# ---------------------------------------------------------------------------
# resolve_env_refs — env-ref resolution
# ---------------------------------------------------------------------------


def test_resolve_env_ref_set_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_TEST_VAR", "test-value")
    result = resolve_env_refs({"env": "CORLINMAN_TEST_VAR"})
    assert result == "test-value"


def test_resolve_env_ref_unset_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_MISSING_VAR", raising=False)
    result = resolve_env_refs({"env": "CORLINMAN_MISSING_VAR"})
    assert result is None


def test_resolve_env_ref_unset_with_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_MISSING_VAR", raising=False)
    result = resolve_env_refs({"env": "CORLINMAN_MISSING_VAR", "default": "fallback"})
    assert result == "fallback"


def test_resolve_env_ref_set_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_OVERRIDE", "live-value")
    result = resolve_env_refs({"env": "CORLINMAN_OVERRIDE", "default": "fallback"})
    assert result == "live-value"


def test_resolve_env_ref_empty_string_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty-string env var counts as *present* — mirrors Rust ``std::env::var``."""
    monkeypatch.setenv("CORLINMAN_EMPTY", "")
    result = resolve_env_refs({"env": "CORLINMAN_EMPTY", "default": "fallback"})
    assert result == ""  # empty string beats the default


# ---------------------------------------------------------------------------
# resolve_env_refs — multi-key table is NOT an env-ref
# ---------------------------------------------------------------------------


def test_multi_key_table_with_env_key_is_not_resolved() -> None:
    """A scheduler-job table ``{env = {KEY="val"}}`` must pass through intact."""
    value = {"env": {"KEY": "val"}, "other": "stuff"}
    result = resolve_env_refs(value)
    # 3 keys → not an env-ref; passes through as a dict
    assert result == value


def test_env_key_with_extra_keys_not_resolved() -> None:
    """``{env="X", default="Y", extra="z"}`` → not an env-ref (extra key)."""
    value = {"env": "SOME_VAR", "default": "d", "extra": "z"}
    result = resolve_env_refs(value)
    assert result == value


# ---------------------------------------------------------------------------
# resolve_env_refs — nested / recursive
# ---------------------------------------------------------------------------


def test_resolve_env_refs_nested_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NESTED_KEY", "nested-value")
    data = {
        "outer": {
            "inner": {"env": "NESTED_KEY"},
            "static": 42,
        }
    }
    result = resolve_env_refs(data)
    assert result["outer"]["inner"] == "nested-value"
    assert result["outer"]["static"] == 42


def test_resolve_env_refs_list_of_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIST_VAR", "list-value")
    data = [{"env": "LIST_VAR"}, "plain", 1]
    result = resolve_env_refs(data)
    assert result == ["list-value", "plain", 1]


def test_resolve_env_refs_does_not_mutate_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve_env_refs`` returns a fresh structure; input is untouched."""
    monkeypatch.setenv("CORLINMAN_MUT_TEST", "x")
    original = {"providers": {"openai": {"env": "CORLINMAN_MUT_TEST"}}}
    result = resolve_env_refs(original)
    assert original["providers"]["openai"] == {"env": "CORLINMAN_MUT_TEST"}
    assert result["providers"]["openai"] == "x"


# ---------------------------------------------------------------------------
# Integration: load_from_path resolves env-refs end-to-end
# ---------------------------------------------------------------------------


def test_load_resolves_provider_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    monkeypatch.delenv("PROXY_KEY", raising=False)
    monkeypatch.setenv("QQ_WS_URL", "ws://napcat:3001")
    p = _write(tmp_path, _FULL_TOML)
    cfg = load_from_path(p)

    assert cfg["providers"]["openai"]["api_key"] == "sk-live"
    # PROXY_KEY unset → falls to default "no-auth"
    assert cfg["providers"]["proxy"]["api_key"] == "no-auth"
    # static fields pass through
    assert cfg["providers"]["proxy"]["base_url"] == "http://localhost:9999/v1"
    assert cfg["models"]["default"] == "gpt-4o-mini"
    # channel ws_url resolved from env
    assert cfg["channels"]["qq"]["ws_url"] == "ws://napcat:3001"


def test_load_unset_env_ref_collapses_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PROXY_KEY", raising=False)
    monkeypatch.delenv("QQ_WS_URL", raising=False)
    p = _write(tmp_path, _FULL_TOML)
    cfg = load_from_path(p)

    assert cfg["providers"]["openai"]["api_key"] is None
    assert cfg["providers"]["proxy"]["api_key"] == "no-auth"  # has default
    assert cfg["channels"]["qq"]["ws_url"] == "ws://localhost:3001"  # has default
