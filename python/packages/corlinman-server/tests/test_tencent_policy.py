from __future__ import annotations

import json

from corlinman_server.tencent_policy import ReloadingTencentPolicyResolver


def test_resolver_is_fail_closed_for_missing_and_malformed_files(tmp_path) -> None:
    path = tmp_path / "py-config.json"
    resolver = ReloadingTencentPolicyResolver(str(path))
    assert resolver() is True

    path.write_text("not-json", encoding="utf-8")
    assert resolver() is True


def test_resolver_accepts_only_explicit_boolean_false(tmp_path) -> None:
    path = tmp_path / "py-config.json"
    resolver = ReloadingTencentPolicyResolver(str(path))

    path.write_text(json.dumps({"tencent_safety": {"enabled": False}}), encoding="utf-8")
    assert resolver() is False

    path.write_text(json.dumps({"tencent_safety": {"enabled": "false"}}), encoding="utf-8")
    assert resolver() is True


def test_resolver_enables_for_valid_snapshots_without_a_policy_section(tmp_path) -> None:
    path = tmp_path / "py-config.json"
    resolver = ReloadingTencentPolicyResolver(str(path))

    for payload in ({}, [], {"tencent_safety": None}):
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert resolver() is True


def test_resolver_does_not_keep_stale_disabled_value_after_delete(tmp_path) -> None:
    path = tmp_path / "py-config.json"
    path.write_text(json.dumps({"tencent_safety": {"enabled": False}}), encoding="utf-8")
    resolver = ReloadingTencentPolicyResolver(str(path))
    assert resolver() is False

    path.unlink()
    assert resolver() is True
