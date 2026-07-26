from __future__ import annotations

import pytest
from corlinman_server.gateway.qq_instances import (
    DEFAULT_QQ_INSTANCE_ID,
    InvalidQqInstanceId,
    materialize_qq_fleet,
    normalize_qq_fleet,
    parse_instance_id,
)


def test_legacy_singleton_is_effective_default_without_mutation() -> None:
    channels = {"qq": {"enabled": True, "group_whitelist": [123]}}

    fleet = normalize_qq_fleet(channels)

    assert fleet.legacy is True
    assert fleet.default_instance == DEFAULT_QQ_INSTANCE_ID
    assert fleet.instances[DEFAULT_QQ_INSTANCE_ID].enabled is True
    assert channels == {"qq": {"enabled": True, "group_whitelist": [123]}}


def test_explicit_empty_instances_disables_legacy_fallback() -> None:
    fleet = normalize_qq_fleet(
        {"qq": {"default_instance": "", "instances": {}, "enabled": True}}
    )

    assert fleet.legacy is False
    assert fleet.default_instance is None
    assert dict(fleet.instances) == {}
    assert fleet.warnings


def test_canonical_requires_explicit_valid_default() -> None:
    with pytest.raises(ValueError, match="default_instance is required"):
        normalize_qq_fleet({"qq": {"instances": {"bot-a": {}}}})
    with pytest.raises(ValueError, match="does not name"):
        normalize_qq_fleet(
            {
                "qq": {
                    "default_instance": "missing",
                    "instances": {"bot-a": {}},
                }
            }
        )


def test_materialize_legacy_is_idempotent() -> None:
    legacy = {"qq": {"enabled": True, "humanlike": {"persona_id": "grantley"}}}

    once = materialize_qq_fleet(legacy)
    twice = materialize_qq_fleet(once)

    assert once == twice
    assert once["qq"]["default_instance"] == "default"
    assert once["qq"]["instances"]["default"]["enabled"] is True


def test_revision_is_stable_and_changes_with_config() -> None:
    left = normalize_qq_fleet(
        {"qq": {"default_instance": "a", "instances": {"a": {"enabled": True}}}}
    )
    same = normalize_qq_fleet(
        {"qq": {"instances": {"a": {"enabled": True}}, "default_instance": "a"}}
    )
    changed = normalize_qq_fleet(
        {"qq": {"default_instance": "a", "instances": {"a": {"enabled": False}}}}
    )

    assert left.revision == same.revision
    assert left.revision != changed.revision


def test_instance_id_and_connection_mode_are_validated() -> None:
    with pytest.raises(InvalidQqInstanceId):
        parse_instance_id("Not Safe")
    with pytest.raises(ValueError, match="connection_mode"):
        normalize_qq_fleet(
            {
                "qq": {
                    "default_instance": "default",
                    "instances": {"default": {"connection_mode": "magic"}},
                }
            }
        )
