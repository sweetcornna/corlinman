"""Both boot modes must install the same agent-facing config.

corlinman runs the agent either in its own process (``grpc_agent``, what
``deploy/install.sh`` provisions) or inside the gateway (in-process). Each
mode learns the agent-facing config blocks a different way:

* two-process — ``corlinman_server.main._apply_agent_config_from_sidecar``
  reads them out of ``py-config.json``;
* in-process — ``app_factory._apply_agent_side_config`` reads them off the
  live config dict.

If only one side is wired the divergence is invisible to normal testing:
the admin routes and the UI keep working, and the tool quietly uses a
built-in default. That is exactly how ``[voice]`` shipped broken in
v1.39.0, and how ``[models].image_*`` shipped in-process-broken in v1.40.0
— the sidecar half was wired, the in-process half was not.

These tests pin the parity itself rather than either half.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from corlinman_agent.image.defaults import get_image_defaults, reset_image_defaults
from corlinman_agent.runtime_defaults import (
    get_agent_runtime_defaults,
    reset_agent_runtime_defaults,
)
from corlinman_agent.voice import get_voice_defaults, reset_voice_defaults
from corlinman_agent.web.defaults import (
    get_web_search_defaults,
    reset_web_search_defaults,
)
from corlinman_server.gateway.lifecycle.app_factory import (
    _AGENT_CONFIG_SECTIONS,
    _apply_agent_side_config,
)
from corlinman_server.gateway.lifecycle.py_config import render_py_config
from corlinman_server.main import _apply_agent_config_from_sidecar

_CONFIG = {
    "providers": {},
    "models": {
        "aliases": {},
        "image_provider": "img-slot",
        "image_model": "gpt-image-2",
    },
    "voice": {"backend": "gemini", "voice": "Kore"},
    "web_search": {"backend": "serpapi", "api_key": "k-1"},
    "agent_runtime": {
        "max_rounds": 24,
        "enable_execute_code": True,
        "sandbox_backend": "docker",
    },
}


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    def _reset() -> None:
        reset_voice_defaults()
        reset_image_defaults()
        reset_web_search_defaults()
        reset_agent_runtime_defaults()

    _reset()
    yield
    _reset()


def _installed() -> dict[str, object]:
    """The agent-visible state each path is supposed to produce."""
    runtime = get_agent_runtime_defaults()
    return {
        "voice_backend": get_voice_defaults().backend,
        "voice": get_voice_defaults().voice,
        "image_provider": get_image_defaults().provider,
        "image_model": get_image_defaults().model,
        "search_backend": get_web_search_defaults().backend,
        "search_key": get_web_search_defaults().api_key,
        "max_rounds": runtime.max_rounds,
        "execute_code": runtime.enable_execute_code,
        "sandbox_backend": runtime.sandbox_backend,
    }


_EXPECTED = {
    "voice_backend": "gemini",
    "voice": "Kore",
    "image_provider": "img-slot",
    "image_model": "gpt-image-2",
    "search_backend": "serpapi",
    "search_key": "k-1",
    "max_rounds": 24,
    "execute_code": True,
    "sandbox_backend": "docker",
}


def test_in_process_boot_installs_every_block() -> None:
    _apply_agent_side_config(_CONFIG)
    assert _installed() == _EXPECTED


def test_sidecar_boot_installs_every_block(tmp_path: Path) -> None:
    sidecar = tmp_path / "py-config.json"
    sidecar.write_text(json.dumps(render_py_config(_CONFIG)), encoding="utf-8")

    _apply_agent_config_from_sidecar(str(sidecar))

    assert _installed() == _EXPECTED


def test_the_two_paths_agree(tmp_path: Path) -> None:
    """The actual invariant: same config in, same agent state out."""
    _apply_agent_side_config(_CONFIG)
    in_process = _installed()

    reset_voice_defaults()
    reset_image_defaults()
    reset_web_search_defaults()
    reset_agent_runtime_defaults()

    sidecar = tmp_path / "py-config.json"
    sidecar.write_text(json.dumps(render_py_config(_CONFIG)), encoding="utf-8")
    _apply_agent_config_from_sidecar(str(sidecar))

    assert _installed() == in_process


def test_every_section_the_hook_reads_is_a_hot_reload_trigger() -> None:
    """``_config_swap_fn`` re-runs the hook only for sections in this set;
    a block read by the hook but missing here would land in config.toml
    and stop there until the next restart."""
    assert _AGENT_CONFIG_SECTIONS == frozenset(
        {"voice", "models", "web_search", "agent_runtime"}
    )


def test_one_bad_block_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voice block that blows up must not cost you the search binding.

    Failure is injected rather than provoked with bad data: the parsers are
    all deliberately tolerant, so no realistic input raises today. What is
    being pinned is the *structure* — one ``try`` per block — which stays
    correct if a future backend registration does start raising.
    """
    import corlinman_agent.voice as voice_mod

    def _boom(_section: object) -> None:
        raise RuntimeError("custom backend exploded")

    monkeypatch.setattr(voice_mod, "apply_voice_config", _boom)

    sidecar = tmp_path / "py-config.json"
    sidecar.write_text(
        json.dumps(
            {
                "voice": {"backend": "gemini"},
                "web_search": {"backend": "serpapi", "api_key": "k-1"},
                "image": {"image_provider": "img-slot"},
            }
        ),
        encoding="utf-8",
    )

    _apply_agent_config_from_sidecar(str(sidecar))

    assert get_web_search_defaults().backend == "serpapi"
    assert get_image_defaults().provider == "img-slot"


def test_unreadable_sidecar_is_survivable(tmp_path: Path) -> None:
    _apply_agent_config_from_sidecar(str(tmp_path / "does-not-exist.json"))
    (tmp_path / "garbage.json").write_text("{not json", encoding="utf-8")
    _apply_agent_config_from_sidecar(str(tmp_path / "garbage.json"))
    assert _installed()["search_backend"] == ""


def test_malformed_blocks_do_not_raise(tmp_path: Path) -> None:
    """Both paths are best-effort: a bad block must not take the process
    down at boot."""
    broken = {
        "voice": "not-a-table",
        "models": 7,
        "web_search": [],
        "agent_runtime": "nope",
    }
    _apply_agent_side_config(broken)

    sidecar = tmp_path / "py-config.json"
    sidecar.write_text(json.dumps(broken), encoding="utf-8")
    _apply_agent_config_from_sidecar(str(sidecar))

    assert _installed()["search_backend"] == ""
