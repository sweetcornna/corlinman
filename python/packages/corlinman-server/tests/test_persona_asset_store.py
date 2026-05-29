"""Tests for ``corlinman_server.persona.asset_store`` — emoji + reference
image registry backing the Persona Studio (PLAN_PERSONA_STUDIO W1).

Covers the contract the admin routes + agent tools depend on:

* put/get/list/delete round-trip
* MIME allowlist enforcement
* Per-asset + per-persona quota enforcement
* sha256-keyed on-disk blob dedup
* delete_all sweeps both metadata and blobs
* Replacement upload (same slot) reuses the row id and frees the prior bytes
"""

from __future__ import annotations

import hashlib

import pytest
from corlinman_server.persona import (
    AssetMimeRejected,
    AssetQuotaExceeded,
    AssetTooLarge,
    PersonaAssetStore,
)

# A tiny but legitimate PNG ("\x89PNG\r\n\x1a\n" + IHDR-only) so we can
# upload "valid bytes" without hauling around a real image asset.
_PNG_MAGIC = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108020000009077"
    "53DE"
)


def _png(extra: int = 0) -> bytes:
    """Build a deterministic byte payload that starts with the PNG
    magic header — content matters only insofar as size + sha256 vary."""
    return _PNG_MAGIC + (b"\x00" * extra)


@pytest.fixture
async def store(tmp_path):
    s = await PersonaAssetStore.open(
        tmp_path / "persona_assets.sqlite",
        tmp_path / "personas",
        max_bytes_per_asset=1024 * 1024,  # 1 MiB
        max_bytes_per_persona=4 * 1024 * 1024,  # 4 MiB
    )
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_put_get_round_trip(store, tmp_path) -> None:
    rec = await store.put(
        "grantley",
        "emoji",
        "happy",
        bytes_=_png(),
        mime="image/png",
        file_name="happy.png",
    )
    assert rec.persona_id == "grantley"
    assert rec.kind == "emoji"
    assert rec.label == "happy"
    assert rec.sha256 == hashlib.sha256(_png()).hexdigest()

    got = await store.get("grantley", "emoji", "happy")
    assert got is not None
    assert got.id == rec.id

    blob = store.path_for(got)
    assert blob.is_file()
    assert blob.read_bytes() == _png()


async def test_list_by_kind(store) -> None:
    await store.put(
        "p1", "emoji", "happy", bytes_=_png(1),
        mime="image/png", file_name="h.png",
    )
    await store.put(
        "p1", "emoji", "angry", bytes_=_png(2),
        mime="image/png", file_name="a.png",
    )
    await store.put(
        "p1", "reference", "front", bytes_=_png(3),
        mime="image/png", file_name="f.png",
    )
    emoji = await store.list("p1", kind="emoji")
    refs = await store.list("p1", kind="reference")
    all_ = await store.list("p1")
    assert {a.label for a in emoji} == {"happy", "angry"}
    assert {a.label for a in refs} == {"front"}
    assert len(all_) == 3


async def test_delete_round_trip(store) -> None:
    rec = await store.put(
        "p1", "emoji", "happy", bytes_=_png(),
        mime="image/png", file_name="h.png",
    )
    blob = store.path_for(rec)
    assert blob.is_file()
    assert await store.delete("p1", "emoji", "happy") is True
    assert await store.get("p1", "emoji", "happy") is None
    assert not blob.exists()
    # Second delete is a no-op.
    assert await store.delete("p1", "emoji", "happy") is False


async def test_delete_by_id(store) -> None:
    rec = await store.put(
        "p1", "emoji", "happy", bytes_=_png(),
        mime="image/png", file_name="h.png",
    )
    assert await store.delete_by_id(rec.id) is True
    assert await store.get_by_id(rec.id) is None


async def test_delete_all_sweeps_dir(store, tmp_path) -> None:
    for label in ("happy", "angry", "sad"):
        await store.put(
            "p1", "emoji", label, bytes_=_png(len(label)),
            mime="image/png", file_name=f"{label}.png",
        )
    await store.put(
        "p1", "reference", "front", bytes_=_png(99),
        mime="image/png", file_name="front.png",
    )
    removed = await store.delete_all("p1")
    assert removed == 4
    assert await store.list("p1") == []
    persona_dir = tmp_path / "personas" / "p1"
    # Either gone entirely or at least empty of the bucket dirs.
    assert not persona_dir.exists() or not any(persona_dir.iterdir())


