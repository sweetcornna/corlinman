"""Repro for CMP-05 — strict mode misses outbound/persistent mutating tools.

The strict-mode docstring claims it "denies every mutating tool", but
``MUTATING_TOOLS`` omitted ``memory_write`` (writes persistent state),
``send_attachment`` (outbound side effect to the chat channel), and
``text_to_speech`` (outbound side effect). Before the fix these returned
``allow`` under strict mode.
"""

from __future__ import annotations

from corlinman_agent.permission import ALLOW, DENY, PermissionGate


def test_strict_mode_denies_memory_write() -> None:
    g = PermissionGate(strict=True)
    assert g.decide("memory_write") == DENY


def test_strict_mode_denies_send_attachment() -> None:
    g = PermissionGate(strict=True)
    assert g.decide("send_attachment") == DENY


def test_strict_mode_denies_text_to_speech() -> None:
    g = PermissionGate(strict=True)
    assert g.decide("text_to_speech") == DENY


def test_strict_mode_still_allows_read_only_tools() -> None:
    # Regression guard: the additions must not flip read-only tools.
    g = PermissionGate(strict=True)
    assert g.decide("read_file") == ALLOW
    assert g.decide("search_files") == ALLOW
    assert g.decide("web_search") == ALLOW
    assert g.decide("memory_search") == ALLOW
