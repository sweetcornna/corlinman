"""Stable identity and effective configuration for QQ channel instances."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NewType

QqInstanceId = NewType("QqInstanceId", str)
DEFAULT_QQ_INSTANCE_ID = QqInstanceId("default")
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class InvalidQqInstanceId(ValueError):
    """Raised when a configured QQ instance id cannot be used safely."""


def parse_instance_id(value: str) -> QqInstanceId:
    candidate = str(value).strip()
    if not _INSTANCE_ID_RE.fullmatch(candidate):
        raise InvalidQqInstanceId(
            "QQ instance id must match ^[a-z0-9][a-z0-9_-]{0,63}$"
        )
    return QqInstanceId(candidate)


@dataclass(frozen=True, slots=True)
class QqInstanceConfig:
    """One normalized QQ instance; ``values`` retains extension keys."""

    instance_id: QqInstanceId
    display_name: str
    enabled: bool
    connection_mode: str
    expected_uin: int | None
    values: Mapping[str, Any] = field(repr=False)

    @classmethod
    def from_mapping(
        cls, instance_id: str, values: Mapping[str, Any]
    ) -> QqInstanceConfig:
        parsed = parse_instance_id(instance_id)
        mode = str(values.get("connection_mode") or "external").strip().lower()
        if mode not in {"external", "managed"}:
            raise ValueError(f"unsupported QQ connection_mode: {mode!r}")
        raw_uin = values.get("expected_uin")
        expected_uin = None
        if raw_uin not in (None, ""):
            try:
                expected_uin = int(str(raw_uin))
            except ValueError as exc:
                raise ValueError("expected_uin must be a numeric QQ UIN") from exc
            if expected_uin <= 0:
                raise ValueError("expected_uin must be positive")
        copied = _copy_mapping(values)
        return cls(
            instance_id=parsed,
            display_name=str(values.get("display_name") or parsed),
            enabled=bool(values.get("enabled", False)),
            connection_mode=mode,
            expected_uin=expected_uin,
            values=MappingProxyType(copied),
        )


@dataclass(frozen=True, slots=True)
class QqFleetConfig:
    """Normalized QQ fleet with an explicit default and stable revision."""

    default_instance: QqInstanceId | None
    instances: Mapping[QqInstanceId, QqInstanceConfig]
    revision: str
    legacy: bool = False
    warnings: tuple[str, ...] = ()

    def get(self, instance_id: str) -> QqInstanceConfig | None:
        return self.instances.get(parse_instance_id(instance_id))


def fleet_revision(default_instance: str | None, instances: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"default_instance": default_instance, "instances": instances},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            out[str(key)] = _copy_mapping(item)
        elif isinstance(item, list):
            out[str(key)] = [
                _copy_mapping(v) if isinstance(v, Mapping) else v for v in item
            ]
        else:
            out[str(key)] = item
    return out
