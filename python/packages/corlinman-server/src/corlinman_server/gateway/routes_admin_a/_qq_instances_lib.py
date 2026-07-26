"""Wire models and helpers for the canonical QQ instance admin routes."""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import HTTPException
from pydantic import BaseModel, Field

from corlinman_server.gateway.qq_instances import (
    QqAdminError,
    QqInstanceAdminService,
    QqInstanceSnapshot,
)
from corlinman_server.gateway.routes_admin_a._channels_lib import (
    ChannelConfigBody,
    _apply_channel_config,
)
from corlinman_server.gateway.routes_admin_a.state import AdminState


class QqInstanceOut(BaseModel):
    instance_id: str
    display_name: str
    enabled: bool
    connection_mode: str
    expected_uin: int | None = None
    is_default: bool
    revision: str
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    runtime: dict[str, Any] | None = None

    @classmethod
    def from_snapshot(cls, snapshot: QqInstanceSnapshot) -> QqInstanceOut:
        return cls(**snapshot.to_dict())


class QqInstancesOut(BaseModel):
    instances: list[QqInstanceOut]
    retained: list[str] = Field(default_factory=list)
    revision: str
    warnings: list[str] = Field(default_factory=list)


class CreateQqInstanceBody(BaseModel):
    instance_id: str
    display_name: str | None = Field(default=None, max_length=200)
    enabled: bool = False


class PatchQqInstanceBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


class KeywordsBody(BaseModel):
    group_keywords: dict[str, list[str]] = Field(default_factory=dict)


class KeywordsOut(BaseModel):
    status: str = "ok"
    group_keywords: dict[str, list[str]] = Field(default_factory=dict)
    revision: str


class HumanlikeBody(BaseModel):
    enabled: bool
    persona_id: str | None = None


class HumanlikeOut(BaseModel):
    enabled: bool
    persona_id: str | None = None
    revision: str


class ReconnectOut(BaseModel):
    status: str
    changed: bool


class DeletionBody(BaseModel):
    new_default: str | None = None


class RestoreBody(BaseModel):
    make_default: bool | None = None


class PurgeBody(BaseModel):
    confirm_instance_id: str


class MigrationBody(BaseModel):
    target_instance_id: str


class ConfigOut(BaseModel):
    status: str = "ok"
    wrote: list[str] = Field(default_factory=list)
    instance: QqInstanceOut


def qq_admin_service(state: AdminState) -> QqInstanceAdminService:
    service = state.qq_instance_admin
    if isinstance(service, QqInstanceAdminService):
        return service
    service = QqInstanceAdminService(state)
    state.qq_instance_admin = service
    return service


def raise_http(error: QqAdminError) -> NoReturn:
    raise HTTPException(
        status_code=error.status_code,
        detail=error.public_detail(),
    ) from error


def validate_keywords(group_keywords: dict[str, list[str]]) -> dict[str, list[str]]:
    cleaned: dict[str, list[str]] = {}
    for group, keywords in group_keywords.items():
        group = str(group).strip()
        if not group:
            raise QqAdminError(422, "invalid_group", "group id must be non-empty")
        values = [str(value).strip() for value in keywords]
        if any(not value for value in values):
            raise QqAdminError(422, "invalid_keyword", "keyword must be non-empty")
        cleaned[group] = values
    return cleaned


def apply_config(values: dict[str, Any], body: ChannelConfigBody) -> list[str]:
    return _apply_channel_config("qq", values, body)


__all__ = [
    "ConfigOut",
    "CreateQqInstanceBody",
    "DeletionBody",
    "HumanlikeBody",
    "HumanlikeOut",
    "KeywordsBody",
    "KeywordsOut",
    "MigrationBody",
    "PatchQqInstanceBody",
    "PurgeBody",
    "QqInstanceOut",
    "QqInstancesOut",
    "ReconnectOut",
    "RestoreBody",
    "apply_config",
    "qq_admin_service",
    "raise_http",
    "validate_keywords",
]
