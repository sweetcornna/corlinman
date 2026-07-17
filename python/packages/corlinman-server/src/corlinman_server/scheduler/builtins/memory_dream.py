"""``memory.dream`` builtin — nightly reflection + persona diary (W8, N3).

The closing innovation: an affect-weighted generative replay that turns
the day's memories into (1) higher-order **reflections** grafted back
into the store as first-class memories, (2) a first-person **diary
entry** on the persona's real life record, (3) a small next-morning
**mood nudge**, and (4) gated **demotion** proposals for stale clutter.

No other system closes this loop: Letta's sleep-time compute reorganizes
context but has no persona, no affect weighting, no diary artifact, and
no eval-gated forgetting; generative-agents reflection isn't affect-
gated, bi-temporal, or coupled to a life state machine.

Safety rails (dreams are LLM-generated, so treat them as hypotheses):
- reflections land at **low trust (0.4)** with a mandatory ``derived_from``
  edge to real evidence — any reflection citing an item id that isn't in
  the sampled pool is REJECTED (anti-hallucination);
- the trust loop (W7) then governs them like any other memory;
- diary writes go through the existing capped ``persona_life_diary_add``
  path — the dream never touches persona internals directly;
- ``dry_run`` (default) reports the whole dream without writing anything.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
    resolve_data_dir,
)

_logger = logging.getLogger("corlinman_server.scheduler.builtins.memory_dream")

MEMORY_DREAM_BUILTIN_NAME: str = "memory.dream"

_DREAM_SYSTEM_PROMPT = (
    "You are the reflective, dreaming mind of a long-lived AI persona.\n"
    "Below are memories from recent conversations, each with an id.\n"
    "In the persona's own first-person voice, dream over them: notice\n"
    "patterns, form 1-3 higher-order REFLECTIONS, and write a short\n"
    "diary entry.\n\n"
    "Output ONLY a JSON object with these fields:\n"
    "- reflections: array (1-3) of {text, evidence} where text is a\n"
    "  concise insight and evidence is an array of memory ids it is\n"
    "  drawn from. EVERY id MUST appear in the memories below.\n"
    "- diary: a 2-4 sentence first-person diary entry (the persona\n"
    "  reflecting on the day). May be in the persona's language.\n"
    "- mood_delta: {e, p, a} each in [-0.3, 0.3] — how the day shifted\n"
    "  the persona's mood (Evaluation/Potency/Activity).\n"
    "- demote: array of memory ids that now feel stale / not worth\n"
    "  keeping prominent (may be empty).\n"
    "No markdown fences, no commentary."
)


def _dream_config(app_state: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "enabled": False,
        "dry_run": True,
        "lookback_hours": 36.0,
        "sample": 12,
        "persona_id": "grantley",
    }
    raw = getattr(app_state, "memory_dream_config", None)
    if isinstance(raw, dict):
        for key in ("enabled", "dry_run"):
            v = raw.get(key, cfg[key])
            if isinstance(v, bool):
                cfg[key] = v
        v = raw.get("lookback_hours", cfg["lookback_hours"])
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            cfg["lookback_hours"] = float(v)
        v = raw.get("sample", cfg["sample"])
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            cfg["sample"] = v
        v = raw.get("persona_id", cfg["persona_id"])
        if isinstance(v, str) and v.strip():
            cfg["persona_id"] = v.strip()
    return cfg


def _clamp(value: Any, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return 0.0


async def _write_diary(app_state: Any, persona_id: str, entry: str) -> bool:
    """Append the dream's diary entry via the existing persona path."""
    # Canonical AppState path is extras["persona_state_store"] (c2 wiring);
    # corlinman_persona_state_store is the FastAPI app.state name, kept as
    # a fallback for callers that pass that object.
    store = None
    extras = getattr(app_state, "extras", None)
    if isinstance(extras, dict):
        store = extras.get("persona_state_store")
    if store is None:
        store = getattr(app_state, "corlinman_persona_state_store", None)
    if store is None:
        return False
    try:
        from corlinman_agent.persona.life import dispatch_persona_life_diary_add

        await dispatch_persona_life_diary_add(
            args_json=json.dumps(
                {"entry": entry, "tag": "dream", "mood": "梦"}
            ),
            persona_id=persona_id,
            state_store=store,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — diary is best-effort
        _logger.warning("memory.dream diary write failed: %s", exc)
        return False


async def _memory_dream_action(context: BuiltinContext) -> dict[str, Any]:
    app_state = context.app_state
    if app_state is None:
        return {"ok": False, "reason": "app_state_unavailable"}
    cfg = _dream_config(app_state)
    if not cfg["enabled"]:
        return {"ok": False, "reason": "disabled"}
    kernel = getattr(app_state, "memory_kernel", None)
    if kernel is None:
        return {"ok": False, "reason": "memory_kernel_unavailable"}
    runner_fn = getattr(app_state, "agent_runner_fn", None)
    if runner_fn is None:
        return {"ok": False, "reason": "agent_runner_unavailable"}

    try:
        from corlinman_memory_kernel import KernelScope
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"deps_unavailable: {exc}"}

    persona_id = cfg["persona_id"]
    material = await kernel.sample_dream_material(
        persona_id, lookback_hours=cfg["lookback_hours"], limit=cfg["sample"]
    )
    report: dict[str, Any] = {
        "builtin": MEMORY_DREAM_BUILTIN_NAME,
        "dry_run": cfg["dry_run"],
        "persona_id": persona_id,
        "material": len(material),
        "reflections": 0,
        "reflections_rejected": 0,
        "diary_written": False,
        "demoted": 0,
        "mood_delta": None,
    }
    if not material:
        return {"ok": True, **report, "reason": "no_material"}

    valid_ids = {item.id for item in material}
    # id → owning user, so a reflection inherits the scope of its
    # evidence (see the reflection loop): one derived only from user A's
    # memories must stay private to A, not surface to every user of the
    # persona.
    item_user = {item.id: item.scope.scope_user_id for item in material}
    rendered = "\n".join(f"[{item.id}] {item.text}" for item in material)
    prompt = f"{_DREAM_SYSTEM_PROMPT}\n\n## Memories\n{rendered}"

    try:
        result = await runner_fn(prompt)
        raw = (
            str(result.get("reply", ""))
            if isinstance(result, dict) and result.get("ok")
            else ""
        )
        dream = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return {"ok": False, "reason": f"dream_parse_failed: {exc}", **report}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"dream_failed: {exc}", **report}
    if not isinstance(dream, dict):
        return {"ok": False, "reason": "dream_not_object", **report}

    # Reflections — evidence must reference REAL sampled ids.
    reflections = dream.get("reflections", [])
    if not isinstance(reflections, list):
        reflections = []
    for refl in reflections[:3]:
        if not isinstance(refl, dict):
            continue
        text = str(refl.get("text", "")).strip()
        evidence = [
            str(e) for e in refl.get("evidence", []) if str(e) in valid_ids
        ]
        if not text or not evidence:
            report["reflections_rejected"] += 1
            continue
        # Scope the reflection to its evidence: a single owning user →
        # private to that user; evidence spanning users (or agent-scoped
        # items) → persona-global (a genuine cross-user pattern, no one
        # user's PII). This is what keeps A's reflections off B's turns.
        owners = {item_user.get(ev) for ev in evidence}
        refl_user = owners.pop() if len(owners) == 1 else None
        report["reflections"] += 1
        if not cfg["dry_run"]:
            new_id = await kernel.add_item(
                KernelScope(scope_user_id=refl_user, persona_id=persona_id),
                text=text,
                kind="reflection",
                source="dream",
                trust=0.4,
                importance=0.5,
            )
            for ev in evidence:
                await kernel.add_edge(new_id, ev, "derived_from")

    # Diary — through the existing capped persona path.
    diary = str(dream.get("diary", "")).strip()
    if diary and not cfg["dry_run"]:
        report["diary_written"] = await _write_diary(
            app_state, persona_id, diary
        )
    elif diary:
        report["diary_written"] = True  # would-write (dry run)

    # Mood nudge (clamped) applied as a one-shot EMA step.
    md = dream.get("mood_delta")
    if isinstance(md, dict):
        delta = (
            _clamp(md.get("e"), -0.3, 0.3),
            _clamp(md.get("p"), -0.3, 0.3),
            _clamp(md.get("a"), -0.3, 0.3),
        )
        report["mood_delta"] = list(delta)
        if not cfg["dry_run"]:
            # ADD the delta to the accumulated mood (not an EMA-replace).
            await kernel.nudge_affect_state(persona_id, delta)

    # Gated demotion — soft, reversible, never touches valid_to_ms.
    demote = [str(d) for d in dream.get("demote", []) if str(d) in valid_ids]
    if demote and not cfg["dry_run"]:
        report["demoted"] = await kernel.demote_items(demote)
    elif demote:
        report["demoted"] = len(demote)

    data_dir = resolve_data_dir(context)
    if data_dir is not None:
        try:
            reports_dir = data_dir / "reports" / "memory-dream"
            reports_dir.mkdir(parents=True, exist_ok=True)
            mode = "dry" if cfg["dry_run"] else "live"
            stamp = "{}-{:03d}-{}".format(
                time.strftime("%Y%m%d-%H%M%S", time.gmtime()),
                int(time.time() * 1000) % 1000,
                mode,
            )
            (reports_dir / f"{stamp}.json").write_text(
                json.dumps(
                    {**report, "diary_preview": diary[:200]},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover — report is best-effort
            _logger.warning("memory.dream report write failed: %s", exc)

    return {"ok": True, **report}


register_builtin(MEMORY_DREAM_BUILTIN_NAME, _memory_dream_action)
