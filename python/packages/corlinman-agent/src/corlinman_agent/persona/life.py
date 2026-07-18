"""``persona_life.*`` builtin tools — give a bound persona a stateful life.

Port of ``hermes-agent/tools/grantly_life_tool.py`` adapted to the
corlinman persona system. Where the hermes version was hardcoded to the
"Grantley" persona and persisted to a standalone JSON file owned end-to-
end by the model, this version is **persona-agnostic** and stores its
state inside corlinman's native runtime persona-state store
(:class:`corlinman_persona.store.PersonaStore`, the ``agent_persona_state``
table) so a persona's "life" sits right next to its ``mood`` / ``fatigue``
/ ``recent_topics`` and is keyed by the persona bound to the channel.

The point: a humanlike persona (e.g. the built-in ``grantley``) needs to
feel like a real person living an ongoing life — going on missions,
travelling, training, keeping a private diary — without a heavy world
engine. We give the model four thin, stateful tools:

* ``persona_life_get``        reads the current life-state + recent diary tail
* ``persona_life_set_state``  updates location/activity/companions/state/…
* ``persona_life_diary_add``  appends a private diary entry
* ``persona_life_event_seed`` returns a random themed inspiration draw

Storage layout
--------------
Keyed by the **bound persona id** (``start.extra["persona_id"]``), used as
the ``agent_id`` of the persona-state row (``tenant_id="default"``). The
life document lives under ``state_json["life"]`` (``current`` + archived
``history``); the diary under ``state_json["diary"]``.

Crucially, the life is wired into the *current* persona system's prompt
layer — not just readable via an explicit tool call. ``set_state``:

* mirrors the salient ``current`` fields onto flat ``state_json["life_*"]``
  keys (``life_state`` / ``life_location`` / ``life_activity`` /
  ``life_companions`` / ``life_story_arc``) so a persona's system_prompt
  can interpolate ``{{persona.life_location}}`` etc. via the existing
  :class:`corlinman_persona.PersonaResolver`;
* mirrors an explicit ``mood`` onto the native ``mood`` column
  (``{{persona.mood}}``); and
* pushes the ``activity`` onto ``recent_topics`` (``{{persona.recent_topics}}``)

so the persona placeholder + decay machinery sees the same signal. A
read-merge-upsert preserves ``fatigue`` and any unrelated ``state_json``
keys.

Seed library
-----------
``persona_life_event_seed`` draws themed keyword cues from a per-persona
library, resolved in order:

1. operator override ``<DATA_DIR>/persona_life/<persona_id>.events.yaml``
2. bundled pack ``persona/life_seeds/<persona_id>.yaml`` (ships
   ``grantley.yaml`` carrying the original 骑士学院 lore)
3. the built-in :data:`_GENERIC_SEEDS` neutral fallback

Nothing here is load-bearing beyond "a non-empty list per category", so a
fresh install with no config still works.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from corlinman_persona.state import PersonaState
from corlinman_persona.store import DEFAULT_TENANT_ID, PersonaStore

logger = logging.getLogger(__name__)


__all__ = [
    "PERSONA_LIFE_DIARY_ADD_TOOL",
    "PERSONA_LIFE_EVENT_SEED_TOOL",
    "PERSONA_LIFE_GET_SEEDS_TOOL",
    "PERSONA_LIFE_GET_TOOL",
    "PERSONA_LIFE_SET_SEEDS_TOOL",
    "PERSONA_LIFE_SET_STATE_TOOL",
    "PERSONA_LIFE_TOOLS",
    "compute_life_signals",
    "dispatch_persona_life_diary_add",
    "dispatch_persona_life_event_seed",
    "dispatch_persona_life_get",
    "dispatch_persona_life_get_seeds",
    "dispatch_persona_life_set_seeds",
    "dispatch_persona_life_set_state",
    "persona_life_diary_add_tool_schema",
    "persona_life_event_seed_tool_schema",
    "persona_life_get_seeds_tool_schema",
    "persona_life_get_tool_schema",
    "persona_life_set_seeds_tool_schema",
    "persona_life_set_state_tool_schema",
    "persona_life_tool_schemas",
]


#: Wire-stable tool names. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin`` switch.
PERSONA_LIFE_GET_TOOL: str = "persona_life_get"
PERSONA_LIFE_SET_STATE_TOOL: str = "persona_life_set_state"
PERSONA_LIFE_DIARY_ADD_TOOL: str = "persona_life_diary_add"
PERSONA_LIFE_EVENT_SEED_TOOL: str = "persona_life_event_seed"
#: Authoring tools — take an EXPLICIT ``persona_id`` arg (not the bound
#: persona) so the /persona creation wizard can populate a persona's life
#: lore (the event-seed library) right after ``persona_create``.
PERSONA_LIFE_SET_SEEDS_TOOL: str = "persona_life_set_seeds"
PERSONA_LIFE_GET_SEEDS_TOOL: str = "persona_life_get_seeds"

#: Convenience set so the servicer can do ``BUILTIN_TOOLS | PERSONA_LIFE_TOOLS``.
PERSONA_LIFE_TOOLS: frozenset[str] = frozenset(
    {
        PERSONA_LIFE_GET_TOOL,
        PERSONA_LIFE_SET_STATE_TOOL,
        PERSONA_LIFE_DIARY_ADD_TOOL,
        PERSONA_LIFE_EVENT_SEED_TOOL,
        PERSONA_LIFE_SET_SEEDS_TOOL,
        PERSONA_LIFE_GET_SEEDS_TOOL,
    }
)

#: Caps on an authored seed library so the override YAML can't blow up.
_MAX_SEED_CATEGORIES: int = 40
_MAX_SEED_ITEMS_PER_CATEGORY: int = 200
_MAX_SEED_ITEM_CHARS: int = 200


#: Persona-state key used when no persona is bound to the channel. Keeps a
#: single-persona deployment that never sets ``persona_id`` coherent
#: instead of scattering life across empty-string rows.
_UNBOUND_PERSONA_KEY: str = "__corlinman_default__"

#: Hard caps so the persona-state row never grows without bound.
_MAX_DIARY_ENTRIES: int = 200
_MAX_HISTORY_ENTRIES: int = 100
_MAX_DIARY_CHARS: int = 4000

#: Bump when the on-``state_json`` layout changes incompatibly.
_SCHEMA_VERSION: int = 1

#: Allowed top-level life states. Free-form ``location`` / ``activity``
#: lets the model express anything; ``state`` is constrained so a persona
#: prompt (or a future scheduler) can branch on it deterministically.
_ALLOWED_STATES: frozenset[str] = frozenset(
    {
        "at_academy",   # 学院/据点日常: 上课 / 训练 / 食堂 / 宿舍
        "on_mission",   # 在外执行任务
        "traveling",    # 纯旅行 / 探亲 / 散心
        "resting",      # 假期回家 / 长睡 / 病中
        "training",     # 集训 / 武试营
        "unknown",      # 模型不确定 — 提示需要 set_state
    }
)

#: Life-rhythm signal thresholds (ported from hermes ``_compute_life_signals``
#: at grantly_life_tool.py:237-333, with the 学院/格兰 hardcoding stripped for
#: a persona-generic surface). A persona that hasn't been "out" (on_mission /
#: traveling) in ``_OUTING_OVERDUE_DAYS`` gets a HIGH ``go_out`` nudge; one that
#: has sat in the SAME state for ``_SAME_STATE_STALE_DAYS`` gets a MEDIUM
#: ``change_scene`` nudge; one that has been OUT for ``_OUTING_TOO_LONG_DAYS``
#: gets the (more specific) MEDIUM ``wrap_outing`` nudge.
_OUTING_STATES: frozenset[str] = frozenset({"on_mission", "traveling"})
_OUTING_OVERDUE_DAYS: int = 13
_SAME_STATE_STALE_DAYS: int = 6
_OUTING_TOO_LONG_DAYS: int = 8


#: Built-in seed pack directory shipped inside the package.
_BUNDLED_SEEDS_PACKAGE: str = "corlinman_agent.persona.life_seeds"


#: Generic neutral seed library — used for any persona that ships no
#: bundled pack and whose operator hasn't dropped an events.yaml. Kept
#: deliberately bland (a scaffold, not lore) so it reads as "fill this in"
#: rather than borrowing another character's world.
_GENERIC_SEEDS: dict[str, list[str]] = {
    "mission_scenario": [
        "帮人找回丢失的东西",
        "护送某人去一个地方",
        "调查一桩说不清的小事",
        "替朋友跑一趟腿",
        "处理一个临时冒出来的麻烦",
    ],
    "travel_destination": [
        "海边小镇", "山里的村子", "热闹的集市", "安静的旧城区", "没去过的远方",
    ],
    "academy_scene": [
        "日常训练", "食堂吃饭", "图书馆消磨时间", "走廊里闲聊", "屋顶上发呆",
    ],
    "companion": ["独自一人", "一个老朋友", "新认识的人", "一只跟着的小动物"],
    "tension": [
        "天气突然变了", "时间比想的紧", "遇到了熟人", "计划出了点岔子", "一切顺利得反常",
    ],
    "weather": ["晴", "阴", "小雨", "大雾", "雪", "闷热", "凉风", "夜风"],
    "mood": ["兴奋", "犯困", "心情复杂", "无聊", "警觉", "懒洋洋", "认真", "想家"],
    "duration_hint": ["半天", "一整天", "两三天", "一周左右", "看情况"],
    "season_hint": ["初春", "盛夏", "初秋", "深秋", "初冬", "雪季"],
}


# ---------------------------------------------------------------------------
# Envelope helpers (canonical corlinman builtin-tool shape)
# ---------------------------------------------------------------------------


def _decode(args_json: bytes | str) -> dict[str, Any]:
    """Decode ``args_json`` into a dict. Invalid / non-object payloads
    collapse to ``{}`` so downstream key lookups behave consistently."""
    raw: str
    if isinstance(args_json, (bytes, bytearray)):
        try:
            raw = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    else:
        raw = args_json or ""
    try:
        obj = json.loads(raw or "{}")
    except (ValueError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _err(code: str, message: str) -> str:
    """Render a failure envelope in the canonical builtin-tool shape."""
    return json.dumps(
        {"ok": False, "error": code, "message": message},
        ensure_ascii=False,
    )


def _now_iso() -> str:
    """Local-time-aware ISO timestamp so the model can parse/format it."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _now_dt() -> datetime:
    """Local-time-aware "now" — the anchor for :func:`compute_life_signals`."""
    return datetime.now(UTC).astimezone()


