"""Unit tests for :mod:`corlinman_server.home_channel_store`.

Covers the home-channel binding lifecycle (set/get round-trip,
idempotent re-``/sethome`` on the ``user_id`` PK, ``list_all_homes``
ordering), the ``resolve_user_id`` channel+sender keying, and the
one-time tip flag (``mark_tip_shown`` / ``was_tip_shown``).

Every helper takes an explicit ``db_path`` so the tests are fully
isolated from the gateway data dir (the autouse conftest fixture
already pins ``CORLINMAN_DATA_DIR`` at a temp dir, but pinning
``db_path`` makes the exercised path unambiguous).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from corlinman_server.home_channel_store import (
    HomeChannelRow,
    default_db_path,
    get_home,
    list_all_homes,
    mark_tip_shown,
    resolve_user_id,
    set_home,
    was_tip_shown,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "homes.sqlite"


# ---------------------------------------------------------------------------
# resolve_user_id
# ---------------------------------------------------------------------------


def test_resolve_user_id_combines_channel_and_sender() -> None:
    assert resolve_user_id("telegram", "12345") == "telegram:12345"


def test_resolve_user_id_is_deterministic_and_channel_scoped() -> None:
    # Same human id on two channels resolves to two distinct keys.
    assert resolve_user_id("slack", "u1") != resolve_user_id("discord", "u1")
    # Stable across repeated calls.
    assert resolve_user_id("slack", "u1") == resolve_user_id("slack", "u1")


# ---------------------------------------------------------------------------
# set_home / get_home round-trip
# ---------------------------------------------------------------------------


def test_get_home_unset_returns_none(db_path: Path) -> None:
    assert get_home("telegram:nobody", db_path=db_path) is None


def test_set_home_get_home_round_trip(db_path: Path) -> None:
    set_home(
        "telegram:42",
        channel="telegram",
        account="bot-a",
        thread="dm-42",
        sender="42",
        db_path=db_path,
        now_ms=1_700_000_000_000,
    )

    row = get_home("telegram:42", db_path=db_path)
    assert row == HomeChannelRow(
        user_id="telegram:42",
        channel="telegram",
        account="bot-a",
        thread="dm-42",
        sender="42",
        set_at_ms=1_700_000_000_000,
    )


def test_set_home_get_home_via_resolve_user_id(db_path: Path) -> None:
    uid = resolve_user_id("slack", "U99")
    set_home(
        uid,
        channel="slack",
        account="ws",
        thread="C1",
        sender="U99",
        db_path=db_path,
        now_ms=123,
    )
    row = get_home(uid, db_path=db_path)
    assert row is not None
    assert row.user_id == "slack:U99"
    assert row.sender == "U99"


# ---------------------------------------------------------------------------
# idempotent re-/sethome on the user_id PK (no dup, updates)
# ---------------------------------------------------------------------------


def test_resethome_replaces_row_in_place_no_duplicate(db_path: Path) -> None:
    uid = "telegram:42"
    set_home(
        uid,
        channel="telegram",
        account="bot-a",
        thread="dm-old",
        sender="42",
        db_path=db_path,
        now_ms=1000,
    )
    # User re-issues /sethome from a *different* binding.
    set_home(
        uid,
        channel="discord",
        account="guild-x",
        thread="chan-new",
        sender="42#dc",
        db_path=db_path,
        now_ms=2000,
    )

    # Exactly one row for the PK — UPSERT replaced, not appended.
    assert len(list_all_homes(db_path=db_path)) == 1

    row = get_home(uid, db_path=db_path)
    assert row is not None
    # All mutable columns reflect the second call.
    assert row.channel == "discord"
    assert row.account == "guild-x"
    assert row.thread == "chan-new"
    assert row.sender == "42#dc"
    assert row.set_at_ms == 2000


def test_resethome_same_binding_is_idempotent_but_bumps_timestamp(
    db_path: Path,
) -> None:
    uid = "telegram:7"
    kwargs = {"channel": "telegram", "account": "bot", "thread": "dm-7", "sender": "7"}
    set_home(uid, **kwargs, db_path=db_path, now_ms=100)
    set_home(uid, **kwargs, db_path=db_path, now_ms=500)

    rows = list_all_homes(db_path=db_path)
    assert len(rows) == 1
    assert rows[0].set_at_ms == 500


# ---------------------------------------------------------------------------
# list_all_homes
# ---------------------------------------------------------------------------


def test_list_all_homes_empty(db_path: Path) -> None:
    assert list_all_homes(db_path=db_path) == []


def test_list_all_homes_returns_all_ordered_by_set_at(db_path: Path) -> None:
    set_home(
        "c:b", channel="c", account="a", thread="t", sender="b",
        db_path=db_path, now_ms=3000,
    )
    set_home(
        "c:a", channel="c", account="a", thread="t", sender="a",
        db_path=db_path, now_ms=1000,
    )
    set_home(
        "c:c", channel="c", account="a", thread="t", sender="c",
        db_path=db_path, now_ms=2000,
    )

    rows = list_all_homes(db_path=db_path)
    assert [r.user_id for r in rows] == ["c:a", "c:c", "c:b"]
    assert [r.set_at_ms for r in rows] == [1000, 2000, 3000]


# ---------------------------------------------------------------------------
# mark_tip_shown / was_tip_shown
# ---------------------------------------------------------------------------


def test_tip_not_shown_by_default(db_path: Path) -> None:
    assert was_tip_shown("u1", "telegram", "dm-1", db_path=db_path) is False


def test_mark_tip_shown_round_trip(db_path: Path) -> None:
    mark_tip_shown("u1", "telegram", "dm-1", db_path=db_path, now_ms=42)
    assert was_tip_shown("u1", "telegram", "dm-1", db_path=db_path) is True


def test_tip_flag_is_scoped_per_user_channel_thread(db_path: Path) -> None:
    mark_tip_shown("u1", "telegram", "dm-1", db_path=db_path, now_ms=42)
    # Different thread / channel / user => independent flags, still unshown.
    assert was_tip_shown("u1", "telegram", "dm-2", db_path=db_path) is False
    assert was_tip_shown("u1", "slack", "dm-1", db_path=db_path) is False
    assert was_tip_shown("u2", "telegram", "dm-1", db_path=db_path) is False


def test_mark_tip_shown_idempotent_preserves_first_timestamp(
    db_path: Path,
) -> None:
    mark_tip_shown("u1", "telegram", "dm-1", db_path=db_path, now_ms=100)
    # Re-mark with a later clock — ON CONFLICT DO NOTHING must keep the
    # original shown_at_ms (first display time is what audits see).
    mark_tip_shown("u1", "telegram", "dm-1", db_path=db_path, now_ms=999)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT shown_at_ms FROM first_chat_tips_shown "
            "WHERE user_id=? AND channel=? AND thread=?",
            ("u1", "telegram", "dm-1"),
        ).fetchall()
    finally:
        conn.close()

    # Exactly one row (no duplicate insert) and the original timestamp.
    assert rows == [(100,)]


# ---------------------------------------------------------------------------
# default_db_path resolution
# ---------------------------------------------------------------------------


def test_default_db_path_honours_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    assert default_db_path() == tmp_path / "home_channels.sqlite"