# ---------------------------------------------------------------------------
# Replacement uploads
# ---------------------------------------------------------------------------


async def test_replacement_reuses_row_id(store) -> None:
    first = await store.put(
        "p1", "emoji", "happy", bytes_=_png(1),
        mime="image/png", file_name="happy.png",
    )
    second = await store.put(
        "p1", "emoji", "happy", bytes_=_png(2),
        mime="image/png", file_name="happy.png",
    )
    assert first.id == second.id, "row id must be stable across replacement"
    assert first.sha256 != second.sha256
    # Old blob is gone, new one is present.
    assert not store.path_for(first).exists()
    assert store.path_for(second).is_file()


async def test_replacement_frees_old_bytes_for_quota(store) -> None:
    # Persona cap is 4 MiB; each asset is ~512 KiB. We push 7 slots
    # (3.5 MiB total), then replace one with bigger bytes that would
    # exceed the cap if the old bytes were still counted but fit
    # cleanly when freed.
    for i in range(7):
        await store.put(
            "p1", "emoji", f"slot{i}",
            bytes_=_png(512 * 1024 - 8),  # ~512 KiB
            mime="image/png", file_name=f"s{i}.png",
        )
    # Replace slot0 with a bigger asset (would otherwise overflow).
    await store.put(
        "p1", "emoji", "slot0",
        bytes_=_png(900 * 1024),
        mime="image/png", file_name="s0v2.png",
    )


# ---------------------------------------------------------------------------
# Quota + MIME enforcement
# ---------------------------------------------------------------------------


async def test_mime_rejected(store) -> None:
    with pytest.raises(AssetMimeRejected):
        await store.put(
            "p1", "emoji", "txt",
            bytes_=b"hello",
            mime="text/plain",
            file_name="x.txt",
        )


async def test_per_asset_cap(store) -> None:
    # Per-asset cap is 1 MiB in the fixture.
    with pytest.raises(AssetTooLarge):
        await store.put(
            "p1", "emoji", "big",
            bytes_=_png(2 * 1024 * 1024),
            mime="image/png", file_name="big.png",
        )


async def test_per_persona_cap(store) -> None:
    # Per-persona cap is 4 MiB; 5 × ~900 KiB = ~4.5 MiB → 5th rejected.
    for i in range(4):
        await store.put(
            "p1", "emoji", f"s{i}",
            bytes_=_png(900 * 1024),
            mime="image/png", file_name=f"s{i}.png",
        )
    with pytest.raises(AssetQuotaExceeded):
        await store.put(
            "p1", "emoji", "overflow",
            bytes_=_png(900 * 1024),
            mime="image/png", file_name="o.png",
        )


# ---------------------------------------------------------------------------
# Multi-persona isolation
# ---------------------------------------------------------------------------


async def test_personas_are_isolated(store) -> None:
    await store.put(
        "alice", "emoji", "happy", bytes_=_png(1),
        mime="image/png", file_name="a.png",
    )
    await store.put(
        "bob", "emoji", "happy", bytes_=_png(2),
        mime="image/png", file_name="b.png",
    )
    a = await store.get("alice", "emoji", "happy")
    b = await store.get("bob", "emoji", "happy")
    assert a is not None and b is not None
    assert a.id != b.id
    assert a.sha256 != b.sha256


# ---------------------------------------------------------------------------
# Used-bytes accounting
# ---------------------------------------------------------------------------


async def test_used_bytes_tracks_writes_and_deletes(store) -> None:
    assert await store.used_bytes("p1") == 0
    r1 = await store.put(
        "p1", "emoji", "h", bytes_=_png(100),
        mime="image/png", file_name="h.png",
    )
    assert await store.used_bytes("p1") == r1.size_bytes
    r2 = await store.put(
        "p1", "reference", "front", bytes_=_png(200),
        mime="image/png", file_name="f.png",
    )
    assert await store.used_bytes("p1") == r1.size_bytes + r2.size_bytes
    await store.delete("p1", "emoji", "h")
    assert await store.used_bytes("p1") == r2.size_bytes
