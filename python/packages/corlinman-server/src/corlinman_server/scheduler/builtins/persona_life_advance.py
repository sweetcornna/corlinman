"""``persona.life_advance`` builtin — autonomous daily life-beat for personas.

Without this, a persona's life-state only changes when the model explicitly
calls ``persona_life_set_state`` (i.e. during a live chat). With it, every
persona-state row receives a fresh, coherent life beat drawn **without an LLM
call** — one activity + a matching location / companion drawn from the *same*
seed pack via the existing :func:`corlinman_agent.persona.life._resolve_seed_library`
resolution chain (operator override → bundled pack → generic).

Behaviour matrix (mirrors
:mod:`corlinman_server.scheduler.builtins.persona_decay`):

* No ``data_dir`` on ``app_state`` → ``{"ok": False, "reason": "data_dir_unavailable"}``.
* ``corlinman_persona`` / ``corlinman_agent`` not importable → ``{"ok": False,
  "reason": "deps_unavailable: ..."}``.
* SQLite locked / permission denied → ``{"ok": False, "reason": "store_open_failed: ..."}``
  (caught, never raised out of the builtin).

Default-off gate
----------------
The builtin is **always registered** at import time (same as every builtin).
The *default scheduler job* is only appended to
``app.state.corlinman_default_scheduler_jobs`` when the gateway config carries
``[persona.life_advance] enabled = true``.  The companion helper that does
this is :func:`~corlinman_server.gateway.lifecycle.scheduler_integration._register_default_persona_life_advance_job`.

Draw logic (simple, no LLM)
-----------------------------
For each persona row:

1. Resolve the effective seed library for that row's ``agent_id`` (the slug
   used by the persona system as ``persona_id``).
2. Randomly pick one *activity* from whichever of
   ``academy_scene`` / ``mission_scenario`` / ``travel_destination`` is
   non-empty (preference order: academy_scene, mission_scenario,
   travel_destination).
3. From the **same** seed pack draw one companion from ``companion`` and one
   weather note from ``weather`` (both fall back gracefully when absent).
4. Choose a life-state bucket coherently with the source category
   (``academy_scene`` → ``at_academy``; ``mission_scenario`` → ``on_mission``;
   ``travel_destination`` → ``traveling``).
5. Write: ``state_json["life"]["current"]`` ← the new beat;
   push the activity onto ``recent_topics``; optionally append a one-line
   auto diary beat (``state_json["diary"]``).

All writes use the same ``_mirror_placeholder_keys`` path the model-driven
tools use, so ``{{persona.life_activity}}`` etc. in system prompts stay
consistent.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger("corlinman_server.scheduler.builtins.persona_life_advance")

#: Builtin name used in ``JobAction.run_tool(plugin="persona", tool="life_advance")``.
PERSONA_LIFE_ADVANCE_BUILTIN_NAME: str = "persona.life_advance"

#: Hard cap on diary entries kept in state_json — mirrors the agent-side cap.
_MAX_DIARY_ENTRIES: int = 200
#: Hard cap on life history entries.
_MAX_HISTORY_ENTRIES: int = 100

__all__ = [
    "PERSONA_LIFE_ADVANCE_BUILTIN_NAME",
    "_persona_life_advance_action",
]


def _resolve_data_dir(context: BuiltinContext) -> Path | None:
    """Find the gateway's writable data dir on ``app_state``.

    Same three-probe pattern the persona_decay builtin uses.
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


