"""Wire-safe models for the privileged managed-NapCat boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

NapCatProviderKind = Literal["docker", "native"]
_DOCKER_METADATA_KEYS: frozenset[str] = frozenset(
    {"app_volume", "qq_volume", "image", "network"}
)
_NATIVE_METADATA_KEYS: frozenset[str] = frozenset()
NapCatOperation = Literal[
    "provision",
    "adopt",
    "inspect",
    "start",
    "stop",
    "restart",
    "upgrade",
    "remove_runtime",
    "restore",
    "purge_login_state",
    "bind_uin",
]
NapCatRuntimeState = Literal[
    "absent",
    "created",
    "running",
    "stopped",
    "retained",
    "error",
]


@dataclass(frozen=True, slots=True)
class NapCatDescriptor:
    """Private connection descriptor returned only over the manager socket."""

    instance_id: str
    generation: int
    ws_url: str
    http_url: str
    access_token: str = field(repr=False)
    napcat_access_token: str = field(repr=False)
    expected_uin: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NapCatObservedState:
    instance_id: str
    provider: NapCatProviderKind
    generation: int
    state: NapCatRuntimeState
    resource_id: str | None = None
    retained: bool = False
    bound_uin: int | None = None
    error_code: str | None = None
    can_restore: bool = True
    can_purge: bool = True

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NapCatInstanceRecord:
    instance_id: str
    provider: NapCatProviderKind
    generation: int
    resource_id: str
    state_root: str
    token_file: str
    webui_port: int | None = None
    onebot_port: int | None = None
    bound_uin: int | None = None
    retained: bool = False
    legacy_resource: bool = False
    previous_image: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        _validate_metadata(self.provider, self.metadata)
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> NapCatInstanceRecord:
        provider = str(payload["provider"])
        if provider not in {"docker", "native"}:
            raise ValueError("invalid NapCat provider")
        metadata = {
            str(key): str(value)
            for key, value in dict(payload.get("metadata") or {}).items()
        }
        _validate_metadata(provider, metadata)
        return cls(
            instance_id=str(payload["instance_id"]),
            provider=provider,  # type: ignore[arg-type]
            generation=max(1, int(payload.get("generation", 1))),
            resource_id=str(payload["resource_id"]),
            state_root=str(payload["state_root"]),
            token_file=str(payload["token_file"]),
            webui_port=_optional_int(payload.get("webui_port")),
            onebot_port=_optional_int(payload.get("onebot_port")),
            bound_uin=_optional_int(payload.get("bound_uin")),
            retained=bool(payload.get("retained", False)),
            legacy_resource=bool(payload.get("legacy_resource", False)),
            previous_image=_optional_str(payload.get("previous_image")),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class ManagerRequest:
    request_id: str
    operation: NapCatOperation
    instance_id: str
    generation: int | None = None
    expected_uin: int | None = None


@dataclass(frozen=True, slots=True)
class ManagerResponse:
    ok: bool
    request_id: str
    observed: NapCatObservedState | None = None
    descriptor: NapCatDescriptor | None = None
    error_code: str | None = None
    message: str | None = None
    warning_code: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "observed": self.observed.to_json() if self.observed else None,
            "descriptor": self.descriptor.to_json() if self.descriptor else None,
            "error_code": self.error_code,
            "message": self.message,
            "warning_code": self.warning_code,
        }


def _validate_metadata(provider: str, metadata: dict[str, str]) -> None:
    allowed = _DOCKER_METADATA_KEYS if provider == "docker" else _NATIVE_METADATA_KEYS
    unknown = set(metadata) - allowed
    if unknown:
        raise ValueError(f"unsupported NapCat inventory metadata: {sorted(unknown)}")


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)
