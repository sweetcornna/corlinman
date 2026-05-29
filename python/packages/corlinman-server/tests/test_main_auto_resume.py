"""Tests for the boot-time :class:`AgentResumeService`.

The scanner runs once at gateway boot and decides, per channel, whether
to re-deliver an in_progress turn (cross-channel boot replay) or to
defer to the channel's own drain (QQ-family) or to a future re-send
(HTTP).

These tests construct a journal with synthetic in_progress rows and an
in-memory inbox, run the scanner, and assert on:

- the structured :class:`ResumeScanReport` it returns;
- the inbox rows it enqueued (or didn't);
- the ``agent.resume.scan_complete`` log line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.auto_resume import (
    DEFAULT_RESUME_WINDOW_MS,
    AgentResumeService,
    BootReplayDispatcher,
    ResumeScanReport,
    run_boot_auto_resume,
)
from corlinman_server.inbox import INBOX_PENDING, Inbox


@pytest.fixture
async def journal(tmp_path: Path) -> AgentJournal:
    j = await AgentJournal.open(tmp_path / "journal.sqlite")
    yield j
    await j.close()


@pytest.fixture
async def inbox(tmp_path: Path) -> Inbox:
    ib = await Inbox.open(tmp_path / "inbox.sqlite")
    yield ib
    await ib.close()


# ---------------------------------------------------------------------------
# Channel coverage
# ---------------------------------------------------------------------------


async def test_qq_channel_is_skipped_because_inbox_drainer_owns_it(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """QQ already has the inbox drainer + reconnect; the auto-resume
    scanner must NOT enqueue a duplicate row for it. The chat handler
    will pick the original inbox row up on next dispatch poll."""
    tid = await journal.begin_turn(
        "qq|self|1|2", "hi from qq", channel="qq"
    )
    assert tid is not None

    with structlog.testing.capture_logs() as captured:
        report = await run_boot_auto_resume(journal, inbox)

    assert report.found == 1
    assert report.resumed == 0
    assert report.skipped == 1

    # Inbox stays empty — the channel handler is responsible.
    pending = await inbox.list_pending(channel="qq")
    assert pending == []

    # Diagnostic log fires (channel_owns_drain).
    skip_logs = [
        r for r in captured if r.get("event") == "agent.resume.channel_owns_drain"
    ]
    assert len(skip_logs) == 1
    assert skip_logs[0]["channel"] == "qq"

    # The scan_complete log carries the totals.
    scan_logs = [
        r for r in captured if r.get("event") == "agent.resume.scan_complete"
    ]
    assert len(scan_logs) == 1
    assert scan_logs[0]["found"] == 1
    assert scan_logs[0]["resumed"] == 0
    assert scan_logs[0]["skipped"] == 1


async def test_telegram_channel_enqueues_but_does_not_claim_resumed(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """Telegram consumed its update before the crash. The scanner
    enqueues a synthesized pending row (idempotent + forward-compatible
    for when a drainer lands) — BUT no dispatch loop in
    ``corlinman_channels.service`` drains the telegram inbox today, so
    the scan must NOT report it as ``resumed``. It is surfaced under the
    honest ``enqueued_no_drain`` count instead, with a warning log."""
    tid = await journal.begin_turn(
        "tg|chat:42", "please continue", channel="telegram"
    )
    assert tid is not None

    with structlog.testing.capture_logs() as captured:
        report = await run_boot_auto_resume(journal, inbox)

    assert report.found == 1
    # Honest accounting: NOT counted as resumed — nothing drains it.
    assert report.resumed == 0
    assert report.enqueued_no_drain == 1
    assert report.skipped == 0

    # The row is still parked in the inbox (forward-compatible).
    pending = await inbox.list_pending(channel="telegram")
    assert len(pending) == 1
    row = pending[0]
    assert row.channel == "telegram"
    assert row.session_key == "tg|chat:42"
    assert row.user_text == "please continue"
    assert row.status == INBOX_PENDING
    assert row.message_id == f"resume:{tid}"

    # A warning tells operators the message is parked, not resumed.
    warn = [
        r for r in captured if r.get("event") == "agent.resume.enqueued_no_drain"
    ]
    assert len(warn) == 1
    assert warn[0]["channel"] == "telegram"

    # The scan_complete totals reflect the honest split.
    scan = next(
        r for r in captured if r.get("event") == "agent.resume.scan_complete"
    )
    assert scan["resumed"] == 0
    assert scan["enqueued_no_drain"] == 1


async def test_discord_channel_enqueues_but_does_not_claim_resumed(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """Discord follows the same path as Telegram (no native redelivery
    AND no inbox drainer) — enqueued but not counted as resumed."""
    tid = await journal.begin_turn(
        "disc|guild:1|chan:2", "doing the thing", channel="discord"
    )
    assert tid is not None

    report = await run_boot_auto_resume(journal, inbox)
    assert report.resumed == 0
    assert report.enqueued_no_drain == 1

    pending = await inbox.list_pending(channel="discord")
    assert len(pending) == 1
    assert pending[0].message_id == f"resume:{tid}"


async def test_http_channel_is_skipped_with_unsupported_log(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """HTTP / pre-channel-column rows have no re-delivery surface."""
    tid = await journal.begin_turn("http|sess1", "POST /chat")
    assert tid is not None

    with structlog.testing.capture_logs() as captured:
        report = await run_boot_auto_resume(journal, inbox)

    assert report.found == 1
    assert report.resumed == 0
    assert report.skipped == 1

    unsupported = [
        r
        for r in captured
        if r.get("event") == "agent.resume.unsupported_channel"
    ]
    assert len(unsupported) == 1
    assert unsupported[0]["channel"] == "<none>"


async def test_mixed_channels_partition_correctly(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """A boot scan over a mix of channel types: each row routes to its
    own outcome (drain / replay / skip)."""
    await journal.begin_turn("qq-sess", "qq-text", channel="qq")
    tg_tid = await journal.begin_turn(
        "tg-sess", "tg-text", channel="telegram"
    )
    await journal.begin_turn("http-sess", "http-text")

    report = await run_boot_auto_resume(journal, inbox)
    assert report.found == 3
    # telegram is enqueued but not drained -> not resumed, distinct count.
    assert report.resumed == 0
    assert report.enqueued_no_drain == 1  # telegram
    assert report.skipped == 2  # qq + http

    # Only Telegram landed in the inbox (parked, awaiting a drainer).
    all_pending = await inbox.list_pending()
    assert {p.channel for p in all_pending} == {"telegram"}
    assert all_pending[0].user_text == "tg-text"
    assert all_pending[0].message_id == f"resume:{tg_tid}"


# ---------------------------------------------------------------------------
# Stale sweep + window
# ---------------------------------------------------------------------------


async def test_stale_sweep_runs_before_listing(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """A 25-h-old in_progress row gets swept to errored at boot, so it
    never enters the resume-window scan and never re-delivers."""
    import aiosqlite

    tid = await journal.begin_turn(
        "tg-stale", "ancient", channel="telegram"
    )
    assert tid is not None
    # Backdate 25 hours.
    async with aiosqlite.connect(journal._path) as conn:
        await conn.execute(
            "UPDATE turns SET started_at_ms = 0 WHERE turn_id = ?", (tid,)
        )
        await conn.commit()

    report = await run_boot_auto_resume(journal, inbox)
    assert report.swept >= 1
    assert report.found == 0
    assert report.resumed == 0

    pending = await inbox.list_pending(channel="telegram")
    assert pending == []


async def test_scan_complete_log_carries_window_minutes(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """Operators grep ``window_minutes`` to confirm the recency window
    that fired."""
    await journal.begin_turn("tg", "text", channel="telegram")

    with structlog.testing.capture_logs() as captured:
        await run_boot_auto_resume(journal, inbox, window_ms=5 * 60_000)

    scan = next(
        r for r in captured if r.get("event") == "agent.resume.scan_complete"
    )
    assert scan["window_minutes"] == 5
    assert scan["found"] == 1
    # telegram has no drainer -> not resumed, surfaced under no_drain.
    assert scan["resumed"] == 0
    assert scan["enqueued_no_drain"] == 1


# ---------------------------------------------------------------------------
# BootReplayDispatcher unit tests
# ---------------------------------------------------------------------------


async def test_boot_replay_dispatcher_skips_empty_channel(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """An InProgressTurn without a channel id cannot be re-delivered —
    the dispatcher refuses to enqueue (returns False)."""
    from corlinman_server.agent_journal_backend import InProgressTurn

    dispatcher = BootReplayDispatcher(inbox)
    turn = InProgressTurn(
        turn_id=1,
        session_key="sess",
        user_id=None,
        user_text="hello",
        started_at_ms=1,
        channel="",
    )
    assert await dispatcher.replay(turn) is False
    assert await inbox.list_pending() == []


async def test_boot_replay_dispatcher_skips_empty_text(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """Empty user_text — nothing to re-deliver."""
    from corlinman_server.agent_journal_backend import InProgressTurn

    dispatcher = BootReplayDispatcher(inbox)
    turn = InProgressTurn(
        turn_id=1,
        session_key="sess",
        user_id=None,
        user_text="",
        started_at_ms=1,
        channel="telegram",
    )
    assert await dispatcher.replay(turn) is False


# ---------------------------------------------------------------------------
# Service config + idempotency
# ---------------------------------------------------------------------------


async def test_service_run_without_inbox_logs_unsupported(
    journal: AgentJournal,
) -> None:
    """If the gateway can't open an inbox (e.g. data_dir read-only),
    every cross-channel row is logged as unsupported and no re-delivery
    happens. The scan still completes cleanly."""
    await journal.begin_turn("tg", "text", channel="telegram")

    service = AgentResumeService(journal, inbox=None)
    with structlog.testing.capture_logs() as captured:
        report = await service.run()

    assert report.found == 1
    assert report.resumed == 0
    assert report.skipped == 1

    unsupported = [
        r
        for r in captured
        if r.get("event") == "agent.resume.unsupported_channel"
    ]
    assert any(r.get("reason") == "no_inbox" for r in unsupported)


async def test_service_run_returns_typed_report(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """The return shape is :class:`ResumeScanReport` with every count
    field populated — admin diag surfaces / future tests can introspect
    instead of parsing logs."""
    await journal.begin_turn("tg", "text-a", channel="telegram")
    await journal.begin_turn("qq", "text-b", channel="qq")

    report = await run_boot_auto_resume(journal, inbox)
    assert isinstance(report, ResumeScanReport)
    assert report.found == 2
    # telegram enqueued-but-not-drained; qq owns its own drain.
    assert report.resumed == 0
    assert report.enqueued_no_drain == 1  # telegram
    assert report.skipped == 1  # qq
    assert report.window_ms == DEFAULT_RESUME_WINDOW_MS
    # Turns tuple round-trips so callers can introspect.
    assert len(report.turns) == 2
    seen_channels = {t.channel for t in report.turns}
    assert seen_channels == {"telegram", "qq"}


async def test_replay_idempotent_under_double_boot(
    journal: AgentJournal, inbox: Inbox
) -> None:
    """Booting the scanner twice in a row (e.g. two HA nodes starting
    together) enqueues twice. That's expected — the chat handler's
    ``find_resumable_turn`` collapses duplicates because both arrive
    with the same ``(session_key, user_text)`` and join the same
    in_progress row.

    This test exists to document that we DO NOT need server-side
    dedup; the consumer side handles it.
    """
    await journal.begin_turn("tg", "text", channel="telegram")
    await run_boot_auto_resume(journal, inbox)
    await run_boot_auto_resume(journal, inbox)

    pending = await inbox.list_pending(channel="telegram")
    # Two rows — same user_text, different inbox ids. The downstream
    # chat handler de-duplicates via journal resume.
    assert len(pending) == 2
    assert {p.user_text for p in pending} == {"text"}


# ---------------------------------------------------------------------------
# Honesty regression: enqueue-without-drain must not be reported as resumed
# ---------------------------------------------------------------------------


def test_no_boot_replay_channel_has_a_real_drain_in_channels_service() -> None:
    """Ground truth for the honesty fix: ``CHANNEL_HAS_BOOT_REPLAY_DRAIN``
    must only list channels that a dispatch loop actually drains in
    ``corlinman_channels.service``. Today that set is empty because the
    service only polls ``inbox.list_pending(channel="qq")`` — no
    telegram/discord/slack/feishu drain exists. If someone later wires a
    drainer they must (a) add the channel id to the set AND (b) update
    this assertion, which forces the two to stay in lockstep.
    """
    import inspect

    from corlinman_channels import service as channels_service
    from corlinman_server.auto_resume import (
        CHANNEL_HAS_BOOT_REPLAY_DRAIN,
        CHANNEL_NEEDS_BOOT_REPLAY,
    )

    src = inspect.getsource(channels_service)
    for channel in CHANNEL_HAS_BOOT_REPLAY_DRAIN:
        # Every channel we claim is drainable must have a matching
        # list_pending poll in the channels service.
        assert (
            f'list_pending(channel="{channel}"' in src
        ), f"{channel} claimed drainable but no drainer in channels.service"

    # And no boot-replay channel we DON'T list as drainable should have a
    # drainer either (otherwise we are under-counting genuine resumes).
    for channel in CHANNEL_NEEDS_BOOT_REPLAY - CHANNEL_HAS_BOOT_REPLAY_DRAIN:
        assert (
            f'list_pending(channel="{channel}"' not in src
        ), f"{channel} has a drainer but is not in CHANNEL_HAS_BOOT_REPLAY_DRAIN"


async def test_genuine_resume_counted_when_channel_has_drainer(
    journal: AgentJournal, inbox: Inbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forward-compat: the day a per-channel drainer is wired and its
    channel is added to ``_CHANNEL_HAS_BOOT_REPLAY_DRAIN``, the scan must
    count it as a genuine ``resumed`` (and NOT under ``enqueued_no_drain``).
    Simulate that future state by extending the set for this test only.
    """
    import corlinman_server.auto_resume as ar

    monkeypatch.setattr(
        ar, "_CHANNEL_HAS_BOOT_REPLAY_DRAIN", frozenset({"telegram"})
    )

    await journal.begin_turn("tg|chat:9", "resume me", channel="telegram")

    with structlog.testing.capture_logs() as captured:
        report = await run_boot_auto_resume(journal, inbox)

    assert report.found == 1
    assert report.resumed == 1
    assert report.enqueued_no_drain == 0

    # No false "parked" warning when the channel is genuinely drainable.
    warn = [
        r for r in captured if r.get("event") == "agent.resume.enqueued_no_drain"
    ]
    assert warn == []
