"""CMP-03 repro — the ONBOARDING episode kind is reachable end-to-end.

Before the fix, :func:`episodes_run_once` called ``classify(bundle)``
with the default ``is_onboarding=False`` (the runner never computed
it and :class:`EpisodesConfig` had no ``onboarding_first_n`` knob), so
a brand-new user's first session distilled as ``CONVERSATION`` and the
``+0.1`` onboarding importance bump was dead.

Acceptance (per audit row CMP-03): distill over a window containing a
new user's first session → kind ``ONBOARDING`` with the ``+0.1``
importance baseline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_episodes import (
    RUN_STATUS_OK,
    EpisodeKind,
    EpisodesConfig,
    EpisodesStore,
    SourcePaths,
    episodes_run_once,
    make_constant_provider,
)

from ._seed import insert_session_message


@pytest.fixture
def episodes_db(tmp_path: Path) -> Path:
    return tmp_path / "episodes.sqlite"


@pytest.fixture
def sources(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
    identity_db: Path,
) -> SourcePaths:
    return SourcePaths(
        sessions_db=sessions_db,
        evolution_db=evolution_db,
        hook_events_db=hook_events_db,
        identity_db=identity_db,
    )


def _config(**overrides: object) -> EpisodesConfig:
    base: dict[str, object] = {
        "min_window_secs": 1,
        "distillation_window_hours": 24.0,
        "max_messages_per_call": 60,
    }
    base.update(overrides)
    return EpisodesConfig(**base)  # type: ignore[arg-type]


async def test_first_session_of_new_user_is_onboarding(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """A new user's very first session distills as ONBOARDING + +0.1."""
    base_ms = 5_000_000
    # One short conversation for a brand-new user (no prior sessions
    # exist in sessions.sqlite at all → this is session #1).
    insert_session_message(
        sessions_db,
        session_key="telegram:new-user",
        seq=0,
        role="user",
        content="hi, first time here",
        ts_ms=base_ms,
    )
    insert_session_message(
        sessions_db,
        session_key="telegram:new-user",
        seq=1,
        role="agent",
        content="welcome!",
        ts_ms=base_ms + 1_000,
    )

    summary = await episodes_run_once(
        config=_config(),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("onboarding chat"),
        now_ms=base_ms + 60_000,
    )

    assert summary.status == RUN_STATUS_OK
    assert summary.episodes_written == 1

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT kind, importance_score FROM episodes"
        )
        rows = await cursor.fetchall()
        await cursor.close()

    assert len(rows) == 1
    kind, importance = rows[0]
    assert kind == EpisodeKind.ONBOARDING
    # A plain two-message chat scores 0.0 otherwise; the onboarding
    # baseline is the only contributor → exactly +0.1.
    assert importance == pytest.approx(0.1)


async def test_later_session_of_existing_user_is_conversation(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """Once a user is past their first N sessions → CONVERSATION.

    The same ``channel_user_id`` reaches us under several
    ``<channel>:<user>`` keys (web, telegram, discord). With
    ``onboarding_first_n=2`` the user's two earliest session_keys are
    onboarding; the third (in-window) one is the user's 3rd session →
    NOT onboarding.
    """
    base_ms = 6_000_000
    user = "shared-user"
    # Two earlier sessions for the SAME channel_user_id under distinct
    # channel-prefixed keys (out of window).
    insert_session_message(
        sessions_db,
        session_key=f"web:{user}",
        seq=0,
        role="user",
        content="older-1",
        ts_ms=base_ms - 2_000_000,
    )
    insert_session_message(
        sessions_db,
        session_key=f"telegram:{user}",
        seq=0,
        role="user",
        content="older-2",
        ts_ms=base_ms - 1_000_000,
    )
    # A third, in-window session for the same user.
    insert_session_message(
        sessions_db,
        session_key=f"discord:{user}",
        seq=0,
        role="user",
        content="back again",
        ts_ms=base_ms,
    )

    summary = await episodes_run_once(
        config=_config(onboarding_first_n=2),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("a chat"),
        now_ms=base_ms + 60_000,
    )
    assert summary.status == RUN_STATUS_OK

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT kind FROM episodes WHERE ended_at >= ?",
            (base_ms,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

    # The in-window session is the user's 3rd → past first-N → not
    # onboarding.
    assert rows
    assert all(r[0] != EpisodeKind.ONBOARDING for r in rows)
