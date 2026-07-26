"""Crash-recovery journal for privileged NapCat lifecycle mutations."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from corlinman_server.gateway.qq_instances.models import parse_instance_id
from corlinman_server.system.napcat_manager.inventory import InventoryVersion
from corlinman_server.system.napcat_manager.models import NapCatInstanceRecord

_SCHEMA = 1
JournaledOperation = Literal[
    "provision",
    "adopt",
    "remove_runtime",
    "restore",
    "purge_login_state",
]
_JOURNALED_OPERATIONS = frozenset(
    {"provision", "adopt", "remove_runtime", "restore", "purge_login_state"}
)


@dataclass(frozen=True, slots=True)
class NapCatOperationIntent:
    """One exact before/after transition awaiting durable completion."""

    operation: JournaledOperation
    request_id: str
    instance_id: str
    provider: str
    generation: int
    before: InventoryVersion
    after: InventoryVersion

    def to_json(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "provider": self.provider,
            "generation": self.generation,
            "before": _version_to_json(self.before),
            "after": _version_to_json(self.after),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> NapCatOperationIntent:
        operation = str(payload["operation"])
        if operation not in _JOURNALED_OPERATIONS:
            raise ValueError("NapCat operation journal operation is invalid")
        before = _version_from_json(payload.get("before"))
        after = _version_from_json(payload.get("after"))
        intent = cls(
            operation=operation,  # type: ignore[arg-type]
            request_id=str(payload["request_id"]),
            instance_id=str(parse_instance_id(str(payload["instance_id"]))),
            provider=str(payload["provider"]),
            generation=int(payload["generation"]),
            before=before,
            after=after,
        )
        intent.validate()
        return intent

    def validate(self) -> None:
        instance_id = str(parse_instance_id(self.instance_id))
        if not self.request_id or len(self.request_id) > 128:
            raise ValueError("NapCat operation journal request id is invalid")
        if self.provider not in {"docker", "native"} or self.generation <= 0:
            raise ValueError("NapCat operation journal target is invalid")
        for version in (self.before, self.after):
            record = version.record
            if version.generation_high_water < 0:
                raise ValueError("NapCat operation journal generation is invalid")
            if record is None:
                continue
            if (
                record.instance_id != instance_id
                or record.provider != self.provider
                or record.generation != self.generation
                or version.generation_high_water < record.generation
            ):
                raise ValueError("NapCat operation journal record is inconsistent")
        if self.operation in {"provision", "adopt"}:
            valid_shape = (
                self.before.record is None and self.after.record is not None
            )
        elif self.operation in {"remove_runtime", "restore"}:
            valid_shape = (
                self.before.record is not None and self.after.record is not None
            )
        else:
            valid_shape = self.before.record is not None and self.after.record is None
        if not valid_shape:
            raise ValueError("NapCat operation journal transition is invalid")


class NapCatOperationJournal:
    """A single-entry, atomic and fsync-backed mutation journal."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> NapCatOperationIntent | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("NapCat operation journal is unavailable") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("NapCat operation journal is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
            raise ValueError("NapCat operation journal schema is invalid")
        raw_intent = payload.get("pending")
        if not isinstance(raw_intent, dict):
            raise ValueError("NapCat operation journal payload is invalid")
        try:
            return NapCatOperationIntent.from_json(raw_intent)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("NapCat operation journal payload is invalid") from exc

    def write(self, intent: NapCatOperationIntent) -> None:
        intent.validate()
        if self.path.exists():
            raise ValueError("NapCat operation journal already has a pending mutation")
        self._replace({"schema": _SCHEMA, "pending": intent.to_json()})

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(self.path.parent)

    def _replace(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            _fsync_directory(self.path.parent)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _version_to_json(version: InventoryVersion) -> dict[str, Any]:
    return {
        "record": version.record.to_json() if version.record is not None else None,
        "generation_high_water": version.generation_high_water,
    }


def _version_from_json(value: Any) -> InventoryVersion:
    if not isinstance(value, dict):
        raise ValueError("NapCat operation journal version is invalid")
    raw_record = value.get("record")
    if raw_record is not None and not isinstance(raw_record, dict):
        raise ValueError("NapCat operation journal record is invalid")
    return InventoryVersion(
        record=(
            NapCatInstanceRecord.from_json(raw_record)
            if raw_record is not None
            else None
        ),
        generation_high_water=int(value["generation_high_water"]),
    )


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
