"""Normalize and materialize legacy/canonical QQ instance configuration."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any

from corlinman_server.gateway.qq_instances.models import (
    DEFAULT_QQ_INSTANCE_ID,
    QqFleetConfig,
    QqInstanceConfig,
    QqInstanceId,
    fleet_revision,
    parse_instance_id,
)

_FLEET_KEYS = frozenset({"default_instance", "instances"})


def normalize_qq_fleet(channels: Mapping[str, Any] | None) -> QqFleetConfig:
    """Return the effective fleet without mutating or rewriting its source.

    An absent ``instances`` key is the legacy singleton shape and is exposed as
    the effective ``default`` instance.  An explicitly empty instances table is
    authoritative and represents no configured accounts.
    """
    qq_raw = channels.get("qq") if isinstance(channels, Mapping) else None
    if not isinstance(qq_raw, Mapping):
        return _build_fleet(None, {}, legacy=False)

    if "instances" not in qq_raw:
        legacy_values = {
            str(key): deepcopy(value)
            for key, value in qq_raw.items()
            if key not in _FLEET_KEYS
        }
        if not legacy_values:
            return _build_fleet(None, {}, legacy=True)
        return _build_fleet(
            str(DEFAULT_QQ_INSTANCE_ID),
            {str(DEFAULT_QQ_INSTANCE_ID): legacy_values},
            legacy=True,
        )

    instances_raw = qq_raw.get("instances")
    if not isinstance(instances_raw, Mapping):
        raise ValueError("channels.qq.instances must be a table")
    instances: dict[str, dict[str, Any]] = {}
    for raw_id, raw_values in instances_raw.items():
        instance_id = str(parse_instance_id(str(raw_id)))
        if not isinstance(raw_values, Mapping):
            raise ValueError(f"channels.qq.instances.{instance_id} must be a table")
        instances[instance_id] = deepcopy(dict(raw_values))

    default_raw = qq_raw.get("default_instance")
    default_instance = str(default_raw).strip() if default_raw not in (None, "") else None
    if default_instance is not None:
        default_instance = str(parse_instance_id(default_instance))
        if default_instance not in instances:
            raise ValueError("channels.qq.default_instance does not name an instance")
    elif instances:
        raise ValueError("channels.qq.default_instance is required when instances exist")

    warnings: tuple[str, ...] = ()
    if any(key not in _FLEET_KEYS for key in qq_raw):
        warnings = (
            "legacy channels.qq fields are ignored because instances is present",
        )
    return _build_fleet(
        default_instance,
        instances,
        legacy=False,
        warnings=warnings,
    )


def materialize_qq_fleet(channels: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a writable canonical ``[channels]`` tree.

    The first account-scoped mutation can call this to move a legacy singleton
    under ``instances.default``.  Calling it repeatedly is idempotent.
    """
    result = deepcopy(dict(channels or {}))
    fleet = normalize_qq_fleet(result)
    if not fleet.legacy:
        return result

    qq = result.get("qq")
    legacy_values = deepcopy(dict(qq)) if isinstance(qq, Mapping) else {}
    result["qq"] = {
        "default_instance": str(DEFAULT_QQ_INSTANCE_ID),
        "instances": {str(DEFAULT_QQ_INSTANCE_ID): legacy_values},
    }
    return result


def _build_fleet(
    default_instance: str | None,
    raw_instances: Mapping[str, Mapping[str, Any]],
    *,
    legacy: bool,
    warnings: tuple[str, ...] = (),
) -> QqFleetConfig:
    instances: dict[QqInstanceId, QqInstanceConfig] = {}
    revision_values: dict[str, Any] = {}
    for raw_id, raw_values in raw_instances.items():
        config = QqInstanceConfig.from_mapping(raw_id, raw_values)
        instances[config.instance_id] = config
        revision_values[str(config.instance_id)] = deepcopy(dict(raw_values))
    parsed_default = parse_instance_id(default_instance) if default_instance else None
    return QqFleetConfig(
        default_instance=parsed_default,
        instances=MappingProxyType(instances),
        revision=fleet_revision(default_instance, revision_values),
        legacy=legacy,
        warnings=warnings,
    )
