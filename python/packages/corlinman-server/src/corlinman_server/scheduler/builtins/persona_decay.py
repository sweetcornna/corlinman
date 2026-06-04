"""``persona.decay`` builtin — R2 persona-liveness.

Wraps :func:`corlinman_persona.decay.apply_decay` (driven through the
same per-row elapsed-hours math as ``corlinman-persona decay-once``) in
the scheduler-builtin contract so the gateway's cron loop sweeps
mood / fatigue / recent-topics decay on its own cadence — *in-process*,
not by shelling out to the CLI. Without this nothing ever schedules
decay, so a fresh install's mood stays ``"neutral"`` and fatigue stays
``0.0`` forever.

Behaviour matrix (mirrors
:mod:`corlinman_server.scheduler.builtins.evolution_darwin_curate`):

* No ``data_dir`` reachable from ``app_state`` → builtin returns
  ``{"ok": False, "reason": "data_dir_unavailable"}``. Same envelope
  shape so the scheduler history surfaces *why* the sweep skipped
  rather than logging a stack trace.
* ``corlinman_persona`` not importable (stubbed test fixture, partial
  install) → ``{"ok": False, "reason": "deps_unavailable: ..."}``.
* SQLite locked / permission denied → caught, returned as
  ``{"ok": False, "reason": "store_open_failed: ..."}``.

The actual decay math is the deterministic
:func:`corlinman_persona.decay.apply_decay` pure function; this wrapper
only resolves the ``agent_state.sqlite`` path off ``app_state``, opens a
:class:`~corlinman_persona.store.PersonaStore`, sweeps every row, and
forwards a count of how many rows changed.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Any

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger("corlinman_server.scheduler.builtins.persona_decay")


#: Builtin name used in ``JobAction.run_tool(plugin="persona", tool="decay")``.
#: The dot-joined form matches the scheduler's ``<plugin>.<tool>`` convention.
PERSONA_DECAY_BUILTIN_NAME: str = "persona.decay"

#: ``state_json`` key holding the topic-aging anchor (unix ms). The
#: fatigue clock rides ``updated_at_ms``, which we restamp to "now" on
#: every sweep so fatigue recovery never double-counts. The topic clock
#: must instead accumulate across sweeps — ``recent_topics`` ages off a
#: per-*day* rule, and an hourly sweep restamping the fatigue clock would
#: pin ``floor(hours/24)`` at 0 forever, so topics would never age out.
#: We anchor the topic clock separately here and only advance it by whole
#: days actually consumed, preserving the sub-day remainder.
_TOPIC_DECAY_ANCHOR_KEY: str = "_topic_decay_anchor_ms"


__all__ = [
    "PERSONA_DECAY_BUILTIN_NAME",
    "_persona_decay_action",
]


def _resolve_data_dir(context: BuiltinContext) -> Path | None:
    """Find the gateway's writable data dir on ``app_state``.

    Same three-probe pattern :func:`_evolution_darwin_curate_action`'s
    resolver uses — falls through ``app_state`` → ``admin_state`` so a
    degraded boot that only landed one of them still discovers the path.
    """
    for owner in (context.app_state, context.admin_state):
        if owner is None:
            continue
        raw = getattr(owner, "data_dir", None)
        if raw is None:
            continue
        if isinstance(raw, Path):
            return raw
        return Path(str(raw))
    return None


async def _persona_decay_action(
    context: BuiltinContext,
) -> dict[str, Any]:
    """Sweep every persona row in ``agent_state.sqlite``, apply
    :func:`apply_decay` against the elapsed wall-time since each row's
    ``updated_at_ms``, and write the result back. Returns a small report
    the scheduler history persists verbatim.

    This mirrors the in-process logic in
    :func:`corlinman_persona.cli._run_decay` rather than spawning the
    ``corlinman-persona decay-once`` subprocess: the scheduler tick loop
    is long-lived and we'd rather open the same async store the resolver
    already uses than fork a CLI process on every fire.
    """
    data_dir = _resolve_data_dir(context)
    if data_dir is None:
        return {"ok": False, "reason": "data_dir_unavailable"}

    # Lazy imports — ``corlinman_persona`` isn't on every test fixture's
    # PYTHONPATH (gateway-side tests stub the scheduler) and we'd rather
    # degrade than crash on a missing dep.
    try:
        from corlinman_persona.decay import (  # noqa: PLC0415
            DecayConfig,
            apply_decay,
        )
        from corlinman_persona.state import PersonaState  # noqa: PLC0415
        from corlinman_persona.store import (  # noqa: PLC0415
            DEFAULT_TENANT_ID,
            PersonaStore,
        )
    except ImportError as exc:
        return {"ok": False, "reason": f"deps_unavailable: {exc}"}

    state_db = data_dir / "agent_state.sqlite"
    config = DecayConfig()
    now_ms = int(time.time() * 1000)
    tenant_id = DEFAULT_TENANT_ID

    try:
        async with PersonaStore(state_db) as store:
            rows = await store.list_all(tenant_id=tenant_id)
            changed = 0
            for row in rows:
                # Fatigue clock: elapsed since the last sweep. We restamp
                # ``updated_at_ms`` to "now" below so this never
                # double-counts hourly recovery.
                fatigue_hours = max(0.0, (now_ms - row.updated_at_ms) / 3_600_000.0)

                # Topic clock: a separate anchor that only advances by
                # whole days consumed, so the per-day topic drop survives
                # frequent sweeps that keep restamping the fatigue clock.
                # Fall back to ``updated_at_ms`` (then "now") for rows
                # written before this anchor existed.
                anchor_ms = (
                    row.state_json.get(_TOPIC_DECAY_ANCHOR_KEY)
                    or row.updated_at_ms
                    or now_ms
                )
                topic_hours = max(0.0, (now_ms - anchor_ms) / 3_600_000.0)

                decayed = apply_decay(
                    row,
                    fatigue_hours,
                    config,
                    topic_hours_elapsed=topic_hours,
                )

                # Advance the anchor only by the whole days actually
                # consumed, preserving the sub-day remainder so topics
                # keep aging toward the next day boundary.
                days_dropped = math.floor(topic_hours / 24.0)
                if days_dropped >= 1:
                    new_anchor_ms = anchor_ms + days_dropped * 24 * 3_600_000
                else:
                    new_anchor_ms = anchor_ms
                new_state_json = dict(decayed.state_json)
                new_state_json[_TOPIC_DECAY_ANCHOR_KEY] = new_anchor_ms

                new_state = PersonaState(
                    agent_id=decayed.agent_id,
                    mood=decayed.mood,
                    fatigue=decayed.fatigue,
                    recent_topics=decayed.recent_topics,
                    # Stamp "now" so the next sweep doesn't double-count
                    # the elapsed hours on the fatigue clock.
                    updated_at_ms=now_ms,
                    state_json=new_state_json,
                )
                await store.upsert(new_state, tenant_id=tenant_id)
                if (
                    new_state.mood != row.mood
                    or new_state.fatigue != row.fatigue
                    or new_state.recent_topics != row.recent_topics
                ):
                    changed += 1
    except Exception as exc:  # noqa: BLE001 - never raise out of builtin
        _logger.warning(
            "scheduler.builtin.persona_decay.failed",
            extra={"error": repr(exc)},
        )
        return {"ok": False, "reason": f"store_open_failed: {exc!r}"}

    return {
        "ok": True,
        "state_db": str(state_db),
        "rows_scanned": len(rows),
        "rows_changed": changed,
    }


# Module-load-time registration so the package ``__init__`` import is
# all that's required to wire the builtin. Tests that monkeypatch the
# registry can simply replace ``BUILTIN_ACTIONS[PERSONA_DECAY_BUILTIN_NAME]``
# without redoing this dance.
register_builtin(
    PERSONA_DECAY_BUILTIN_NAME,
    _persona_decay_action,
)
