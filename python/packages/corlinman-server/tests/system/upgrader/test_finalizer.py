"""Tests for :mod:`corlinman_server.system.upgrader.finalizer`.

The boot finalizer replaces the blanket "orphaned → stalled" flip with a
three-branch terminal decision (version assertion / helper-status mirror
/ stall fallback). These tests drive it through a real
:class:`UpgradeStateStore` persisted to a tmp path, exactly like the
entrypoint wiring does (``defer_boot_reconcile=True`` + immediate
``finalize_boot``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from corlinman_server.system.upgrader import (
    UpgradeRequest,
    UpgradeStateStore,
    finalize_boot,
)


def _seed_running_upgrade(
    data_dir: Path, *, tag: str = "v1.28.0", mode: str = "native"
) -> str:
    """Persist a mid-flight upgrade record, then return its request_id.

    Uses a throwaway store (auto-reconcile deferred so the record stays
    ``running`` on disk) — mirrors the state a gateway leaves behind when
    the upgrade restarts it.
    """
    store = UpgradeStateStore(
        data_dir / ".upgrade-state.json", defer_boot_reconcile=True
    )
    request_id = uuid.uuid4().hex
    req = UpgradeRequest(
        request_id=request_id,
        tag=tag,
        requested_at=1000,
        requested_by="alice",
        mode=mode,  # type: ignore[arg-type]
    )

    async def _seed() -> None:
        await store.begin(req)
        await store.update(request_id, state="running", phase="running")

    asyncio.run(_seed())
    return request_id


def _reload_and_finalize(
    data_dir: Path, *, current_version: str
) -> UpgradeStateStore:
    """Fresh store + finalizer, as the restarted gateway would run them."""
    store = UpgradeStateStore(
        data_dir / ".upgrade-state.json", defer_boot_reconcile=True
    )
    finalize_boot(store, data_dir=data_dir, current_version=current_version)
    return store


def _get(store: UpgradeStateStore, request_id: str):
    return asyncio.run(store.get(request_id))


def _write_helper_status(data_dir: Path, payload: dict) -> None:
    (data_dir / ".upgrade-status").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_version_match_finalizes_succeeded(tmp_path: Path) -> None:
    rid = _seed_running_upgrade(tmp_path, tag="v1.28.0")

    store = _reload_and_finalize(tmp_path, current_version="1.28.0")

    status = _get(store, rid)
    assert status is not None
    assert status.state == "succeeded"
    assert status.phase == "done"
    assert status.version_verified is True
    assert status.error is None
    assert status.finished_at is not None
    # Single-flight slot must be free again.
    assert asyncio.run(store.current_in_flight()) is None


def test_helper_success_with_version_mismatch_fails_assertion(
    tmp_path: Path,
) -> None:
    rid = _seed_running_upgrade(tmp_path, tag="v1.28.0")
    _write_helper_status(
        tmp_path,
        {
            "request_id": str(uuid.UUID(rid)),  # helper uses dashed form
            "state": "succeeded",
            "finished_at": 2000,
        },
    )

    store = _reload_and_finalize(tmp_path, current_version="1.27.0")

    status = _get(store, rid)
    assert status is not None
    assert status.state == "failed"
    assert status.error == "version_assertion_failed"
    assert status.version_verified is False


def test_helper_failure_is_mirrored_with_rollback_flag(tmp_path: Path) -> None:
    rid = _seed_running_upgrade(tmp_path, tag="v1.28.0")
    _write_helper_status(
        tmp_path,
        {
            "request_id": str(uuid.UUID(rid)),
            "state": "failed",
            "error": "healthcheck_timeout",
            "rolled_back": True,
            "log_excerpt": "[fail] new container never went healthy\n",
            "finished_at": 2000,
        },
    )

    store = _reload_and_finalize(tmp_path, current_version="1.27.0")

    status = _get(store, rid)
    assert status is not None
    assert status.state == "failed"
    assert status.error == "healthcheck_timeout"
    assert status.rolled_back is True
    assert "never went healthy" in status.log_excerpt
    assert status.finished_at == 2000


def test_no_status_file_falls_back_to_stalled(tmp_path: Path) -> None:
    rid = _seed_running_upgrade(tmp_path, tag="v1.28.0")

    store = _reload_and_finalize(tmp_path, current_version="1.27.0")

    status = _get(store, rid)
    assert status is not None
    assert status.state == "stalled"
    assert status.error == "gateway_restarted_mid_upgrade"
    assert asyncio.run(store.current_in_flight()) is None


def test_foreign_status_file_is_ignored(tmp_path: Path) -> None:
    rid = _seed_running_upgrade(tmp_path, tag="v1.28.0")
    _write_helper_status(
        tmp_path,
        {
            "request_id": str(uuid.uuid4()),  # some other request
            "state": "succeeded",
        },
    )

    store = _reload_and_finalize(tmp_path, current_version="1.27.0")

    status = _get(store, rid)
    assert status is not None
    assert status.state == "stalled"


def test_terminal_records_are_left_alone(tmp_path: Path) -> None:
    rid = _seed_running_upgrade(tmp_path, tag="v1.28.0")
    seed_store = UpgradeStateStore(
        tmp_path / ".upgrade-state.json", defer_boot_reconcile=True
    )
    asyncio.run(
        seed_store.update(
            rid, state="failed", phase="failed", error="image_pull_failed"
        )
    )

    store = _reload_and_finalize(tmp_path, current_version="1.28.0")

    status = _get(store, rid)
    assert status is not None
    assert status.state == "failed"
    assert status.error == "image_pull_failed"


def test_default_constructor_still_stall_flips(tmp_path: Path) -> None:
    """BUG-02 posture: any store built WITHOUT the finalizer wiring keeps
    the legacy auto-flip so single-flight can never be wedged."""
    rid = _seed_running_upgrade(tmp_path, tag="v1.28.0")

    store = UpgradeStateStore(tmp_path / ".upgrade-state.json")

    status = _get(store, rid)
    assert status is not None
    assert status.state == "stalled"


def test_additive_fields_roundtrip_and_legacy_files_load(
    tmp_path: Path,
) -> None:
    persist = tmp_path / ".upgrade-state.json"
    rid = uuid.uuid4().hex
    store = UpgradeStateStore(persist)
    req = UpgradeRequest(
        request_id=rid,
        tag="v1.28.0",
        requested_at=1000,
        requested_by="alice",
        mode="docker",
        allow_downgrade=True,
        action="rollback_instant",
    )

    async def _seed() -> None:
        await store.begin(req)
        await store.update(
            rid,
            state="succeeded",
            before_version="1.27.0",
            version_verified=True,
            rolled_back=False,
        )

    asyncio.run(_seed())

    reloaded = UpgradeStateStore(persist)
    status = _get(reloaded, rid)
    assert status is not None
    assert status.before_version == "1.27.0"
    assert status.version_verified is True
    assert status.rolled_back is False
    request = reloaded.get_request_sync(rid)
    assert request is not None
    assert request.allow_downgrade is True
    assert request.action == "rollback_instant"

    # Legacy schema-1 file (no new fields) still loads with defaults.
    legacy = {
        "requests": {
            "abc": {
                "request_id": "abc",
                "tag": "v1.0.0",
                "requested_at": 1,
                "requested_by": "bob",
                "mode": "native",
            }
        },
        "statuses": {
            "abc": {
                "request_id": "abc",
                "tag": "v1.0.0",
                "state": "succeeded",
                "phase": "done",
            }
        },
    }
    legacy_path = tmp_path / "legacy-state.json"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    legacy_store = UpgradeStateStore(legacy_path)
    legacy_status = _get(legacy_store, "abc")
    assert legacy_status is not None
    assert legacy_status.before_version is None
    assert legacy_status.version_verified is None
    legacy_req = legacy_store.get_request_sync("abc")
    assert legacy_req is not None
    assert legacy_req.allow_downgrade is False
    assert legacy_req.action == "upgrade"
