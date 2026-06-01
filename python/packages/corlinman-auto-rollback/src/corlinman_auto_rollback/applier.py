"""``EvolutionApplier`` — concrete forward-apply + revert driver.

The Python sibling of the Rust ``corlinman-gateway`` ``EvolutionApplier``
(see ``rust/crates/corlinman-gateway/src/evolution_applier.rs`` in git
history). The Rust applier additionally drives ``kb.sqlite`` chunk
mutations and on-disk tenant prompt/skill files; the Python ``AdminState``
only carries the evolution store handle, so this port is **store-backed**:
every apply / rollback is a transition against ``evolution.sqlite`` plus
its audit tables. The kb / filesystem mutators stay a follow-up — they
need handles the gateway does not yet thread into ``AdminState``.

What the store-backed applier still guarantees, end-to-end:

* :meth:`apply` gates ``status == approved``, opens an
  ``apply_intent_log`` row, captures a reversible snapshot of the
  proposal's pre-apply state into ``evolution_history.inverse_diff``,
  flips ``status → applied`` (+ ``applied_at``), and stamps the intent
  log committed / failed.
* :meth:`revert` gates ``status == applied``, reads the audit row,
  restores the captured pre-apply ``status``, and stamps the rollback
  audit fields on both the history row and the proposal.

:meth:`revert` satisfies the :class:`~corlinman_auto_rollback.revert.Applier`
protocol, so an :class:`EvolutionApplier` can be handed straight to the
:class:`~corlinman_auto_rollback.monitor.AutoRollbackMonitor`.

Errors are typed: forward failures raise :class:`ApplyError` subclasses;
revert failures raise the shared :class:`RevertError` set so the monitor
and the admin route can switch on the concrete subclass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from corlinman_evolution_store import (
    EvolutionHistory,
    EvolutionProposal,
    EvolutionStatus,
    HistoryRepo,
    IntentLogRepo,
    NotFoundError,
    ProposalId,
    ProposalsRepo,
    RepoError,
)

from corlinman_auto_rollback.config import (
    AutoRollbackThresholds,
    EvolutionAutoRollbackConfig,
)
from corlinman_auto_rollback.metrics import (
    MetricSnapshot,
    capture_snapshot,
    watched_event_kinds,
)
from corlinman_auto_rollback.revert import (
    HistoryMissingRevertError,
    InternalRevertError,
    NotAppliedRevertError,
    NotFoundRevertError,
    RevertError,
    UnsupportedKindRevertError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Forward-apply error set. Mirrors the Rust ``ApplyError`` variants the
# gateway route maps onto 4xx/5xx envelopes. Revert errors reuse the
# shared :class:`RevertError` hierarchy from :mod:`.revert`.
# ---------------------------------------------------------------------------


class ApplyError(Exception):
    """Base for every typed failure mode of :meth:`EvolutionApplier.apply`.

    The admin route switches on the concrete subclass to pick the HTTP
    status + JSON envelope, so adding a variant is intentionally
    breaking — mirrors the Rust ``ApplyError`` enum.
    """


class NotFoundApplyError(ApplyError):
    """Proposal id wasn't in ``evolution_proposals``."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"proposal not found: {proposal_id}")
        self.proposal_id = proposal_id


class NotApprovedApplyError(ApplyError):
    """Proposal exists but isn't in ``approved``. Carries the actual
    status string so the route can rebuild the ``invalid_state_transition``
    envelope the approve / deny routes already emit."""

    def __init__(self, status: str) -> None:
        super().__init__(f"proposal not approved (status={status})")
        self.status = status


class UnsupportedKindApplyError(ApplyError):
    """Kind has no forward handler. Every kind is store-applicable in
    this port, so this is reserved for the future kb / filesystem
    handlers — kept in the surface so the route's envelope set is
    stable across the follow-up."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"kind {kind} cannot be applied yet")
        self.kind = kind


class InternalApplyError(ApplyError):
    """Anything the applier couldn't classify above (storage error,
    malformed proposal row, transaction failure). The route logs +
    returns 500; an operator inspects the gateway logs."""

    def __init__(self, message: str) -> None:
        super().__init__(f"apply failed: {message}")
        self.message = message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Unix milliseconds. Matches the Rust ``now_ms`` helper."""
    return int(time.time() * 1000)


