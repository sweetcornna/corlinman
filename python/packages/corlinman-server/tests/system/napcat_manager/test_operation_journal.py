from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.system.napcat_manager.inventory import InventoryVersion
from corlinman_server.system.napcat_manager.models import NapCatInstanceRecord
from corlinman_server.system.napcat_manager.operation_journal import (
    NapCatOperationIntent,
    NapCatOperationJournal,
)


def _record(root: Path, *, retained: bool = False) -> NapCatInstanceRecord:
    state = root / "bot-a"
    return NapCatInstanceRecord(
        instance_id="bot-a",
        provider="native",
        generation=1,
        resource_id="corlinman-napcat@bot-a.service",
        state_root=str(state),
        token_file=str(state / "manager-secrets.env"),
        webui_port=16099,
        onebot_port=16100,
        retained=retained,
    )


def test_journal_roundtrip_and_clear(tmp_path: Path) -> None:
    journal = NapCatOperationJournal(tmp_path / "operation.json")
    intent = NapCatOperationIntent(
        operation="provision",
        request_id="req-1",
        instance_id="bot-a",
        provider="native",
        generation=1,
        before=InventoryVersion(record=None, generation_high_water=0),
        after=InventoryVersion(
            record=_record(tmp_path), generation_high_water=1
        ),
    )

    journal.write(intent)

    assert journal.load() == intent
    text = journal.path.read_text(encoding="utf-8")
    assert "WEBUI_TOKEN" not in text
    assert "ONEBOT_TOKEN" not in text
    assert journal.path.stat().st_mode & 0o777 == 0o600
    journal.clear()
    assert journal.load() is None


def test_journal_refuses_to_overwrite_pending_intent(tmp_path: Path) -> None:
    journal = NapCatOperationJournal(tmp_path / "operation.json")
    intent = NapCatOperationIntent(
        operation="provision",
        request_id="req-1",
        instance_id="bot-a",
        provider="native",
        generation=1,
        before=InventoryVersion(record=None, generation_high_water=0),
        after=InventoryVersion(
            record=_record(tmp_path), generation_high_water=1
        ),
    )
    journal.write(intent)

    with pytest.raises(ValueError, match="pending mutation"):
        journal.write(intent)


def test_journal_rejects_inconsistent_transition(tmp_path: Path) -> None:
    record = _record(tmp_path)
    intent = NapCatOperationIntent(
        operation="purge_login_state",
        request_id="req-1",
        instance_id="bot-a",
        provider="native",
        generation=1,
        before=InventoryVersion(record=record, generation_high_water=1),
        after=InventoryVersion(record=record, generation_high_water=1),
    )

    with pytest.raises(ValueError, match="transition"):
        intent.validate()


def test_journal_fails_closed_on_corrupt_payload(tmp_path: Path) -> None:
    path = tmp_path / "operation.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid"):
        NapCatOperationJournal(path).load()