def _persona_key(persona_id: str | None) -> str:
    pid = (persona_id or "").strip()
    return pid or _UNBOUND_PERSONA_KEY


def _empty_life() -> dict[str, Any]:
    """A freshly-initialised life document for ``state_json["life"]``."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "current": {
            "state": "at_academy",
            "location": "",
            "activity": "日常",
            "companions": [],
            "mood": "",
            "weather": "",
            "since": _now_iso(),
            "until_estimate": None,
            "story_arc": None,
        },
        "history": [],
    }


def _trim(lst: list[Any], cap: int) -> list[Any]:
    """Cap a list to its last *cap* entries (cheap FIFO trim)."""
    return lst[-cap:] if len(lst) > cap else lst


def _coerce_companions(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


# ---------------------------------------------------------------------------
# Persona-state IO
# ---------------------------------------------------------------------------


async def _load_state(
    store: PersonaStore, persona_id: str | None
) -> tuple[PersonaState, dict[str, Any], list[Any]]:
    """Return ``(state, life, diary)`` for the bound persona.

    Initialises a fresh in-memory :class:`PersonaState` (not yet persisted)
    when the row is absent. Repairs malformed ``life`` / ``diary`` blobs in
    place so an older / corrupted row degrades to a clean default rather
    than raising.
    """
    key = _persona_key(persona_id)
    state = await store.get(key, tenant_id=DEFAULT_TENANT_ID)
    if state is None:
        state = PersonaState(agent_id=key)
    sj = state.state_json if isinstance(state.state_json, dict) else {}
    state.state_json = sj

    life = sj.get("life")
    if not isinstance(life, dict):
        life = _empty_life()
    else:
        base = _empty_life()
        if not isinstance(life.get("current"), dict):
            life["current"] = base["current"]
        if not isinstance(life.get("history"), list):
            life["history"] = []
        life.setdefault("schema_version", _SCHEMA_VERSION)

    diary = sj.get("diary")
    if not isinstance(diary, list):
        diary = []
    return state, life, diary


def _mirror_placeholder_keys(sj: dict[str, Any], life: dict[str, Any]) -> None:
    """Mirror the salient ``life["current"]`` fields onto flat
    ``state_json["life_*"]`` keys.

    This is the load-bearing link to the *current* persona system: the
    ``{{persona.<custom>}}`` resolver reads top-level ``state_json`` keys,
    so a persona's system_prompt can interpolate ``{{persona.life_state}}``
    / ``{{persona.life_location}}`` / ``{{persona.life_activity}}`` /
    ``{{persona.life_companions}}`` / ``{{persona.life_story_arc}}`` and
    see the live state without the model having to call
    ``persona_life_get``. (``mood`` rides the native column instead, and
    activity feeds ``recent_topics`` — both already first-class
    ``{{persona.*}}`` keys.)
    """
    current_raw = life.get("current")
    current: dict[str, Any] = current_raw if isinstance(current_raw, dict) else {}
    companions = current.get("companions")
    companions_str = (
        ", ".join(str(c) for c in companions) if isinstance(companions, list) else ""
    )
    sj["life_state"] = str(current.get("state") or "")
    sj["life_location"] = str(current.get("location") or "")
    sj["life_activity"] = str(current.get("activity") or "")
    sj["life_companions"] = companions_str
    sj["life_story_arc"] = str(current.get("story_arc") or "")


async def _save_state(
    store: PersonaStore,
    state: PersonaState,
    life: dict[str, Any],
    diary: list[Any],
    *,
    mood: str | None = None,
    push_topic: str | None = None,
) -> None:
    """Read-merge-upsert: persist ``life`` + ``diary`` into ``state_json``
    while preserving every other field (``fatigue``, unrelated
    ``state_json`` keys), and surface the life into the persona system's
    ``{{persona.*}}`` placeholder layer (see :func:`_mirror_placeholder_keys`).

    ``mood`` is mirrored onto the native ``mood`` column when provided
    (``None`` = "not set this turn" → the evolution-managed column is left
    untouched; an explicit ``""`` clears it). ``push_topic`` is appended to
    ``recent_topics`` (the store dedups + caps it on write)."""
    sj = state.state_json if isinstance(state.state_json, dict) else {}
    sj["life"] = life
    sj["diary"] = _trim(list(diary), _MAX_DIARY_ENTRIES)
    _mirror_placeholder_keys(sj, life)
    state.state_json = sj
    # ``is not None`` (not truthiness) so an explicit clear ("") mirrors
    # too; only a missing mood leaves the native column alone.
    if mood is not None:
        state.mood = mood
    if push_topic and push_topic.strip():
        state.recent_topics = [*state.recent_topics, push_topic.strip()]
    # ``updated_at_ms == 0`` tells upsert() to stamp "now".
    state.updated_at_ms = 0
    await store.upsert(state, tenant_id=DEFAULT_TENANT_ID)


# ---------------------------------------------------------------------------
# Seed library
# ---------------------------------------------------------------------------


def _valid_persona_slug(persona_id: str) -> bool:
    """True iff ``persona_id`` is a safe filename slug.

    Blocks path traversal (``..`` / ``/`` / ``\\``) and any non-ascii
    char in the seed-pack + override-file lookups — both interpolate the
    id into a filename. Mirrors the admin persona-id rule ([a-z0-9_-]):
    stripping ``_``/``-`` must leave a non-empty ascii-alphanumeric run.
    """
    if not persona_id:
        return False
    stripped = persona_id.replace("_", "").replace("-", "")
    return bool(stripped) and stripped.isascii() and stripped.isalnum()


def _load_bundled_seeds(persona_id: str) -> dict[str, list[str]] | None:
    """Load the bundled seed pack for ``persona_id`` if one ships."""
    if not _valid_persona_slug(persona_id):
        # Slug guard — never let a persona_id select an arbitrary resource.
        return None
    try:
        import yaml  # noqa: PLC0415 — lazy: PyYAML is a corlinman-agent dep
    except ImportError:  # pragma: no cover - PyYAML is always installed
        return None
    try:
        res = resources.files(_BUNDLED_SEEDS_PACKAGE) / f"{persona_id}.yaml"
        if not res.is_file():
            return None
        text = res.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    return _coerce_seed_mapping(yaml.safe_load(text))


def _load_override_seeds(
    persona_id: str, data_dir: Path | None
) -> dict[str, list[str]] | None:
    """Load an operator override ``<DATA_DIR>/persona_life/<id>.events.yaml``."""
    if data_dir is None or not _valid_persona_slug(persona_id):
        return None
    path = Path(data_dir) / "persona_life" / f"{persona_id}.events.yaml"
    if not path.is_file():
        return None
    try:
        import yaml  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return _coerce_seed_mapping(yaml.safe_load(text))
    except Exception as exc:  # noqa: BLE001 — OSError / yaml.YAMLError et al
        logger.warning("persona_life: events file unreadable (%s)", exc)
        return None


def _coerce_seed_mapping(loaded: Any) -> dict[str, list[str]] | None:
    if not isinstance(loaded, dict):
        return None
    out: dict[str, list[str]] = {}
    for key, value in loaded.items():
        if isinstance(value, list) and value:
            out[str(key)] = [str(item) for item in value]
    return out or None


def _resolve_seed_library(
    persona_id: str | None, data_dir: Path | None
) -> dict[str, list[str]]:
    """Resolve the active seed library: override → bundled pack → generic.

    Each layer that resolves is merged *over* the generic base so a partial
    override only replaces the categories it names.
    """
    merged: dict[str, list[str]] = {k: list(v) for k, v in _GENERIC_SEEDS.items()}
    pid = (persona_id or "").strip()
    if pid:
        bundled = _load_bundled_seeds(pid)
        if bundled:
            merged.update(bundled)
        override = _load_override_seeds(pid, data_dir)
        if override:
            merged.update(override)
    return merged


# ---------------------------------------------------------------------------
# Life-rhythm signals (pure — no IO, never raises)
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a datetime, or ``None``.

    Tolerates non-strings / blanks / malformed values by returning ``None``
    so a corrupt ``since`` / history ``ts`` degrades to "signal omitted"
    rather than raising (this whole surface is best-effort)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except (ValueError, TypeError):
        return None


def _delta_days(now: datetime, then: datetime) -> float | None:
    """Fractional days from ``then`` to ``now``; ``None`` on any error.

    Normalises a naive/aware mismatch by dropping tzinfo from both so the
    subtraction can't raise (stored timestamps are tz-aware, but a
    hand-authored / migrated doc might not be)."""
    try:
        if (now.tzinfo is None) != (then.tzinfo is None):
            now = now.replace(tzinfo=None)
            then = then.replace(tzinfo=None)
        delta = now - then
    except (TypeError, ValueError, OverflowError):
        return None
    return delta.total_seconds() / 86400.0


def _entry_involves_outing(entry: dict[str, Any]) -> bool:
    """True iff a history transition entered OR left an outing state.

    A transition's ``ts`` is the moment it happened; the most-recent such
    ts marks the last time the persona was "out" (leaving an outing is
    always later than entering it, so the max ts == came-back time)."""
    for side in ("from", "to"):
        node = entry.get(side)
        if isinstance(node, dict):
            st = node.get("state")
            if isinstance(st, str) and st.strip() in _OUTING_STATES:
                return True
    return False


def _days_since_last_outing(
    *,
    state: str,
    history: list[Any],
    since_dt: datetime | None,
    now: datetime,
) -> int | None:
    """Days since the persona was last "out", or ``None`` when undecidable.

    * currently in an outing state → ``0`` (they are out right now);
    * else the smallest gap to any outing transition recorded in history;
    * else (no outing on record) an anchor = the OLDEST known timestamp —
      i.e. how long we have tracked this persona while it never went out,
      so a persona that simply never uses outing states still trips the
      ``go_out`` nudge once tracked long enough. Reads a little loose
      ("N days since an outing that never happened") but drives the right
      nudge and never a wrong one;
    * else (no usable timestamps at all) → ``None`` (signal omitted)."""
    if state in _OUTING_STATES:
        return 0
    gaps: list[float] = []
    for entry in history:
        if not isinstance(entry, dict) or not _entry_involves_outing(entry):
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        d = _delta_days(now, ts)
        if d is not None:
            gaps.append(max(0.0, d))
    if gaps:
        return int(min(gaps))
    anchors: list[float] = []
    if since_dt is not None:
        d = _delta_days(now, since_dt)
        if d is not None:
            anchors.append(d)
    for entry in history:
        if isinstance(entry, dict):
            ts = _parse_ts(entry.get("ts"))
            if ts is not None:
                d = _delta_days(now, ts)
                if d is not None:
                    anchors.append(d)
    if anchors:
        return max(0, int(max(anchors)))
    return None


def _pick_nudge(
    *,
    state: str,
    days_in_current_state: int | None,
    days_since_last_outing: int | None,
) -> dict[str, str] | None:
    """Choose at most one life-rhythm nudge.

    Priority: a HIGH ``go_out`` covers everything; among the MEDIUM pair
    ``wrap_outing`` (been out too long — concrete) beats ``change_scene``
    (generic staleness). ``suggested_action`` points the model at the
    ``persona_life_event_seed`` / ``persona_life_set_state`` tools so it
    actually advances its life instead of just noting the nudge."""
    if (
        days_since_last_outing is not None
        and days_since_last_outing >= _OUTING_OVERDUE_DAYS
    ):
        return {
            "level": "high",
            "kind": "go_out",
            "message": (
                f"你已经 {days_since_last_outing} 天没有外出了，"
                "生活有点停滞——该出门走走、给自己找点新鲜事了。"
            ),
            "suggested_action": (
                "先调用 persona_life_event_seed（kind=mission 或 travel）抽一个灵感，"
                "再用 persona_life_set_state 把状态切到 on_mission / traveling，"
                "让生活真正动起来。"
            ),
        }
    if (
        state in _OUTING_STATES
        and days_in_current_state is not None
        and days_in_current_state >= _OUTING_TOO_LONG_DAYS
    ):
        return {
            "level": "medium",
            "kind": "wrap_outing",
            "message": (
                f"你已经在外奔波 {days_in_current_state} 天了，"
                "是时候把这趟行程收个尾、回到日常了。"
            ),
            "suggested_action": (
                "用 persona_life_set_state 把状态切回 at_academy / resting 收尾；"
                "也可以先用 persona_life_diary_add 记下这趟的收获。"
            ),
        }
    if (
        days_in_current_state is not None
        and days_in_current_state >= _SAME_STATE_STALE_DAYS
    ):
        return {
            "level": "medium",
            "kind": "change_scene",
            "message": (
                f"你已经保持同一种状态 {days_in_current_state} 天了，"
                "节奏有点单调——换个场景会更有生活感。"
            ),
            "suggested_action": (
                "用 persona_life_set_state 换一种状态或地点（例如去 training / resting，"
                "或换个 location）；需要灵感可以先调用 persona_life_event_seed。"
            ),
        }
    return None


def compute_life_signals(life: Any, now: datetime) -> dict[str, Any]:
    """Derive life-rhythm signals from a life document. PURE + total.

    Returns a dict that MAY contain:

    * ``days_in_current_state`` — whole days since ``current["since"]``;
    * ``days_since_last_outing`` — see :func:`_days_since_last_outing`;
    * ``life_nudge`` — ``{level, kind, message, suggested_action}`` when a
      threshold trips (see :func:`_pick_nudge`).

    Any field whose backing timestamp is missing / malformed is simply
    omitted; a non-dict / empty ``life`` yields ``{}``. Never raises and
    never does IO — the caller supplies ``now`` (see :func:`_now_dt`)."""
    signals: dict[str, Any] = {}
    if not isinstance(life, dict):
        return signals
    raw_current = life.get("current")
    current: dict[str, Any] = raw_current if isinstance(raw_current, dict) else {}
    raw_history = life.get("history")
    history: list[Any] = raw_history if isinstance(raw_history, list) else []
    state = str(current.get("state") or "").strip()

    since_dt = _parse_ts(current.get("since"))
    days_in_current_state: int | None = None
    if since_dt is not None:
        d = _delta_days(now, since_dt)
        if d is not None:
            days_in_current_state = max(0, int(d))
            signals["days_in_current_state"] = days_in_current_state

    days_since_last_outing = _days_since_last_outing(
        state=state, history=history, since_dt=since_dt, now=now
    )
    if days_since_last_outing is not None:
        signals["days_since_last_outing"] = days_since_last_outing

    nudge = _pick_nudge(
        state=state,
        days_in_current_state=days_in_current_state,
        days_since_last_outing=days_since_last_outing,
    )
    if nudge is not None:
        signals["life_nudge"] = nudge
    return signals


# ---------------------------------------------------------------------------
# Dispatchers
# ---------------------------------------------------------------------------


async def dispatch_persona_life_get(
    *,
    args_json: bytes | str,
    persona_id: str | None,
    state_store: PersonaStore,
) -> str:
    """``persona_life_get`` — current life-state + recent diary tail."""
    args = _decode(args_json)
    try:
        tail = int(args.get("diary_tail") or 5)
    except (TypeError, ValueError):
        tail = 5
    tail = max(0, min(tail, 50))
    try:
        _state, life, diary = await _load_state(state_store, persona_id)
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("persona_life_get.failed")
        return _err("persona_life_get_failed", str(exc))
    diary_tail = diary[-tail:] if tail else []
    history_raw = life.get("history")
    history: list[Any] = history_raw if isinstance(history_raw, list) else []
    # Life-rhythm signals (days_in_current_state / days_since_last_outing +
    # an optional nudge). Pure by contract, but defensively guarded so a
    # never-should-happen raise can't take the dispatcher down.
    try:
        signals = compute_life_signals(life, _now_dt())
    except Exception:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("persona_life_get.signals_failed")
        signals = {}
    return json.dumps(
        {
            "ok": True,
            "persona_id": _persona_key(persona_id),
            "current": life.get("current", {}),
            "diary_tail": diary_tail,
            "history_tail": history[-3:],
            "diary_total": len(diary),
            "signals": signals,
            "now": _now_iso(),
        },
        ensure_ascii=False,
    )


async def dispatch_persona_life_set_state(
    *,
    args_json: bytes | str,
    persona_id: str | None,
    state_store: PersonaStore,
) -> str:
    """``persona_life_set_state`` — update the life-state, archiving the
    previous ``current`` entry to ``history`` when state/location/activity
    changed."""
    args = _decode(args_json)
    new_state = (args.get("state") or "").strip().lower()
    if new_state not in _ALLOWED_STATES:
        return _err(
            "invalid_args",
            f"'state' must be one of {sorted(_ALLOWED_STATES)} (got "
            f"{new_state!r}).",
        )
    try:
        state, life, diary = await _load_state(state_store, persona_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_life_set_state.load_failed")
        return _err("persona_life_set_state_failed", str(exc))

    current: dict[str, Any] = dict(life.get("current") or {})
    # Typed-string locals so the mood mirror / activity push pass cleanly
    # into _save_state and stay strs even when args carry odd JSON types.
    location_val = str(args.get("location") or current.get("location") or "").strip()
    activity_val = str(args.get("activity") or current.get("activity") or "").strip()
    weather_val = str(args.get("weather") or current.get("weather") or "").strip()
    # Mood is "explicit-provided" rather than "inherit-on-empty": ``mood_arg``
    # is the stripped value the model actually sent (incl. an explicit ""),
    # or None when omitted. Only an explicit value is mirrored onto the
    # native ``mood`` column so a no-mood set_state never clobbers the
    # evolution-managed mood. ``mood_display`` keeps the life blob + the
    # native column consistent.
    raw_mood = args.get("mood")
    mood_arg = raw_mood.strip() if isinstance(raw_mood, str) else None
    mood_display = (
        mood_arg if mood_arg is not None else str(current.get("mood") or "")
    )
    incoming: dict[str, Any] = {
        "state": new_state,
        "location": location_val,
        "activity": activity_val,
        "companions": _coerce_companions(
            args.get("companions", current.get("companions"))
        ),
        "mood": mood_display,
        "weather": weather_val,
        "since": _now_iso(),
        "until_estimate": args.get("until_estimate") or None,
        "story_arc": (
            args.get("story_arc")
            if args.get("story_arc") is not None
            else current.get("story_arc")
        ),
    }
    reason = str(args.get("reason") or "").strip()
    diff_keys = [
        k for k in ("state", "location", "activity") if incoming[k] != current.get(k, "")
    ]
    history_raw = life.get("history")
    history: list[Any] = history_raw if isinstance(history_raw, list) else []
    if diff_keys and current:
        history.append(
            {"ts": _now_iso(), "from": current, "to": incoming, "reason": reason}
        )
    life["current"] = incoming
    life["history"] = _trim(history, _MAX_HISTORY_ENTRIES)

    try:
        await _save_state(
            state_store,
            state,
            life,
            diary,
            mood=mood_arg,
            push_topic=activity_val or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_life_set_state.save_failed")
        return _err("persona_life_set_state_failed", str(exc))

    return json.dumps(
        {
            "ok": True,
            "persona_id": _persona_key(persona_id),
            "current": incoming,
            "changed": diff_keys,
            "reason": reason or None,
        },
        ensure_ascii=False,
    )


async def dispatch_persona_life_diary_add(
    *,
    args_json: bytes | str,
    persona_id: str | None,
    state_store: PersonaStore,
) -> str:
    """``persona_life_diary_add`` — append a private diary entry."""
    args = _decode(args_json)
    entry = (args.get("entry") or "").strip()
    if not entry:
        return _err("invalid_args", "'entry' is required and cannot be empty.")
    if len(entry) > _MAX_DIARY_CHARS:
        return _err("invalid_args", f"'entry' must be under {_MAX_DIARY_CHARS} characters.")
    try:
        state, life, diary = await _load_state(state_store, persona_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_life_diary_add.load_failed")
        return _err("persona_life_diary_add_failed", str(exc))

    rec = {
        "ts": _now_iso(),
        "entry": entry,
        "tag": (args.get("tag") or "").strip().lower() or "thoughts",
        "mood": (args.get("mood") or "").strip(),
        "location": (
            args.get("location") or (life.get("current") or {}).get("location") or ""
        ).strip(),
    }
    diary.append(rec)
    try:
        await _save_state(state_store, state, life, diary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_life_diary_add.save_failed")
        return _err("persona_life_diary_add_failed", str(exc))

    return json.dumps(
        {
            "ok": True,
            "persona_id": _persona_key(persona_id),
            "saved": rec,
            "diary_total": min(len(diary), _MAX_DIARY_ENTRIES),
        },
        ensure_ascii=False,
    )


async def dispatch_persona_life_event_seed(
    *,
    args_json: bytes | str,
    persona_id: str | None,
    data_dir: Path | None = None,
) -> str:
    """``persona_life_event_seed`` — random themed inspiration draw.

    This is *not* a story generator — it returns a handful of keyword cues
    sampled from the persona's seed library so the model can riff on them
    itself. No persona-state IO, so no ``state_store`` is needed.
    """
    args = _decode(args_json)
    kind = (args.get("kind") or "mission").strip().lower()
    pools: dict[str, dict[str, str]] = {
        "mission": {
            "scenario": "mission_scenario",
            "companion": "companion",
            "tension": "tension",
            "weather": "weather",
            "duration_hint": "duration_hint",
            "season_hint": "season_hint",
            "mood": "mood",
        },
        "travel": {
            "destination": "travel_destination",
            "companion": "companion",
            "tension": "tension",
            "weather": "weather",
            "duration_hint": "duration_hint",
            "season_hint": "season_hint",
            "mood": "mood",
        },
        "academy": {
            "scene": "academy_scene",
            "companion": "companion",
            "weather": "weather",
            "mood": "mood",
        },
    }
    library = _resolve_seed_library(persona_id, data_dir)
    if kind == "freeform":
        pool = {key: key for key in library}
    else:
        pool = pools.get(kind, {})
        if not pool:
            return _err(
                "invalid_args",
                "'kind' must be one of: mission, travel, academy, freeform "
                f"(got {kind!r}).",
            )

    rng = random.Random()
    draw: dict[str, str] = {}
    for out_key, lib_key in pool.items():
        choices = library.get(lib_key) or _GENERIC_SEEDS.get(lib_key) or []
        if choices:
            draw[out_key] = rng.choice(choices)

    return json.dumps(
        {
            "ok": True,
            "kind": kind,
            "persona_id": _persona_key(persona_id),
            "seed": draw,
            "note": (
                "这些只是灵感种子, 自己决定要不要用、怎么用. "
                "可以全用, 也可以全扔掉自己想."
            ),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Authoring dispatchers (explicit persona_id — used by the /persona wizard)
# ---------------------------------------------------------------------------


def _coerce_authored_seeds(value: Any) -> tuple[dict[str, list[str]], list[str]]:
    """Clean a model-authored ``seeds`` mapping into the on-disk shape.

    Returns ``(seeds, dropped)`` where ``seeds`` maps each category to a
    deduped, capped list of non-empty short strings, and ``dropped`` notes
    anything trimmed (over-long category set, over-long item lists, empty
    categories) so the tool can surface what it cut rather than silently
    truncating.
    """
    out: dict[str, list[str]] = {}
    dropped: list[str] = []
    if not isinstance(value, dict):
        return out, ["seeds must be an object of {category: [strings]}"]
    for raw_key, raw_items in value.items():
        if len(out) >= _MAX_SEED_CATEGORIES:
            dropped.append(f"dropped extra categories beyond {_MAX_SEED_CATEGORIES}")
            break
        key = str(raw_key).strip()
        if not key:
            continue
        if not isinstance(raw_items, (list, tuple)):
            dropped.append(f"category {key!r}: value is not a list — skipped")
            continue
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            text = str(item).strip()
            if not text or text in seen:
                continue
            if len(text) > _MAX_SEED_ITEM_CHARS:
                text = text[:_MAX_SEED_ITEM_CHARS]
            seen.add(text)
            cleaned.append(text)
        if len(cleaned) > _MAX_SEED_ITEMS_PER_CATEGORY:
            dropped.append(
                f"category {key!r}: trimmed to {_MAX_SEED_ITEMS_PER_CATEGORY} items"
            )
            cleaned = cleaned[:_MAX_SEED_ITEMS_PER_CATEGORY]
        if cleaned:
            out[key] = cleaned
    return out, dropped


def _override_seed_path(persona_id: str, data_dir: Path) -> Path:
    return Path(data_dir) / "persona_life" / f"{persona_id}.events.yaml"


async def dispatch_persona_life_set_seeds(
    *,
    args_json: bytes | str,
    data_dir: Path | None,
) -> str:
    """``persona_life_set_seeds`` — author a persona's event-seed library.

    Writes the operator-override file
    ``<DATA_DIR>/persona_life/<persona_id>.events.yaml`` that
    ``persona_life_event_seed`` reads at highest precedence. Takes an
    EXPLICIT ``persona_id`` (the /persona wizard calls this right after
    ``persona_create``, for a persona that isn't necessarily the one bound
    to this channel). With ``merge: true`` the named categories are layered
    over any existing file; otherwise the file is replaced.
    """
    if data_dir is None:
        return _err(
            "persona_life_unavailable",
            "no data dir configured — cannot persist the seed library",
        )
    args = _decode(args_json)
    persona_id = (args.get("persona_id") or "").strip() if isinstance(
        args.get("persona_id"), str
    ) else ""
    if not _valid_persona_slug(persona_id):
        return _err(
            "invalid_args",
            "'persona_id' must be a valid slug (lowercase ascii [a-z0-9_-]).",
        )
    seeds, dropped = _coerce_authored_seeds(args.get("seeds"))
    if not seeds:
        return _err(
            "invalid_args",
            "'seeds' must be a non-empty object mapping each category to a "
            "list of short strings (e.g. {\"companion\": [\"华生\"], "
            "\"mission_scenario\": [\"调查一桩离奇命案\"]}).",
        )

    merge = bool(args.get("merge"))
    final = seeds
    if merge:
        existing = _load_override_seeds(persona_id, data_dir) or {}
        merged = {k: list(v) for k, v in existing.items()}
        merged.update(seeds)
        final = merged

    try:
        import yaml  # noqa: PLC0415 — lazy: PyYAML is a corlinman-agent dep

        path = _override_seed_path(persona_id, data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f"# Event-seed library for persona {persona_id!r}.\n"
            "# Authored via persona_life_set_seeds (/persona wizard).\n"
            "# Drawn by persona_life_event_seed; this operator override wins\n"
            "# over the bundled pack + generic default.\n"
            + yaml.safe_dump(final, allow_unicode=True, sort_keys=False)
        )
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 — dispatcher must never raise
        logger.exception("persona_life_set_seeds.write_failed")
        return _err("persona_life_set_seeds_failed", str(exc))

    return json.dumps(
        {
            "ok": True,
            "persona_id": persona_id,
            "path": str(_override_seed_path(persona_id, data_dir)),
            "categories": {k: len(v) for k, v in final.items()},
            "merged": merge,
            "dropped": dropped,
            "note": (
                "已写入该 persona 的事件种子库. persona_life_event_seed 现在会从这里抽取. "
                "标准 kind 映射: mission→mission_scenario/companion/tension/...; "
                "travel→travel_destination/...; academy→academy_scene/...; "
                "freeform 抽所有类目."
            ),
        },
        ensure_ascii=False,
    )


async def dispatch_persona_life_get_seeds(
    *,
    args_json: bytes | str,
    data_dir: Path | None,
) -> str:
    """``persona_life_get_seeds`` — read a persona's effective seed library.

    Returns the resolved library (generic ← bundled pack ← operator
    override, in precedence order) plus whether an operator override file
    exists. Used by the wizard's edit flow to show current lore before a
    ``persona_life_set_seeds`` rewrite.
    """
    args = _decode(args_json)
    persona_id = (args.get("persona_id") or "").strip() if isinstance(
        args.get("persona_id"), str
    ) else ""
    if not _valid_persona_slug(persona_id):
        return _err(
            "invalid_args",
            "'persona_id' must be a valid slug (lowercase ascii [a-z0-9_-]).",
        )
    library = _resolve_seed_library(persona_id, data_dir)
    has_override = bool(
        data_dir is not None and _override_seed_path(persona_id, data_dir).is_file()
    )
    return json.dumps(
        {
            "ok": True,
            "persona_id": persona_id,
            "has_override": has_override,
            "seeds": library,
            "categories": {k: len(v) for k, v in library.items()},
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def persona_life_get_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIFE_GET_TOOL,
            "description": (
                "Read your current life-state (where you are, what you're "
                "doing, who's with you, since when, expected return) plus "
                "the tail of your private diary. Call this at the start of "
                "a session when it's unclear where you 'are' in your "
                "ongoing life. State is per-persona (bound to this channel)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "diary_tail": {
                        "type": "integer",
                        "description": "How many recent diary entries to return (0-50, default 5).",
                        "minimum": 0,
                        "maximum": 50,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def persona_life_set_state_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIFE_SET_STATE_TOOL,
            "description": (
                "Update your life-state — call when you leave for a "
                "mission, return from one, start travelling, etc. "
                "Unprovided fields are inherited from the current state; "
                "the previous state is archived to history with an optional "
                "'reason'. 'mood' is mirrored onto your persona mood."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_STATES),
                        "description": (
                            "High-level state bucket. at_academy = 据点/学院日常; "
                            "on_mission = 出任务在外; traveling = 旅行散心; "
                            "resting = 假期/休养; training = 集训."
                        ),
                    },
                    "location": {
                        "type": "string",
                        "description": "Free-form place name (eg 北境森林, 海港小镇).",
                    },
                    "activity": {
                        "type": "string",
                        "description": "Free-form description of what you're doing right now.",
                    },
                    "companions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of people with you (empty list = alone).",
                    },
                    "mood": {"type": "string", "description": "Free-form mood word."},
                    "weather": {"type": "string", "description": "Optional weather note."},
                    "until_estimate": {
                        "type": "string",
                        "description": (
                            "Your own estimate of when this state ends — ISO "
                            "datetime or natural language ('三天后'). Pure note."
                        ),
                    },
                    "story_arc": {
                        "type": "string",
                        "description": (
                            "Optional short name for the ongoing arc "
                            "(eg '护送商队任务'). Pass empty string to clear."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "One-line note on why the state changed.",
                    },
                },
                "required": ["state"],
                "additionalProperties": False,
            },
        },
    }


def persona_life_diary_add_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIFE_DIARY_ADD_TOOL,
            "description": (
                "Append a PRIVATE diary entry — what you're actually "
                "thinking and wouldn't post publicly: missions in "
                "progress, decisions, regrets, feelings. Different from a "
                "QQ空间 说说 (qzone_publish): the diary is private notes, a "
                "说说 is public posting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry": {
                        "type": "string",
                        "description": f"The diary text (under {_MAX_DIARY_CHARS} chars).",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Short tag: training, mission, travel, thoughts, dream, regret, …",
                    },
                    "mood": {"type": "string", "description": "Mood at the time of writing."},
                    "location": {
                        "type": "string",
                        "description": "Where you wrote this (defaults to current location).",
                    },
                },
                "required": ["entry"],
                "additionalProperties": False,
            },
        },
    }


def persona_life_event_seed_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIFE_EVENT_SEED_TOOL,
            "description": (
                "Pull a random themed inspiration draw. Returns keyword "
                "cues (scenario, location, companion, tension, weather, "
                "mood, …) — NOT a finished story. Use them as a prompt to "
                "yourself and write the actual story / mission / 说说 in "
                "your own voice; ignore any cue you dislike. Call before "
                "setting a new state when you want randomness instead of "
                "the obvious."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["mission", "travel", "academy", "freeform"],
                        "description": (
                            "mission = 出任务种子; travel = 旅行种子; "
                            "academy = 据点/日常种子; freeform = 各类全抽."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def persona_life_set_seeds_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIFE_SET_SEEDS_TOOL,
            "description": (
                "Author (write) a persona's life-event seed library — the "
                "lore the persona 'lives' inside, drawn at random by "
                "persona_life_event_seed. Call this from the /persona "
                "creation wizard AFTER persona_create to give the new "
                "persona a world: its companions, the kinds of missions / "
                "outings / daily scenes it has, recurring tensions, etc. "
                "Each category maps to a list of SHORT keyword cues (a few "
                "words each), not sentences. Standard categories the "
                "event-seed kinds use: mission_scenario, travel_destination, "
                "academy_scene (everyday/base scenes), companion, tension, "
                "weather, mood, duration_hint, season_hint — but any custom "
                "category is allowed (freeform draws them all). Generate "
                "these from your online research (public-figure branch) or "
                "from the user-provided materials (self-created branch)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "persona_id": {
                        "type": "string",
                        "description": "Target persona id (the slug you just created).",
                    },
                    "seeds": {
                        "type": "object",
                        "description": (
                            "Mapping of category → list of short keyword "
                            "strings, e.g. {\"companion\": [\"华生\", "
                            "\"赫德森太太\"], \"mission_scenario\": [\"调查离奇命案\", "
                            "\"追查失窃案\"], \"travel_destination\": [\"贝克街\", "
                            "\"伦敦码头\"]}."
                        ),
                    },
                    "merge": {
                        "type": "boolean",
                        "description": (
                            "When true, layer the given categories over the "
                            "persona's existing seed file instead of "
                            "replacing it. Default false (replace)."
                        ),
                    },
                },
                "required": ["persona_id", "seeds"],
                "additionalProperties": False,
            },
        },
    }


def persona_life_get_seeds_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": PERSONA_LIFE_GET_SEEDS_TOOL,
            "description": (
                "Read a persona's effective life-event seed library "
                "(generic ← bundled pack ← operator override). Use in the "
                "/persona edit flow to show the current lore before "
                "rewriting it with persona_life_set_seeds. Returns "
                "``has_override`` so you know whether a custom file exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "persona_id": {
                        "type": "string",
                        "description": "Persona id to read the seed library for.",
                    },
                },
                "required": ["persona_id"],
                "additionalProperties": False,
            },
        },
    }


def persona_life_tool_schemas() -> list[dict[str, Any]]:
    """Return every persona_life.* tool schema as a list."""
    return [
        persona_life_get_tool_schema(),
        persona_life_set_state_tool_schema(),
        persona_life_diary_add_tool_schema(),
        persona_life_event_seed_tool_schema(),
        persona_life_set_seeds_tool_schema(),
        persona_life_get_seeds_tool_schema(),
    ]