def _sha256_hex(text: str) -> str:
    """SHA-256 of ``text`` (UTF-8) as lowercase hex — matches the Rust
    ``sha256_hex`` helper used for the history ``before_sha`` /
    ``after_sha`` columns."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Marker the apply path writes into ``inverse_diff`` so :meth:`revert`
# can recognise a store-backed audit row and refuse anything else.
_INVERSE_ACTION = "restore_proposal_state"


def _build_inverse_diff(proposal: EvolutionProposal) -> str:
    """Capture the proposal's pre-apply state as a JSON ``inverse_diff``.

    The store-backed revert path restores ``status`` (and the decision
    audit fields) from this blob. Keeping the full pre-apply triple here
    means a revert is a pure function of the history row — no second
    read of a possibly-since-mutated proposal."""
    prior_status = (
        proposal.status.as_str()
        if hasattr(proposal.status, "as_str")
        else str(proposal.status)
    )
    return json.dumps(
        {
            "action": _INVERSE_ACTION,
            "prior_status": prior_status,
            "prior_decided_at": proposal.decided_at,
            "prior_decided_by": proposal.decided_by,
        }
    )


# ---------------------------------------------------------------------------
# Applier
# ---------------------------------------------------------------------------


class EvolutionApplier:
    """Forward-apply + revert driver over a single ``evolution.sqlite``.

    Constructed from the shared :class:`~corlinman_evolution_store.EvolutionStore`
    connection. The gateway holds one instance and the admin
    ``/apply`` + ``/rollback`` routes call straight into it; the
    :class:`~corlinman_auto_rollback.monitor.AutoRollbackMonitor` calls
    :meth:`revert` through the :class:`~corlinman_auto_rollback.revert.Applier`
    protocol.
    """

    def __init__(
        self,
        conn: object,
        config: EvolutionAutoRollbackConfig | None = None,
    ) -> None:
        """``conn`` is the shared :class:`aiosqlite.Connection` (or any
        handle the three repos accept). Construct it from
        ``EvolutionStore.conn``.

        ``config`` carries the AutoRollback thresholds — only its
        ``signal_window_secs`` is used here, to capture the apply-time
        metrics baseline over the *same* window length the monitor will
        re-sample with (a symmetric window prevents a false breach from
        sample-window mismatch). When omitted (e.g. the manual
        operator-action admin route, which doesn't yet thread the
        config), the conservative default thresholds apply so the
        baseline is still a valid, monitor-decodable snapshot rather
        than the empty ``{}`` that the strict decoder rejects."""
        self._conn = conn
        self._proposals = ProposalsRepo(conn)  # type: ignore[arg-type]
        self._history = HistoryRepo(conn)  # type: ignore[arg-type]
        self._intent_log = IntentLogRepo(conn)  # type: ignore[arg-type]
        self._thresholds: AutoRollbackThresholds = (
            config.thresholds if config is not None else AutoRollbackThresholds()
        )

    # -- forward apply ------------------------------------------------------

    async def apply(self, proposal_id: ProposalId) -> EvolutionHistory:
        """Apply an ``approved`` proposal.

        Returns the freshly-inserted :class:`EvolutionHistory` row (with
        autoincrement ``id`` populated). On any failure the proposal
        stays ``approved`` and no history row lands.

        Raises:
            NotFoundApplyError: ``proposal_id`` isn't in the table.
            NotApprovedApplyError: row exists but isn't ``approved``.
            InternalApplyError: storage / transaction failure.
        """
        proposal = await self._load_proposal_for_apply(proposal_id)

        # Open the intent log only for proposals that survived the
        # gates — a NotFound / NotApproved must not litter the table.
        intent_at = _now_ms()
        try:
            intent_id = await self._intent_log.record_intent(
                str(proposal_id),
                proposal.kind.as_str(),
                proposal.target,
                intent_at,
            )
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalApplyError(f"intent log open: {exc}") from exc

        try:
            history = await self._commit_apply(proposal_id, proposal)
        except ApplyError as exc:
            # Stamp the intent failed so the half-committed scan only
            # ever surfaces truly-stuck applies.
            await self._stamp_intent_failed(intent_id, str(exc))
            raise
        except Exception as exc:  # classify + re-raise typed
            await self._stamp_intent_failed(intent_id, str(exc))
            raise InternalApplyError(str(exc)) from exc

        # Log-only on a stamp failure: the apply itself succeeded.
        try:
            await self._intent_log.mark_committed(intent_id, _now_ms())
        except RepoError as exc:
            logger.warning(
                "apply succeeded but intent-log commit stamp failed"
                " (proposal_id=%s, error=%s)",
                proposal_id,
                exc,
            )
        return history

    async def _load_proposal_for_apply(
        self, proposal_id: ProposalId
    ) -> EvolutionProposal:
        try:
            proposal = await self._proposals.get(proposal_id)
        except NotFoundError as exc:
            raise NotFoundApplyError(str(proposal_id)) from exc
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalApplyError(f"proposal read: {exc}") from exc

        status_str = (
            proposal.status.as_str()
            if hasattr(proposal.status, "as_str")
            else str(proposal.status)
        )
        if proposal.status != EvolutionStatus.APPROVED:
            raise NotApprovedApplyError(status_str)
        return proposal

    async def _commit_apply(
        self,
        proposal_id: ProposalId,
        proposal: EvolutionProposal,
    ) -> EvolutionHistory:
        """Write the audit row + flip ``status → applied``.

        ``before_sha`` hashes the captured pre-apply snapshot;
        ``after_sha`` hashes the proposal diff that is now live —
        mirrors the Rust applier's content-hash columns so the History
        tab + AutoRollback can detect drift.
        """
        now = _now_ms()
        inverse_diff = _build_inverse_diff(proposal)
        metrics_baseline = await self._capture_baseline(proposal, now)
        history_row = EvolutionHistory(
            proposal_id=proposal_id,
            kind=proposal.kind,
            target=proposal.target,
            before_sha=_sha256_hex(inverse_diff),
            after_sha=_sha256_hex(proposal.diff),
            inverse_diff=inverse_diff,
            metrics_baseline=metrics_baseline,
            applied_at=now,
        )
        try:
            history_id = await self._history.insert(history_row)
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalApplyError(f"history insert: {exc}") from exc

        try:
            await self._proposals.mark_applied(proposal_id, now)
        except NotFoundError as exc:
            # The row vanished between the gate read and here — surface
            # it as NotFound rather than a generic 500.
            raise NotFoundApplyError(str(proposal_id)) from exc
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalApplyError(f"proposal status flip: {exc}") from exc

        history_row.id = history_id
        return history_row

    async def _capture_baseline(
        self, proposal: EvolutionProposal, now_ms: int
    ) -> dict:
        """Snapshot the proposal target's signal counts *before* the
        apply takes effect so the monitor has a real baseline to diff
        against inside the grace window.

        Mirrors the monitor's own capture: same per-kind whitelist
        (:func:`watched_event_kinds`) and the same
        ``signal_window_secs`` window length, so the two snapshots diff
        cleanly. A kind without a whitelist yields a snapshot with an
        empty ``counts`` dict (but a fully-populated, monitor-decodable
        envelope) — never the bare ``{}`` that the strict
        :meth:`MetricSnapshot.from_dict` decoder rejects.

        A capture failure (e.g. the signals table is missing) must not
        sink the whole apply: fall back to an empty-counts snapshot of
        the same shape, which is still a valid baseline (it just records
        "no pre-apply signals seen").
        """
        watched = watched_event_kinds(proposal.kind)
        try:
            snapshot = await capture_snapshot(
                self._conn,  # type: ignore[arg-type]
                proposal.target,
                watched,
                self._thresholds.signal_window_secs,
                now_ms,
            )
        except Exception as exc:  # capture is best-effort, baseline shape still valid
            logger.warning(
                "apply: baseline capture_snapshot failed; storing empty-counts"
                " baseline (proposal_id=%s, target=%s, error=%s)",
                proposal.id,
                proposal.target,
                exc,
            )
            snapshot = MetricSnapshot(
                target=proposal.target,
                captured_at_ms=now_ms,
                window_secs=self._thresholds.signal_window_secs,
                counts=dict.fromkeys(watched, 0),
            )
        return snapshot.to_dict()

    async def _stamp_intent_failed(self, intent_id: int, reason: str) -> None:
        try:
            await self._intent_log.mark_failed(intent_id, _now_ms(), reason)
        except RepoError as exc:  # best-effort audit stamp
            logger.warning(
                "intent-log fail stamp failed (intent_id=%s, error=%s)",
                intent_id,
                exc,
            )

    # -- revert -------------------------------------------------------------

    async def revert(self, proposal_id: ProposalId, reason: str) -> EvolutionHistory:
        """Revert an ``applied`` proposal.

        Restores the proposal's captured pre-apply ``status`` and stamps
        the rollback audit fields on the history row + the proposal.
        Returns the updated :class:`EvolutionHistory`.

        Satisfies the :class:`~corlinman_auto_rollback.revert.Applier`
        protocol (the protocol declares a ``-> None`` return; returning
        the richer history row is a compatible widening — the monitor
        ignores the value).

        Raises one of the :class:`~corlinman_auto_rollback.revert.RevertError`
        subclasses on failure.
        """
        # Gate on ``status == applied``. The proposal's other fields
        # aren't needed for the revert — the inverse_diff carries the
        # reversible state — so the return value is intentionally
        # dropped once the gate passes.
        await self._load_proposal_for_revert(proposal_id)

        try:
            history = await self._history.latest_for_proposal(proposal_id)
        except NotFoundError as exc:
            raise HistoryMissingRevertError(str(proposal_id)) from exc
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalRevertError(f"history read: {exc}") from exc

        prior_status = self._parse_inverse_diff(proposal_id, history)

        now = _now_ms()
        # Restore the proposal: flip status back to its pre-apply value
        # and stamp the rollback audit fields. ``mark_auto_rolled_back``
        # would force ``status = 'rolled_back'``; the operator/auto
        # rollback contract here is to return the proposal to the
        # decided state it held before apply so it can be re-applied
        # once the underlying issue is fixed.
        try:
            await self._restore_proposal(proposal_id, prior_status, now, reason)
        except NotFoundError as exc:
            # Lost the apply race — another revert already moved it.
            raise NotAppliedRevertError("rolled_back") from exc
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalRevertError(f"proposal restore: {exc}") from exc

        try:
            await self._history.mark_rolled_back(proposal_id, now, reason)
        except NotFoundError as exc:
            raise HistoryMissingRevertError(str(proposal_id)) from exc
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalRevertError(f"history rollback stamp: {exc}") from exc

        history.rolled_back_at = now
        history.rollback_reason = reason
        return history

    async def _load_proposal_for_revert(
        self, proposal_id: ProposalId
    ) -> EvolutionProposal:
        try:
            proposal = await self._proposals.get(proposal_id)
        except NotFoundError as exc:
            raise NotFoundRevertError(str(proposal_id)) from exc
        except Exception as exc:  # RepoError + raw sqlite errors
            raise InternalRevertError(f"proposal read: {exc}") from exc

        status_str = (
            proposal.status.as_str()
            if hasattr(proposal.status, "as_str")
            else str(proposal.status)
        )
        if proposal.status != EvolutionStatus.APPLIED:
            # RolledBack lands here too — the monitor tells idempotent
            # re-fires apart from missing proposals via the status.
            raise NotAppliedRevertError(status_str)
        return proposal

    @staticmethod
    def _parse_inverse_diff(
        proposal_id: ProposalId,
        history: EvolutionHistory,
    ) -> EvolutionStatus:
        """Decode the captured pre-apply ``status`` from the history
        row's ``inverse_diff``. A malformed / foreign-shaped blob is
        data corruption — the forward apply must have written one of
        ours."""
        try:
            raw = json.loads(history.inverse_diff)
        except (TypeError, ValueError) as exc:
            raise InternalRevertError(
                f"malformed inverse_diff for {proposal_id}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("action") != _INVERSE_ACTION:
            raise UnsupportedKindRevertError(
                str(getattr(history.kind, "as_str", lambda: history.kind)())
            )
        prior = raw.get("prior_status")
        try:
            return EvolutionStatus.from_str(str(prior))
        except Exception as exc:  # ParseError -> InternalRevertError
            raise InternalRevertError(
                f"inverse_diff carried an unknown prior_status {prior!r}"
            ) from exc

    async def _restore_proposal(
        self,
        proposal_id: ProposalId,
        prior_status: EvolutionStatus,
        rolled_back_at_ms: int,
        reason: str,
    ) -> None:
        """Flip ``status`` back to ``prior_status`` and stamp the
        ``auto_rollback_*`` audit fields, guarded on the row still being
        ``applied`` so a double-revert race surfaces as
        :class:`NotFoundError` instead of a silent second rollback."""
        cursor = await self._conn.execute(  # type: ignore[attr-defined]
            "UPDATE evolution_proposals "
            "  SET status = ?, "
            "      auto_rollback_at = ?, "
            "      auto_rollback_reason = ?, "
            "      applied_at = NULL "
            "WHERE id = ? AND status = 'applied'",
            (prior_status.as_str(), rolled_back_at_ms, reason, str(proposal_id)),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()  # type: ignore[attr-defined]
        if affected == 0:
            raise NotFoundError(str(proposal_id))


# ``__all__`` lists the forward-apply surface plus the revert-error
# subset re-exported from :mod:`.revert` — keeping both here lets the
# admin route import the full envelope set from one module.
__all__ = [
    "ApplyError",
    "EvolutionApplier",
    "HistoryMissingRevertError",
    "InternalApplyError",
    "InternalRevertError",
    "NotAppliedRevertError",
    "NotApprovedApplyError",
    "NotFoundApplyError",
    "NotFoundRevertError",
    "RevertError",
    "UnsupportedKindApplyError",
    "UnsupportedKindRevertError",
]
