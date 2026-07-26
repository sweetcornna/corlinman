"""Durable, non-secret inventory for manager-owned NapCat resources."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from corlinman_server.gateway.qq_instances.models import parse_instance_id
from corlinman_server.system.napcat_manager.models import NapCatInstanceRecord

_SCHEMA = 2


@dataclass(frozen=True, slots=True)
class InventoryVersion:
    """Atomic record plus generation high-water for one instance."""

    record: NapCatInstanceRecord | None
    generation_high_water: int


class NapCatInventory:
    def __init__(
        self,
        path: Path,
        *,
        state_root: Path,
        legacy_state_roots: tuple[Path, ...] = (),
    ) -> None:
        self.path = Path(path)
        self.state_root = Path(state_root).resolve(strict=False)
        self.legacy_state_roots = tuple(
            Path(root).resolve(strict=False) for root in legacy_state_roots
        )
        self._records: dict[str, NapCatInstanceRecord] = {}
        self._generations: dict[str, int] = {}
        self._load()

    def all(self) -> dict[str, NapCatInstanceRecord]:
        return {
            instance_id: _clone_record(record)
            for instance_id, record in self._records.items()
        }

    def get(self, instance_id: str) -> NapCatInstanceRecord | None:
        record = self._records.get(str(parse_instance_id(instance_id)))
        return _clone_record(record) if record is not None else None

    def next_generation(self, instance_id: str) -> int:
        return self.snapshot(instance_id).generation_high_water + 1

    def snapshot(self, instance_id: str) -> InventoryVersion:
        instance_id = str(parse_instance_id(instance_id))
        record = self._records.get(instance_id)
        high_water = self._generations.get(instance_id, 0)
        if record is not None:
            high_water = max(high_water, record.generation)
        return InventoryVersion(
            record=_clone_record(record) if record is not None else None,
            generation_high_water=high_water,
        )

    def matches(self, instance_id: str, version: InventoryVersion) -> bool:
        return self.snapshot(instance_id) == version

    def commit_version(self, instance_id: str, version: InventoryVersion) -> None:
        instance_id = str(parse_instance_id(instance_id))
        if version.generation_high_water < 0:
            raise ValueError("NapCat inventory generation is invalid")
        record = version.record
        if record is not None:
            if record.instance_id != instance_id:
                raise ValueError("NapCat inventory instance id does not match its key")
            if version.generation_high_water < record.generation:
                raise ValueError("NapCat inventory generation is inconsistent")
            self.assert_owned_paths(record)
        records = dict(self._records)
        generations = dict(self._generations)
        if record is None:
            records.pop(instance_id, None)
        else:
            records[instance_id] = _clone_record(record)
        if version.generation_high_water > 0:
            generations[instance_id] = version.generation_high_water
        else:
            generations.pop(instance_id, None)
        self._flush(records, generations)
        self._records = records
        self._generations = generations

    def put(self, record: NapCatInstanceRecord) -> None:
        current = self.snapshot(record.instance_id)
        self.commit_version(
            record.instance_id,
            InventoryVersion(
                record=record,
                generation_high_water=max(
                    current.generation_high_water,
                    record.generation,
                ),
            ),
        )

    def delete(self, instance_id: str) -> None:
        current = self.snapshot(instance_id)
        self.commit_version(
            instance_id,
            InventoryVersion(
                record=None,
                generation_high_water=current.generation_high_water,
            ),
        )

    def assert_owned_paths(self, record: NapCatInstanceRecord) -> None:
        lexical_state = Path(record.state_root)
        lexical_token = Path(record.token_file)
        state = lexical_state.resolve(strict=False)
        token = lexical_token.resolve(strict=False)
        if record.legacy_resource:
            if state not in self.legacy_state_roots:
                raise ValueError("NapCat legacy state_root is not explicitly owned")
            _reject_symlink_chain(lexical_state, state)
        else:
            _reject_symlink_chain(lexical_state, self.state_root)
            if not _is_within(state, self.state_root):
                raise ValueError("NapCat state_root is outside the managed root")
        if not _is_within(token, state):
            raise ValueError("NapCat token_file is outside the instance state root")
        _reject_symlink_chain(lexical_token, state)

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError("NapCat inventory is unavailable") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("NapCat inventory is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
            raise ValueError("NapCat inventory schema is invalid")
        records = payload.get("instances")
        generations = payload.get("generations")
        if not isinstance(records, dict) or not isinstance(generations, dict):
            raise ValueError("NapCat inventory payload is invalid")

        loaded_generations: dict[str, int] = {}
        for instance_id, generation in generations.items():
            try:
                parsed_id = str(parse_instance_id(instance_id))
                parsed_generation = int(generation)
            except (TypeError, ValueError) as exc:
                raise ValueError("NapCat inventory generation is invalid") from exc
            if parsed_generation <= 0:
                raise ValueError("NapCat inventory generation is invalid")
            loaded_generations[parsed_id] = parsed_generation

        loaded_records: dict[str, NapCatInstanceRecord] = {}
        for instance_id, value in records.items():
            if not isinstance(value, dict):
                raise ValueError("NapCat inventory instance is invalid")
            try:
                record = NapCatInstanceRecord.from_json(value)
                if record.instance_id != instance_id:
                    raise ValueError("NapCat inventory instance id does not match its key")
                parse_instance_id(record.instance_id)
                self.assert_owned_paths(record)
            except (KeyError, TypeError, ValueError, OSError) as exc:
                raise ValueError("NapCat inventory instance is invalid") from exc
            high_water = loaded_generations.get(record.instance_id)
            if high_water is None or high_water < record.generation:
                raise ValueError("NapCat inventory generation is inconsistent")
            loaded_records[record.instance_id] = record

        self._records = loaded_records
        self._generations = loaded_generations

    def _flush(
        self,
        records: dict[str, NapCatInstanceRecord],
        generations: dict[str, int],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _SCHEMA,
            "generations": dict(sorted(generations.items())),
            "instances": {
                key: record.to_json() for key, record in sorted(records.items())
            },
        }
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
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _clone_record(record: NapCatInstanceRecord) -> NapCatInstanceRecord:
    return NapCatInstanceRecord.from_json(record.to_json())


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_symlink_chain(path: Path, stop: Path) -> None:
    current = path
    while _is_within(current, stop):
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode):
                raise ValueError("managed NapCat path contains a symlink")
        if current == stop:
            break
        current = current.parent
