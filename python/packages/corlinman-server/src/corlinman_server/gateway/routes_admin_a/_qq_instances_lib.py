"""Wire models and helpers for the canonical QQ instance admin routes."""

from __future__ import annotations

import re
import time
from typing import Any, Literal, NoReturn

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

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


_MONITOR_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _clean_qq_id_list(values: list[str], label: str) -> list[str]:
    cleaned = [str(u).strip() for u in values if str(u).strip()]
    if any(not u.isdigit() for u in cleaned):
        raise ValueError(f"{label} must be QQ numbers")
    return list(dict.fromkeys(cleaned))


class QqMonitorSource(BaseModel):
    """One monitored group inside a monitor task."""

    group: str
    watch_user_ids: list[str] = Field(default_factory=list, max_length=200)
    """Collection filter; empty = everyone in the group."""
    focus_user_ids: list[str] = Field(default_factory=list, max_length=200)
    """Members the digest covers in extra detail; always collected even
    when ``watch_user_ids`` narrows the scope."""

    @model_validator(mode="after")
    def _validate(self) -> QqMonitorSource:
        if not self.group.strip().isdigit():
            raise ValueError("source group must be a QQ group number")
        object.__setattr__(
            self, "watch_user_ids", _clean_qq_id_list(self.watch_user_ids, "watch_user_ids")
        )
        object.__setattr__(
            self, "focus_user_ids", _clean_qq_id_list(self.focus_user_ids, "focus_user_ids")
        )
        return self


class QqMonitorSpec(BaseModel):
    """One monitor task (mirrors the runtime parser in
    ``corlinman_channels.service._qq_monitor_parse_entry`` — keep the
    two validation surfaces in sync). A task aggregates 1..N source
    groups into a single combined report on one schedule."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    enabled: bool = True
    sources: list[QqMonitorSource] = Field(min_length=1, max_length=20)
    schedule_type: Literal["daily", "interval"]
    daily_time: str | None = None
    interval_minutes: int | None = None
    timezone: str = ""
    window_minutes: int = Field(default=0, ge=0, le=7 * 1440)
    target_type: Literal["group", "user"]
    target_id: str
    style_extra: str = Field(default="", max_length=2000)
    send_when_empty: bool = False

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_shape(cls, data: Any) -> Any:
        """#190 stored a flat single-group shape (``source_group`` +
        ``watch_user_ids``); lift it into ``sources`` so hand-written
        rows and pre-upgrade configs keep validating."""
        if (
            isinstance(data, dict)
            and not data.get("sources")
            and data.get("source_group")
        ):
            data = dict(data)
            data["sources"] = [
                {
                    "group": data.pop("source_group"),
                    "watch_user_ids": data.pop("watch_user_ids", []) or [],
                    "focus_user_ids": data.pop("focus_user_ids", []) or [],
                }
            ]
        return data

    @model_validator(mode="after")
    def _validate(self) -> QqMonitorSpec:
        if not self.target_id.strip().isdigit():
            raise ValueError("target_id must be a QQ number")
        groups = [source.group for source in self.sources]
        if len(groups) != len(set(groups)):
            raise ValueError("source groups must be unique within a task")
        if self.schedule_type == "daily":
            if not self.daily_time or not _MONITOR_HHMM_RE.match(self.daily_time.strip()):
                raise ValueError("daily schedule requires daily_time as HH:MM")
        elif not self.interval_minutes or self.interval_minutes < 5:
            raise ValueError("interval schedule requires interval_minutes >= 5")
        if self.timezone.strip():
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(self.timezone.strip())
            except Exception as exc:  # noqa: BLE001 — surface as 422
                raise ValueError(f"unknown timezone: {self.timezone}") from exc
        return self

    def effective_window_minutes(self) -> int:
        if self.window_minutes > 0:
            return self.window_minutes
        if self.schedule_type == "daily":
            return 1440
        return int(self.interval_minutes or 0)


class MonitorsBody(BaseModel):
    monitors: list[QqMonitorSpec] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _unique_ids(self) -> MonitorsBody:
        ids = [m.id for m in self.monitors]
        if len(ids) != len(set(ids)):
            raise ValueError("monitor ids must be unique")
        return self


class MonitorsOut(BaseModel):
    monitors: list[QqMonitorSpec] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    revision: str


class MonitorTriggerOut(BaseModel):
    status: str = "triggered"


class MonitorsStatusOut(BaseModel):
    """Live digest-loop status + captured-message counts per monitor id."""

    statuses: dict[str, dict[str, Any]] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)


def parse_monitor_entries(raw: Any) -> tuple[list[QqMonitorSpec], list[str]]:
    """Config rows → validated specs; invalid rows become warnings.

    Hand-edited TOML can contain entries the wire model rejects — GET
    must still answer (the runtime skips those rows the same way)."""
    if not isinstance(raw, list):
        return [], []
    monitors: list[QqMonitorSpec] = []
    warnings: list[str] = []
    for index, entry in enumerate(raw):
        try:
            monitors.append(QqMonitorSpec.model_validate(entry))
        except ValidationError as exc:
            errors = exc.errors()
            message = (
                str(errors[0].get("msg", "validation error"))
                if errors
                else "validation error"
            )
            warnings.append(f"monitors[{index}] invalid: {message}")
    return monitors, warnings


async def monitor_window_counts(
    instance_id: str, monitors: list[QqMonitorSpec]
) -> dict[str, int]:
    """Captured-message count inside each enabled monitor's window.

    Reuses the channel package's module-cached history handle (one
    aiosqlite connection per process); an unavailable store degrades to
    an empty dict — the UI then simply hides the counters."""
    try:
        from corlinman_channels.service import _try_open_group_history
    except Exception:  # noqa: BLE001 — channels package absent in some tests
        return {}
    store = await _try_open_group_history()
    if store is None:
        return {}
    now_ms = int(time.time() * 1000)
    counts: dict[str, int] = {}
    for monitor in monitors:
        if not monitor.enabled:
            continue
        try:
            since_ms = now_ms - monitor.effective_window_minutes() * 60_000
            total = 0
            for source in monitor.sources:
                collected = list(
                    dict.fromkeys((*source.watch_user_ids, *source.focus_user_ids))
                )
                total += await store.count_window(
                    instance_id=instance_id,
                    group_id=source.group,
                    since_ms=since_ms,
                    sender_ids=(collected if source.watch_user_ids else None),
                )
            counts[monitor.id] = total
        except Exception:  # noqa: BLE001 — a broken store must not 500 status
            continue
    return counts


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
    "MonitorTriggerOut",
    "MonitorsBody",
    "MonitorsOut",
    "MonitorsStatusOut",
    "PatchQqInstanceBody",
    "PurgeBody",
    "QqInstanceOut",
    "QqInstancesOut",
    "QqMonitorSource",
    "QqMonitorSpec",
    "ReconnectOut",
    "RestoreBody",
    "apply_config",
    "monitor_window_counts",
    "parse_monitor_entries",
    "qq_admin_service",
    "raise_http",
    "validate_keywords",
]
