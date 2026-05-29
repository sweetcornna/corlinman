"""Gateway-boot auto-resume scanner — picks up turns interrupted by a
process crash and re-drives them without waiting for the user to resend.

Mirrors hermes-agent's "resume on restart" behavior: the chat handler
already implements the *read* half (a Chat RPC that re-arrives with the
same ``(session_key, user_text)`` within the resume window picks up the
journal's in-progress row and replays its tool buffer — see
:meth:`corlinman_server.agent_journal.AgentJournal.find_resumable_turn`).
This module implements the *write* half:

- Scan the journal for every ``in_progress`` row younger than the resume
  window.
- For channels that already have a durable inbox (QQ), do nothing — the
  channel's own boot drainer (``Inbox.reset_stale_dispatched`` plus the
  channel reconnect handler) replays the inbox row, the chat RPC
  re-arrives, ``find_resumable_turn`` matches, and the agent continues
  exactly where it left off.
- For channels that consumed the upstream event before the crash
  (Telegram long-poll advances its offset eagerly, Discord/Slack ack
  events on receipt) the upstream WILL NOT redeliver. We synthesise a
  fresh inbox row tagged with the channel so the next dispatch poll
  picks it up — the existing channel handler then re-runs the message
  through the chat RPC, the journal matches, and resume fires.

The scanner runs **once** at gateway boot, between the gRPC server
starting and the first client RPC being accepted. Operators grep
``agent.resume.scan_complete`` to confirm it fired.

Multi-node note: two gateway nodes booting against the same Postgres
journal can both enter the scan. The :class:`Inbox` is local to each
node, so each one enqueues its own boot-replay rows, but the chat
handler's ``find_resumable_turn`` lookup is atomic and the Postgres
backend's partial unique index on
``(session_key, user_text, user_id)`` where ``status='in_progress'``
means at most one ``begin_turn`` wins. The losing node's re-delivery
will hit the existing in_progress row via ``find_resumable_turn`` and
join the same replay buffer — no duplicate work, no torn state.

For SQLite (single-process), there is only one gateway and the question
is moot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import structlog

from corlinman_server.agent_journal import AgentJournal, InProgressTurn
from corlinman_server.inbox import Inbox

logger = structlog.get_logger(__name__)


# Default boot-time recency window. Anything older than this is assumed
# to be a turn the user has moved on from — we sweep it to ``errored``
# rather than re-deliver. The number matches hermes-agent's resume TTL.
DEFAULT_RESUME_WINDOW_MS: int = 10 * 60 * 1000

# Default stale-sweep cutoff. Rows older than this AND still
# ``in_progress`` are unambiguously abandoned (24h+); we flip them to
# ``errored`` so the table doesn't accumulate phantom rows.
DEFAULT_STALE_CUTOFF_S: int = 24 * 60 * 60


# Channels that already have an inbox + dispatch loop that re-runs
# pending rows on reconnect. The channel handles re-delivery itself;
# the auto-resume scanner only logs the row and moves on. ``""`` is
# treated as HTTP — no re-delivery path exists, so the user must resend.
_CHANNEL_HAS_OWN_DRAIN = frozenset({"qq", "qq_official", "wechat_official"})

# Channels whose upstream events are consumed before the crash and
# therefore WILL NOT redeliver. The scanner enqueues a synthesized
# inbox row tagged with the channel.
#
# IMPORTANT — the enqueue is only HALF the contract. A row is only
# truly "resumed" once a dispatch loop in
# ``corlinman_channels.service`` drains pending rows for that channel
# and re-runs them through the chat RPC. Today only ``qq`` has such a
# drainer (``service._try_open_inbox`` + ``_qq_dispatch_loop`` call
# ``inbox.list_pending(channel="qq")``); telegram/discord/slack/feishu
# ``run_*_channel`` loops do NOT poll the inbox. So a synthesized row
# for those channels sits ``pending`` forever.
#
# We still enqueue (the write is idempotent and forward-compatible —
# the day a per-channel drainer lands, the row is already waiting), but
# we must NOT count it as a successful resume, or the boot scan reports
# a false-positive: "N turns resumed" when in fact nothing will consume
# them. ``_CHANNEL_HAS_BOOT_REPLAY_DRAIN`` is the subset of
# replay-enqueue channels that a drainer actually exists for; only
# those increment ``resumed``. The rest increment ``enqueued_no_drain``
# and log a warning so operators are told the truth.
_CHANNEL_NEEDS_BOOT_REPLAY = frozenset({"telegram", "discord", "slack", "feishu"})

# Subset of :data:`_CHANNEL_NEEDS_BOOT_REPLAY` for which a real inbox
# drain loop exists in ``corlinman_channels.service``. Empty today —
# none of the boot-replay channels are drained (only ``qq``, which is
# in :data:`_CHANNEL_HAS_OWN_DRAIN`, not here). When a per-channel
# drainer is wired (e.g. a ``_telegram_dispatch_loop`` that calls
# ``inbox.list_pending(channel="telegram")``), add its channel id here
# and the boot scan will start counting it as a genuine resume.
_CHANNEL_HAS_BOOT_REPLAY_DRAIN: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResumeScanReport:
    """Outcome of one :meth:`AgentResumeService.run` pass.

    Surface for tests + a future ``/admin/resume`` diag route — the boot
    log line already carries the numbers an operator needs, but having
    the structured result back makes the scanner unit-testable without
    parsing logs.
    """

    found: int
    """Rows the scan saw within the recency window."""
    resumed: int
    """Rows genuinely resumed — a synthesized inbox row was enqueued
    AND a dispatch loop exists for that channel that will drain it. This
    count is honest: it only includes channels in
    :data:`_CHANNEL_HAS_BOOT_REPLAY_DRAIN`."""
    enqueued_no_drain: int
    """Rows where a synthesized inbox row WAS enqueued but NO dispatch
    loop drains that channel (telegram/discord/slack/feishu today). The
    row is parked ``pending`` and will only be picked up once a drainer
    is wired. NOT counted as ``resumed`` — surfacing this separately is
    what keeps the boot scan from claiming a false-positive success."""
    skipped: int
    """Rows for channels that handle their own re-delivery (QQ etc.)
    or HTTP turns where no re-delivery path exists."""
    swept: int
    """Rows the boot-time stale sweep flipped to ``errored`` (older
    than the stale-cutoff, distinct from the recency-window scan)."""
    window_ms: int
    """The recency window the scan ran under, for log/UI display."""
    turns: tuple[InProgressTurn, ...] = field(default=())
    """The in-progress rows the scanner saw (for diagnostics)."""


class BootReplayDispatcher:
    """Enqueues synthesized inbox rows for in-progress turns whose
    upstream event was already consumed before the crash.

    Carved out as its own class so the auto-resume service stays a
    thin coordinator and the actual re-enqueue logic is independently
    testable. Lives on top of :class:`Inbox` — the channel-specific
    dispatch loops (existing ``_qq_dispatch_loop``, future
    ``_telegram_dispatch_loop``) drain the same inbox on next poll.
    """

    __slots__ = ("_inbox",)

    def __init__(self, inbox: Inbox) -> None:
        self._inbox = inbox

    async def replay(self, turn: InProgressTurn) -> bool:
        """Synthesize a fresh ``pending`` row for ``turn``. Returns
        ``True`` when a row was enqueued, ``False`` otherwise (empty
        user_text or channel, or an inbox write failure).

        The ``message_id`` is prefixed ``resume:`` so the channel
        handler can short-circuit retry logic that keys on the original
        upstream id — a synthesized resume row should never look like
        the original upstream event, otherwise it would race the
        original (if the upstream DID redeliver).

        Idempotency: re-enqueueing on a subsequent reboot is harmless
        because the chat handler's ``find_resumable_turn`` matches by
        ``(session_key, user_text)`` and the inbox row only triggers
        ONE chat RPC; the second resume RPC arrives, finds the same
        in_progress row, and joins it.
        """
        if not turn.user_text or not turn.channel:
            return False
        synthesized_id = f"resume:{turn.turn_id}"
        try:
            inbox_id = await self._inbox.enqueue(
                channel=turn.channel,
                session_key=turn.session_key,
                message_id=synthesized_id,
                user_text=turn.user_text,
            )
        except Exception as exc:  # noqa: BLE001 — degrade silently
            logger.warning(
                "agent.resume.replay_enqueue_failed",
                turn_id=turn.turn_id,
                channel=turn.channel,
                error=str(exc),
            )
            return False
        if inbox_id <= 0:
            return False
        logger.info(
            "agent.resume.replay_enqueued",
            turn_id=turn.turn_id,
            channel=turn.channel,
            session_key=turn.session_key,
            inbox_id=inbox_id,
            message_id=synthesized_id,
        )
        return True


class AgentResumeService:
    """Boot-time orchestrator: scan the journal + drive re-delivery.

    Wired by :func:`corlinman_server.main._serve` (and by
    :mod:`corlinman_server.gateway.grpc.agent_server` if the gateway
    co-hosts the agent) AFTER the gRPC server starts but BEFORE the
    first RPC is accepted, so a re-delivery can land on the freshly
    booted servicer immediately.

    The orchestrator is small on purpose: scan → categorise → log. The
    actual re-delivery is delegated to :class:`BootReplayDispatcher`
    (cross-channel) or skipped for channels that own their own drain.
    """

    __slots__ = (
        "_journal",
        "_inbox",
        "_window_ms",
        "_stale_cutoff_s",
        "_dispatcher",
    )

    def __init__(
        self,
        journal: AgentJournal,
        inbox: Inbox | None,
        *,
        window_ms: int = DEFAULT_RESUME_WINDOW_MS,
        stale_cutoff_s: int = DEFAULT_STALE_CUTOFF_S,
    ) -> None:
        self._journal = journal
        self._inbox = inbox
        self._window_ms = int(window_ms)
        self._stale_cutoff_s = int(stale_cutoff_s)
        self._dispatcher: BootReplayDispatcher | None = (
            BootReplayDispatcher(inbox) if inbox is not None else None
        )

    async def run(self) -> ResumeScanReport:
        """Execute one scan pass and return the structured report.

        Order matters:

        1. Stale-sweep first so deeply abandoned rows (24h+) get
           cleared to ``errored`` — they will NOT appear in the next
           ``list_resumable_in_progress`` call.
        2. Then list everything within the recency window.
        3. Categorise each row by channel; for cross-channel rows that
           need boot-replay, enqueue a synthesized inbox entry.

        Always emits the ``agent.resume.scan_complete`` log line at
        the end — operators grep for it to confirm the scanner fired.
        """
        swept = 0
        try:
            swept = await self._journal.mark_stale_in_progress_as_errored(
                older_than_seconds=self._stale_cutoff_s
            )
        except Exception as exc:  # noqa: BLE001 — never block boot
            logger.warning(
                "agent.resume.stale_sweep_failed", error=str(exc)
            )

        try:
            turns: list[InProgressTurn] = (
                await self._journal.list_resumable_in_progress(
                    window_ms=self._window_ms
                )
            )
        except Exception as exc:  # noqa: BLE001 — never block boot
            logger.warning(
                "agent.resume.list_failed", error=str(exc)
            )
            turns = []

        resumed = 0
        enqueued_no_drain = 0
        skipped = 0
        for turn in turns:
            channel = (turn.channel or "").lower()
            if not channel:
                # HTTP turn (or pre-channel-column legacy row) — no
                # re-delivery surface; the user must resend. Log so
                # the operator can spot the gap.
                logger.info(
                    "agent.resume.unsupported_channel",
                    session=turn.session_key,
                    channel="<none>",
                    turn_id=turn.turn_id,
                )
                skipped += 1
                continue
            if channel in _CHANNEL_HAS_OWN_DRAIN:
                # QQ-family: the inbox drainer + channel reconnect
                # already cover re-delivery. Skip without enqueueing.
                logger.info(
                    "agent.resume.channel_owns_drain",
                    session=turn.session_key,
                    channel=channel,
                    turn_id=turn.turn_id,
                )
                skipped += 1
                continue
            if channel in _CHANNEL_NEEDS_BOOT_REPLAY:
                if self._dispatcher is None:
                    # Inbox unavailable (e.g. a stripped-down test
                    # rig). Log + skip — the user can resend.
                    logger.info(
                        "agent.resume.unsupported_channel",
                        session=turn.session_key,
                        channel=channel,
                        turn_id=turn.turn_id,
                        reason="no_inbox",
                    )
                    skipped += 1
                    continue
                if await self._dispatcher.replay(turn):
                    if channel in _CHANNEL_HAS_BOOT_REPLAY_DRAIN:
                        # A dispatch loop exists for this channel; the
                        # synthesized row WILL be drained on next poll.
                        # This is a genuine resume.
                        resumed += 1
                    else:
                        # Row enqueued, but NO drainer polls this
                        # channel's inbox (see _CHANNEL_NEEDS_BOOT_REPLAY
                        # docstring). Do NOT report it as resumed — that
                        # would be a false-positive. Park + warn so the
                        # operator knows the message is waiting on a
                        # drainer that doesn't exist yet.
                        enqueued_no_drain += 1
                        logger.warning(
                            "agent.resume.enqueued_no_drain",
                            session=turn.session_key,
                            channel=channel,
                            turn_id=turn.turn_id,
                            reason="no_dispatch_loop_drains_this_channel",
                        )
                else:
                    skipped += 1
                continue
            # Unknown channel — log as unsupported so operators
            # notice the gap when they add a new channel.
            logger.info(
                "agent.resume.unsupported_channel",
                session=turn.session_key,
                channel=channel,
                turn_id=turn.turn_id,
            )
            skipped += 1

        window_minutes = max(1, self._window_ms // 60_000)
        logger.info(
            "agent.resume.scan_complete",
            found=len(turns),
            resumed=resumed,
            enqueued_no_drain=enqueued_no_drain,
            skipped=skipped,
            window_minutes=window_minutes,
        )

        return ResumeScanReport(
            found=len(turns),
            resumed=resumed,
            enqueued_no_drain=enqueued_no_drain,
            skipped=skipped,
            swept=swept,
            window_ms=self._window_ms,
            turns=tuple(turns),
        )


async def run_boot_auto_resume(
    journal: AgentJournal,
    inbox: Inbox | None,
    *,
    window_ms: int = DEFAULT_RESUME_WINDOW_MS,
    stale_cutoff_s: int = DEFAULT_STALE_CUTOFF_S,
) -> ResumeScanReport:
    """Convenience entrypoint — construct and run the service in one
    call. Used by :func:`corlinman_server.main._serve` so the boot path
    stays a single ``await`` line.
    """
    service = AgentResumeService(
        journal,
        inbox,
        window_ms=window_ms,
        stale_cutoff_s=stale_cutoff_s,
    )
    return await service.run()


async def open_inbox_for_boot_resume(data_dir_path: Any) -> Inbox | None:
    """Best-effort lazy open of the inbox under ``<data_dir>/inbox.sqlite``.

    Mirrors the contract in :func:`corlinman_channels.service._try_open_inbox`
    so a boot-time scan opens the SAME on-disk file the channels will use
    once they start. Returns ``None`` (and logs a warning) on any open
    failure — the auto-resume service treats ``None`` as "no boot-replay
    surface available" and falls back to logging the channel as
    unsupported.

    ``data_dir_path`` is typed ``Any`` so callers can pass either a
    :class:`pathlib.Path` or a string without the type-checker complaining.
    """
    from pathlib import Path  # local import: keep top-level fast

    try:
        root = Path(data_dir_path)
        root.mkdir(parents=True, exist_ok=True)
        return await Inbox.open(root / "inbox.sqlite")
    except Exception as exc:  # noqa: BLE001 — degrade silently
        logger.warning("agent.resume.inbox_open_failed", error=str(exc))
        return None


__all__ = [
    "AgentResumeService",
    "BootReplayDispatcher",
    "DEFAULT_RESUME_WINDOW_MS",
    "DEFAULT_STALE_CUTOFF_S",
    "ResumeScanReport",
    "open_inbox_for_boot_resume",
    "run_boot_auto_resume",
]


# Re-export the channel coverage sets so tests can reference them
# without poking module privates.
CHANNEL_HAS_OWN_DRAIN: frozenset[str] = _CHANNEL_HAS_OWN_DRAIN
CHANNEL_NEEDS_BOOT_REPLAY: frozenset[str] = _CHANNEL_NEEDS_BOOT_REPLAY
CHANNEL_HAS_BOOT_REPLAY_DRAIN: frozenset[str] = _CHANNEL_HAS_BOOT_REPLAY_DRAIN


def _iter_unsupported_channels(turns: Iterable[InProgressTurn]) -> list[str]:
    """Diagnostic helper: list channel ids the scanner cannot resume.

    Useful for future ``/admin/resume`` route to surface the coverage
    gap; not used by the scanner itself.
    """
    out: list[str] = []
    for t in turns:
        c = (t.channel or "").lower()
        if c and c not in _CHANNEL_HAS_OWN_DRAIN and c not in _CHANNEL_NEEDS_BOOT_REPLAY:
            out.append(c)
    return out
