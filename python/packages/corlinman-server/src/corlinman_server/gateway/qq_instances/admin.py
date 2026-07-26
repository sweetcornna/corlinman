"""Shared admin service for the canonical QQ instance fleet."""

from __future__ import annotations

import fcntl
import inspect
import json
import os
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from corlinman_server.gateway.qq_instances.config import (
    materialize_qq_fleet,
    normalize_qq_fleet,
)
from corlinman_server.gateway.qq_instances.models import parse_instance_id

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "napcat_access_token",
        "token",
        "secret",
        "password",
        "api_key",
    }
)
_ENDPOINT_KEYS = frozenset({"ws_url", "napcat_url"})
_RETAINED_FILE = "qq-instances-retained.json"


_PUBLIC_DETAIL_KEYS: dict[str, frozenset[str]] = {
    "identity_mismatch": frozenset({"expected_uin"}),
    "instance_exists": frozenset({"instance_id"}),
    "qq_instance_not_found": frozenset({"instance_id"}),
    "revision_conflict": frozenset({"current_revision"}),
}


class QqAdminError(RuntimeError):
    """Typed failure translated to a stable HTTP envelope by route modules."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def public_detail(self) -> dict[str, Any]:
        """Return the stable wire envelope with code-specific safe details."""
        allowed = _PUBLIC_DETAIL_KEYS.get(self.code, frozenset())
        return {
            "error": self.code,
            "message": self.message,
            **{key: value for key, value in self.details.items() if key in allowed},
        }


@dataclass(frozen=True, slots=True)
class QqInstanceSnapshot:
    instance_id: str
    display_name: str
    enabled: bool
    connection_mode: str
    expected_uin: int | None
    is_default: bool
    revision: str
    config: dict[str, Any]
    secrets: dict[str, dict[str, Any]]
    runtime: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "connection_mode": self.connection_mode,
            "expected_uin": self.expected_uin,
            "is_default": self.is_default,
            "revision": self.revision,
            "config": deepcopy(self.config),
            "secrets": deepcopy(self.secrets),
            "runtime": deepcopy(self.runtime),
        }


_RETAINED_PHASES = frozenset({"deleting", "retained", "restoring", "purging"})
_CONNECTION_MODES = frozenset({"managed", "external"})


@dataclass(frozen=True, slots=True)
class RetainedQqInstance:
    """Durable lifecycle tombstone for one removed QQ instance."""

    raw_config: dict[str, Any]
    connection_mode: str
    manager_generation: int | None
    phase: str = "retained"

    def __post_init__(self) -> None:
        if self.connection_mode not in _CONNECTION_MODES:
            raise ValueError("invalid retained QQ connection mode")
        if self.phase not in _RETAINED_PHASES:
            raise ValueError("invalid retained QQ lifecycle phase")
        if self.manager_generation is not None and self.manager_generation <= 0:
            raise ValueError("invalid retained QQ manager generation")
        if self.connection_mode == "external" and self.manager_generation is not None:
            raise ValueError("external QQ instances cannot own a manager generation")

    def with_phase(self, phase: str) -> RetainedQqInstance:
        return RetainedQqInstance(
            raw_config=deepcopy(self.raw_config),
            connection_mode=self.connection_mode,
            manager_generation=self.manager_generation,
            phase=phase,
        )

    def with_generation(self, generation: int) -> RetainedQqInstance:
        return RetainedQqInstance(
            raw_config=deepcopy(self.raw_config),
            connection_mode=self.connection_mode,
            manager_generation=generation,
            phase=self.phase,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "raw_config": deepcopy(self.raw_config),
            "connection_mode": self.connection_mode,
            "manager_generation": self.manager_generation,
            "phase": self.phase,
        }


class RetainedQqInstanceStore:
    """Process-safe 0600 lifecycle tombstones for removed QQ instances."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / _RETAINED_FILE
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")

    def list(self) -> dict[str, RetainedQqInstance]:
        with self._locked():
            return self._load_unlocked()

    def get(self, instance_id: str) -> RetainedQqInstance | None:
        instance_id = str(parse_instance_id(instance_id))
        with self._locked():
            return self._load_unlocked().get(instance_id)

    def put(self, instance_id: str, retained: RetainedQqInstance) -> None:
        instance_id = str(parse_instance_id(instance_id))
        with self._locked():
            rows = self._load_unlocked()
            rows[instance_id] = retained
            self._flush_unlocked(rows)

    def delete(self, instance_id: str) -> None:
        instance_id = str(parse_instance_id(instance_id))
        with self._locked():
            rows = self._load_unlocked()
            rows.pop(instance_id, None)
            self._flush_unlocked(rows)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load_unlocked(self) -> dict[str, RetainedQqInstance]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise QqAdminError(
                500,
                "retained_state_unavailable",
                "retained QQ instance state is unavailable",
            ) from exc
        if not isinstance(payload, dict):
            raise QqAdminError(
                500,
                "retained_state_invalid",
                "retained QQ instance state is invalid",
            )
        version = payload.get("version", 1)
        raw_rows = payload.get("instances")
        if version not in (1, 2) or not isinstance(raw_rows, dict):
            raise QqAdminError(
                500,
                "retained_state_invalid",
                "retained QQ instance state is invalid",
            )
        rows: dict[str, RetainedQqInstance] = {}
        try:
            for raw_id, raw_row in raw_rows.items():
                instance_id = str(parse_instance_id(str(raw_id)))
                if not isinstance(raw_row, dict):
                    raise ValueError("retained row must be an object")
                if version == 1:
                    row_config = deepcopy(raw_row)
                    connection_mode = str(row_config.get("connection_mode", "external"))
                    row = RetainedQqInstance(
                        raw_config=row_config,
                        connection_mode=connection_mode,
                        manager_generation=None,
                    )
                else:
                    raw_config = raw_row.get("raw_config")
                    if not isinstance(raw_config, dict):
                        raise ValueError("retained raw_config must be an object")
                    generation = raw_row.get("manager_generation")
                    if generation is not None and (
                        isinstance(generation, bool) or not isinstance(generation, int)
                    ):
                        raise ValueError("invalid manager generation")
                    row = RetainedQqInstance(
                        raw_config=deepcopy(raw_config),
                        connection_mode=str(raw_row.get("connection_mode", "")),
                        manager_generation=generation,
                        phase=str(raw_row.get("phase", "")),
                    )
                rows[instance_id] = row
        except (TypeError, ValueError) as exc:
            raise QqAdminError(
                500,
                "retained_state_invalid",
                "retained QQ instance state is invalid",
            ) from exc
        return rows

    def _flush_unlocked(self, rows: Mapping[str, RetainedQqInstance]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            try:
                self.path.unlink()
            except FileNotFoundError:
                return
            self._fsync_parent()
            return
        payload = json.dumps(
            {
                "version": 2,
                "instances": {
                    key: value.to_json() for key, value in sorted(rows.items())
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            self._fsync_parent()
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _fsync_parent(self) -> None:
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class QqInstanceAdminService:
    """Copy-on-write fleet operations over the production channels writer."""

    def __init__(self, state: Any) -> None:
        self.state = state

    def list_instances(self) -> tuple[list[QqInstanceSnapshot], str, tuple[str, ...]]:
        channels = self._channels()
        fleet = self._normalise(channels)
        raw_instances = self._raw_instances()
        snapshots = [
            self._snapshot(str(instance_id), item.values, fleet, raw_instances)
            for instance_id, item in fleet.instances.items()
        ]
        return snapshots, fleet.revision, fleet.warnings

    def get_instance(self, instance_id: str | None = None) -> QqInstanceSnapshot:
        channels = self._channels()
        fleet = self._normalise(channels)
        resolved_id = self.resolve_instance_id(instance_id, fleet=fleet)
        item = fleet.get(resolved_id)
        if item is None:  # pragma: no cover - resolve checks it
            raise self._not_found(resolved_id)
        return self._snapshot(
            resolved_id,
            item.values,
            fleet,
            self._raw_instances(),
        )

    def resolved_instance_config(self, instance_id: str | None = None) -> dict[str, Any]:
        fleet = self._normalise(self._channels())
        resolved_id = self.resolve_instance_id(instance_id, fleet=fleet)
        item = self._require_instance(fleet, resolved_id)
        return deepcopy(dict(item.values))

    def resolve_instance_id(self, instance_id: str | None, *, fleet: Any = None) -> str:
        fleet = fleet or self._normalise(self._channels())
        if instance_id not in (None, ""):
            try:
                resolved = str(parse_instance_id(str(instance_id)))
            except (TypeError, ValueError) as exc:
                raise QqAdminError(
                    422,
                    "invalid_instance_id",
                    "QQ instance id is invalid",
                ) from exc
        else:
            if fleet.default_instance is None:
                raise QqAdminError(
                    404,
                    "default_instance_not_configured",
                    "the QQ fleet has no default instance",
                )
            resolved = str(fleet.default_instance)
        if fleet.get(resolved) is None:
            raise self._not_found(resolved)
        return resolved

    async def create_instance(
        self,
        instance_id: str,
        *,
        display_name: str | None = None,
        enabled: bool = False,
        expected_revision: str | None = None,
    ) -> QqInstanceSnapshot:
        try:
            instance_id = str(parse_instance_id(instance_id))
        except (TypeError, ValueError) as exc:
            raise QqAdminError(
                422,
                "invalid_instance_id",
                "QQ instance id is invalid",
            ) from exc
        display_name = (display_name or instance_id).strip()
        if not display_name:
            raise QqAdminError(422, "invalid_display_name", "display_name must not be empty")

        def _edit(channels: dict[str, Any], fleet: Any) -> str:
            if fleet.get(instance_id) is not None:
                raise QqAdminError(
                    409,
                    "instance_exists",
                    f"QQ instance {instance_id!r} already exists",
                    instance_id=instance_id,
                )
            qq = channels["qq"]
            instances = qq["instances"]
            instances[instance_id] = {
                "display_name": display_name,
                "enabled": bool(enabled),
                "connection_mode": "managed",
            }
            if not qq.get("default_instance"):
                qq["default_instance"] = instance_id
            return instance_id

        await self._mutate(_edit, expected_revision=expected_revision)
        return self.get_instance(instance_id)

    async def patch_instance(
        self,
        instance_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
        expected_revision: str | None = None,
    ) -> QqInstanceSnapshot:
        instance_id = self._validated_id(instance_id)
        if display_name is not None and not display_name.strip():
            raise QqAdminError(422, "invalid_display_name", "display_name must not be empty")

        def _edit(channels: dict[str, Any], fleet: Any) -> str:
            self._require_instance(fleet, instance_id)
            values = channels["qq"]["instances"][instance_id]
            if display_name is not None:
                values["display_name"] = display_name.strip()
            if enabled is not None:
                values["enabled"] = bool(enabled)
            return instance_id

        await self._mutate(_edit, expected_revision=expected_revision)
        return self.get_instance(instance_id)

    async def set_default(
        self,
        instance_id: str,
        *,
        expected_revision: str | None = None,
    ) -> QqInstanceSnapshot:
        instance_id = self._validated_id(instance_id)

        def _edit(channels: dict[str, Any], fleet: Any) -> str:
            self._require_instance(fleet, instance_id)
            channels["qq"]["default_instance"] = instance_id
            return instance_id

        await self._mutate(_edit, expected_revision=expected_revision)
        return self.get_instance(instance_id)

    async def update_instance_config(
        self,
        instance_id: str,
        updater: Callable[[dict[str, Any]], Any],
        *,
        expected_revision: str | None = None,
    ) -> tuple[QqInstanceSnapshot, Any]:
        instance_id = self._validated_id(instance_id)

        def _edit(channels: dict[str, Any], fleet: Any) -> Any:
            self._require_instance(fleet, instance_id)
            values = channels["qq"]["instances"][instance_id]
            return updater(values)

        result = await self._mutate(_edit, expected_revision=expected_revision)
        return self.get_instance(instance_id), result

    def validate_instance_removal(
        self,
        instance_id: str,
        *,
        new_default: str | None = None,
        expected_revision: str | None = None,
        require_managed: bool = False,
    ) -> QqInstanceSnapshot:
        instance_id = self._validated_id(instance_id)
        if new_default is not None:
            new_default = self._validated_id(new_default)
        fleet = self._normalise(self._channels())
        item = self._require_instance(fleet, instance_id)
        revision = _normalise_etag(expected_revision)
        if revision is not None and revision != fleet.revision:
            raise QqAdminError(
                409,
                "revision_conflict",
                "the QQ fleet changed since it was loaded",
                expected=revision,
                actual=fleet.revision,
            )
        self._validate_removal_target(
            fleet,
            item,
            instance_id=instance_id,
            new_default=new_default,
            require_managed=require_managed,
        )
        return self.get_instance(instance_id)

    async def remove_instance_config(
        self,
        instance_id: str,
        *,
        new_default: str | None = None,
        expected_revision: str | None = None,
        require_managed: bool = False,
    ) -> dict[str, Any]:
        instance_id = self._validated_id(instance_id)
        if new_default is not None:
            new_default = self._validated_id(new_default)

        def _edit(channels: dict[str, Any], fleet: Any) -> dict[str, Any]:
            item = self._require_instance(fleet, instance_id)
            remaining = self._validate_removal_target(
                fleet,
                item,
                instance_id=instance_id,
                new_default=new_default,
                require_managed=require_managed,
            )
            raw_config = self._raw_instance_config(instance_id)
            qq = channels["qq"]
            qq["instances"].pop(instance_id)
            if remaining:
                if fleet.default_instance == item.instance_id:
                    qq["default_instance"] = new_default
            else:
                qq.pop("default_instance", None)
            return {
                "raw_config": raw_config,
                "resolved_config": deepcopy(dict(item.values)),
            }

        result = await self._mutate(_edit, expected_revision=expected_revision)
        if not isinstance(result, dict):  # pragma: no cover - writer contract guard
            raise QqAdminError(
                500,
                "qq_config_write_failed",
                "channels writer returned an invalid deletion result",
            )
        return result

    @staticmethod
    def _validate_removal_target(
        fleet: Any,
        item: Any,
        *,
        instance_id: str,
        new_default: str | None,
        require_managed: bool,
    ) -> list[str]:
        if require_managed and item.connection_mode != "managed":
            raise QqAdminError(
                409,
                "external_instance_not_retained",
                "external QQ instances cannot retain manager-owned login state",
            )
        remaining = [str(key) for key in fleet.instances if str(key) != instance_id]
        if fleet.default_instance == item.instance_id and remaining:
            if new_default is None:
                raise QqAdminError(
                    409,
                    "new_default_required",
                    "deleting the default QQ instance requires new_default",
                    candidates=remaining,
                )
            if new_default not in remaining:
                raise QqAdminError(
                    422,
                    "invalid_new_default",
                    "new_default must name a remaining QQ instance",
                )
        elif new_default is not None and new_default not in remaining:
            raise QqAdminError(
                422,
                "invalid_new_default",
                "new_default must name a remaining QQ instance",
            )
        return remaining

    async def restore_instance_config(
        self,
        instance_id: str,
        config: Mapping[str, Any],
        *,
        make_default: bool | None = None,
        expected_revision: str | None = None,
    ) -> QqInstanceSnapshot:
        instance_id = self._validated_id(instance_id)
        raw_config = deepcopy(dict(config))

        def _edit(channels: dict[str, Any], fleet: Any) -> str:
            if fleet.get(instance_id) is not None:
                raise QqAdminError(
                    409,
                    "instance_exists",
                    f"QQ instance {instance_id!r} already exists",
                )
            qq = channels["qq"]
            qq["instances"][instance_id] = deepcopy(raw_config)
            if make_default is True or not qq.get("default_instance"):
                qq["default_instance"] = instance_id
            return instance_id

        await self._mutate(_edit, expected_revision=expected_revision)
        return self.get_instance(instance_id)

    def raw_instance_config(self, instance_id: str) -> dict[str, Any]:
        """Return the persistence shape, including unresolved secret references."""
        instance_id = self._validated_id(instance_id)
        if self._normalise(self._channels()).get(instance_id) is None:
            raise self._not_found(instance_id)
        return self._raw_instance_config(instance_id)

    def retained_store(self) -> RetainedQqInstanceStore:
        data_dir = getattr(self.state, "data_dir", None)
        if data_dir is None:
            raise QqAdminError(
                503,
                "data_dir_missing",
                "gateway booted without a writable data directory",
            )
        return RetainedQqInstanceStore(Path(data_dir))

    async def _mutate(
        self,
        callback: Callable[[dict[str, Any], Any], Any],
        *,
        expected_revision: str | None,
    ) -> Any:
        writer = getattr(self.state, "channels_writer", None)
        if writer is None:
            raise QqAdminError(
                503,
                "channels_writer_missing",
                "gateway booted without a writable channels config",
            )
        expected_revision = _normalise_etag(expected_revision)

        def _apply(current: dict[str, Any], _raw: dict[str, Any]) -> tuple[dict[str, Any], Any]:
            canonical = materialize_qq_fleet(current)
            fleet = self._normalise(canonical)
            if expected_revision is not None and expected_revision != fleet.revision:
                raise QqAdminError(
                    409,
                    "revision_conflict",
                    "the QQ fleet changed since it was loaded",
                    expected=expected_revision,
                    actual=fleet.revision,
                )
            result = callback(canonical, fleet)
            self._normalise(canonical)
            return canonical, result

        mutate = getattr(writer, "mutate", None)
        try:
            if callable(mutate):
                result = mutate(_apply)
                return await result if inspect.isawaitable(result) else result
            current = deepcopy(self._channels())
            candidate, result = _apply(current, self._raw_channels())
            persisted = writer(candidate)
            if inspect.isawaitable(persisted):
                await persisted
            return result
        except QqAdminError:
            raise
        except Exception as exc:
            raise QqAdminError(
                500,
                "qq_config_write_failed",
                "failed to persist the QQ configuration",
            ) from exc

    def _snapshot(
        self,
        instance_id: str,
        values: Mapping[str, Any],
        fleet: Any,
        raw_instances: Mapping[str, Mapping[str, Any]],
    ) -> QqInstanceSnapshot:
        raw = raw_instances.get(instance_id, {})
        runtime = None
        registry = getattr(self.state, "qq_runtime_registry", None)
        if registry is not None:
            runtime = registry.health(instance_id)
        return QqInstanceSnapshot(
            instance_id=instance_id,
            display_name=str(values.get("display_name") or instance_id),
            enabled=bool(values.get("enabled", False)),
            connection_mode=str(values.get("connection_mode") or "external"),
            expected_uin=_optional_positive_int(values.get("expected_uin")),
            is_default=(str(fleet.default_instance) == instance_id),
            revision=fleet.revision,
            config=_redact_config(values),
            secrets={
                key: _secret_status(
                    raw.get(key),
                    values.get(key),
                    managed=str(values.get("connection_mode") or "external") == "managed",
                )
                for key in sorted(_SECRET_KEYS & (set(raw) | set(values)))
            },
            runtime=runtime,
        )

    def _channels(self) -> dict[str, Any]:
        channels = getattr(self.state, "channels_config", None)
        return deepcopy(channels) if isinstance(channels, dict) else {}

    def _raw_channels(self) -> dict[str, Any]:
        path = getattr(self.state, "config_path", None)
        if path is None:
            return self._channels()
        try:
            config = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return self._channels()
        channels = config.get("channels")
        return deepcopy(channels) if isinstance(channels, dict) else {}

    def _raw_instances(self) -> dict[str, dict[str, Any]]:
        try:
            canonical = materialize_qq_fleet(self._raw_channels())
        except ValueError:
            return {}
        qq = canonical.get("qq")
        instances = qq.get("instances") if isinstance(qq, dict) else None
        if not isinstance(instances, dict):
            return {}
        return {
            str(key): deepcopy(value)
            for key, value in instances.items()
            if isinstance(value, dict)
        }

    def _raw_instance_config(self, instance_id: str) -> dict[str, Any]:
        raw = self._raw_instances().get(instance_id)
        if raw is not None:
            return deepcopy(raw)
        item = self._normalise(self._channels()).get(instance_id)
        return deepcopy(dict(item.values)) if item is not None else {}

    @staticmethod
    def _normalise(channels: Mapping[str, Any]) -> Any:
        try:
            return normalize_qq_fleet(channels)
        except (TypeError, ValueError) as exc:
            raise QqAdminError(
                500,
                "invalid_qq_fleet",
                "QQ fleet configuration is invalid",
            ) from exc

    @staticmethod
    def _validated_id(instance_id: str) -> str:
        try:
            return str(parse_instance_id(instance_id))
        except (TypeError, ValueError) as exc:
            raise QqAdminError(
                422,
                "invalid_instance_id",
                "QQ instance id is invalid",
            ) from exc

    @staticmethod
    def _require_instance(fleet: Any, instance_id: str) -> Any:
        item = fleet.get(instance_id)
        if item is None:
            raise QqInstanceAdminService._not_found(instance_id)
        return item

    @staticmethod
    def _not_found(instance_id: str) -> QqAdminError:
        return QqAdminError(
            404,
            "qq_instance_not_found",
            "QQ instance does not exist",
            instance_id=instance_id,
        )


def _normalise_etag(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    return value.strip('"') or None


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _secret_status(raw: Any, resolved: Any, *, managed: bool) -> dict[str, Any]:
    if managed and raw in (None, ""):
        return {"is_set": True, "source": "managed"}
    if isinstance(raw, Mapping) and "env" in raw:
        return {"is_set": resolved not in (None, ""), "source": "env"}
    return {"is_set": resolved not in (None, ""), "source": "literal"}


def _redact_config(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in values.items():
        key = str(raw_key)
        if _is_secret_key(key):
            continue
        if key in _ENDPOINT_KEYS:
            safe = _safe_endpoint(value)
            if safe is not None:
                out[key] = safe
            continue
        if isinstance(value, Mapping):
            out[key] = _redact_config(value)
        elif isinstance(value, list):
            out[key] = [
                _redact_config(item) if isinstance(item, Mapping) else deepcopy(item)
                for item in value
            ]
        else:
            out[key] = deepcopy(value)
    return out


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SECRET_KEYS or any(
        marker in lowered for marker in ("token", "secret", "password", "api_key")
    )


def _safe_endpoint(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if not parsed.scheme or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))
    except ValueError:
        return None


__all__ = [
    "QqAdminError",
    "QqInstanceAdminService",
    "QqInstanceSnapshot",
    "RetainedQqInstanceStore",
]