def _now_iso() -> str:
    """UTC-aware ISO timestamp, same helper the agent-side life tools use."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _trim(lst: list[Any], cap: int) -> list[Any]:
    """Cap a list to its last *cap* entries."""
    return lst[-cap:] if len(lst) > cap else lst


def _draw_life_beat(
    seed_lib: dict[str, list[str]],
) -> dict[str, Any]:
    """Draw a single coherent life beat from *seed_lib*.

    Preference order for the activity source:
      1. ``academy_scene``  → life state ``"at_academy"``
      2. ``mission_scenario`` → life state ``"on_mission"``
      3. ``travel_destination`` → life state ``"traveling"``
      4. fallback when none have entries → generic ``"at_academy"`` / ``"日常"``

    Companion + weather are drawn from the same library instance so the
    persona's specific lore (e.g. Grantley's named companions) is used
    rather than the generic fallback.
    """
    rng = random.Random()

    _PRIORITY: list[tuple[str, str, str]] = [
        ("academy_scene", "at_academy", "location"),
        ("mission_scenario", "on_mission", "location"),
        ("travel_destination", "traveling", "travel_destination"),
    ]

    activity = "日常"
    life_state = "at_academy"
    location = ""

    for category, bucket, _loc_hint in _PRIORITY:
        choices = seed_lib.get(category) or []
        if choices:
            activity = rng.choice(choices)
            life_state = bucket
            # For travel, the category *is* the location pool.
            if category == "travel_destination":
                location = activity
                # Re-draw activity from mission or academy if available.
                for alt_cat in ("mission_scenario", "academy_scene"):
                    alt = seed_lib.get(alt_cat) or []
                    if alt:
                        activity = rng.choice(alt)
                        break
            break

    companions_pool = seed_lib.get("companion") or []
    companion = rng.choice(companions_pool) if companions_pool else ""
    companions = [companion] if companion and companion != "独自一人" else []

    weather_pool = seed_lib.get("weather") or []
    weather = rng.choice(weather_pool) if weather_pool else ""

    mood_pool = seed_lib.get("mood") or []
    mood = rng.choice(mood_pool) if mood_pool else ""

    return {
        "life_state": life_state,
        "location": location,
        "activity": activity,
        "companions": companions,
        "weather": weather,
        "mood": mood,
    }


async def _persona_life_advance_action(
    context: BuiltinContext,
) -> dict[str, Any]:
    """Advance every persona row with a fresh, coherent life beat.

    Opens ``agent_state.sqlite`` off ``app_state.data_dir``, iterates every
    row, resolves its effective seed library, draws one beat (no LLM), and
    writes the update back — mirroring the same ``state_json["life"]`` /
    ``recent_topics`` / ``state_json["diary"]`` path the model-driven tools
    use so ``{{persona.life_activity}}`` stays current.
    """
    data_dir = _resolve_data_dir(context)
    if data_dir is None:
        return {"ok": False, "reason": "data_dir_unavailable"}

    # Lazy imports so the module loads without corlinman_persona / corlinman_agent
    # on test paths that only exercise the registry surface.
    try:
        from corlinman_agent.persona.life import (  # noqa: PLC0415
            _empty_life,
            _mirror_placeholder_keys,
            _resolve_seed_library,
        )
        from corlinman_persona.state import PersonaState  # noqa: PLC0415
        from corlinman_persona.store import (  # noqa: PLC0415
            DEFAULT_TENANT_ID,
            PersonaStore,
        )
    except ImportError as exc:
        return {"ok": False, "reason": f"deps_unavailable: {exc}"}

    state_db = data_dir / "agent_state.sqlite"
    now_ms = int(time.time() * 1000)
    tenant_id = DEFAULT_TENANT_ID

    try:
        async with PersonaStore(state_db) as store:
            rows = await store.list_all(tenant_id=tenant_id)
            changed = 0
            for row in rows:
                # Resolve the effective seed library keyed by agent_id
                # (the persona slug used by life.py).
                seed_lib = _resolve_seed_library(row.agent_id, data_dir)
                beat = _draw_life_beat(seed_lib)

                # ---- Repair / initialise life document ----
                sj: dict[str, Any] = (
                    row.state_json if isinstance(row.state_json, dict) else {}
                )
                life: dict[str, Any] = sj.get("life")  # type: ignore[assignment]
                if not isinstance(life, dict):
                    life = _empty_life()
                else:
                    if not isinstance(life.get("current"), dict):
                        life["current"] = _empty_life()["current"]
                    if not isinstance(life.get("history"), list):
                        life["history"] = []

                # ---- Archive current → history if it changed ----
                old_current: dict[str, Any] = dict(life.get("current") or {})
                history: list[Any] = list(life.get("history") or [])
                now_ts = _now_iso()
                new_current = {
                    "state": beat["life_state"],
                    "location": beat["location"],
                    "activity": beat["activity"],
                    "companions": beat["companions"],
                    "mood": beat["mood"],
                    "weather": beat["weather"],
                    "since": now_ts,
                    "until_estimate": None,
                    "story_arc": old_current.get("story_arc"),  # preserve arc
                }
                diff_keys = [
                    k
                    for k in ("state", "activity", "location")
                    if new_current.get(k) != old_current.get(k, "")
                ]
                if diff_keys and old_current:
                    history.append(
                        {
                            "ts": now_ts,
                            "from": old_current,
                            "to": new_current,
                            "reason": "auto_daily_advance",
                        }
                    )
                life["current"] = new_current
                life["history"] = _trim(history, _MAX_HISTORY_ENTRIES)

                # ---- Mirror placeholder keys ----
                _mirror_placeholder_keys(sj, life)
                sj["life"] = life

                # ---- Optional auto diary beat ----
                diary: list[Any] = sj.get("diary") if isinstance(sj.get("diary"), list) else []  # type: ignore[assignment]
                companion_note = (
                    f"同行: {beat['companions'][0]}" if beat["companions"] else "独自一人"
                )
                auto_entry = {
                    "ts": now_ts,
                    "entry": (
                        f"[每日自动] {beat['activity']}"
                        + (f" @ {beat['location']}" if beat["location"] else "")
                        + f" — {companion_note}"
                        + (f" ({beat['weather']})" if beat["weather"] else "")
                    ),
                    "tag": "auto_advance",
                    "mood": beat["mood"],
                    "location": beat["location"],
                }
                diary.append(auto_entry)
                sj["diary"] = _trim(list(diary), _MAX_DIARY_ENTRIES)

                # ---- Push activity to recent_topics ----
                activity_str = beat["activity"].strip()
                new_topics = (
                    [*row.recent_topics, activity_str] if activity_str else row.recent_topics
                )

                # ---- Mirror mood onto native column ----
                new_mood = beat["mood"] if beat["mood"] else row.mood

                new_state = PersonaState(
                    agent_id=row.agent_id,
                    mood=new_mood,
                    fatigue=row.fatigue,
                    recent_topics=new_topics,
                    updated_at_ms=now_ms,
                    state_json=sj,
                )
                await store.upsert(new_state, tenant_id=tenant_id)
                changed += 1

    except Exception as exc:  # noqa: BLE001 — never raise out of builtin
        _logger.warning(
            "scheduler.builtin.persona_life_advance.failed",
            extra={"error": repr(exc)},
        )
        return {"ok": False, "reason": f"store_open_failed: {exc!r}"}

    return {
        "ok": True,
        "state_db": str(state_db),
        "rows_scanned": len(rows),
        "rows_changed": changed,
    }


# Module-load-time registration — the package ``__init__`` import is all
# that is required to wire the builtin into BUILTIN_ACTIONS. The *default
# scheduler job* (daily) is only appended to app.state when the config flag
# ``[persona.life_advance] enabled = true`` is set; see
# scheduler_integration._register_default_persona_life_advance_job.
register_builtin(
    PERSONA_LIFE_ADVANCE_BUILTIN_NAME,
    _persona_life_advance_action,
)
