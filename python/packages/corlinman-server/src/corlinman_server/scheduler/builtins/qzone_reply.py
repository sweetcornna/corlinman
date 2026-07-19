"""``qzone.reply_comments`` — PR-B6, scheduled comment auto-reply.

Drives a one-turn agent chat under a persona's voice that scans the
persona's *own* recent QZone 说说 for fresh comments and replies to them
in-character. Net-new capability (hermes never had it). The job
metadata carries:

* ``persona_id`` — required, resolves the persona row.
* ``qq_account`` — optional, the persona's own QQ. When present the
  tail pins ``qzone_list_feed(owner_uin=<qq_account>)``; when absent
  the tail tells the model to filter the timeline by the ``my_uin``
  the feed result reports.
* ``max_replies`` — optional int, 1-10, default 3. Cap on how many
  comments the turn may answer.
* ``lookback_posts`` — optional int, 1-20, default 5. How many of the
  persona's most-recent 说说 to scan.

Mechanism
---------

The NapCat/OneBot auth for the qzone tools lives inside the agent-side
tool executor (each dispatcher borrows the QQ login state at call
time), so this builtin cannot call the dispatchers directly — it drives
one internal :class:`ChatService` turn whose system tail instructs the
model to:

1. ``qzone_list_feed`` → keep only its own 说说, most-recent
   ``lookback_posts`` of them (the feed items carry their comments
   inline; ``qzone_get_post`` is available for a per-post re-check);
2. skip its own comments and every comment listed in the injected
   "已回复过" block (fed from the seen-sidecar);
3. ``qzone_post_comment`` (``owner_uin`` = self, ``reply_to_uin`` =
   the commenter) for at most ``max_replies`` fresh comments, then end
   the turn — no new 说说, no questions.

Dedup sidecar
-------------

``<DATA_DIR>/qzone_seen_comments/<persona_id>.json`` (persona slug
guarded against traversal), shape::

    {"version": 1, "seen": {"<tid>": ["<uin>:<unix_ts>", ...]}}

Entries are capped at :data:`_SEEN_PER_TID_MAX` per tid and
:data:`_SEEN_TIDS_MAX` tids total (least-recently-updated tids roll
off). The pre-turn snapshot is rendered into the system prompt; after
the turn every *successful* ``qzone_post_comment`` observed on the
event stream (non-error result whose envelope says ``ok``) is folded
back in via an atomic write.

Audit dict
----------

``{ok, replies_posted, tids_scanned, skipped_seen, error?}`` plus the
usual ``persona_id`` / ``qq_account`` / ``tools_called`` /
``duration_ms`` observability fields. ``replies_posted`` counts the
successful comment posts harvested from the stream; ``tids_scanned``
counts the persona's own posts surfaced by the feed result (capped at
``lookback_posts``); ``skipped_seen`` counts the already-seen entries
injected into the prompt (the model is told to skip exactly those).
A turn that finds no new comments is a *success* with
``replies_posted=0``. The builtin never raises — every failure folds
into the dict (``missing_persona_id`` / ``chat_service_unavailable`` /
``persona_store_unavailable`` / ``persona_store_failed`` /
``persona_not_found`` / ``chat_service_failed`` / ``chat_timeout`` /
``chat_error``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from corlinman_server.scheduler.builtins._qzone_chat import (
    ChatDriveOutcome,
    ToolCallRecord,
    build_internal_chat_request,
    build_session_key,
    coerce_optional_str,
    coerce_str,
    drive_chat_turn,
    resolve_chat_service,
    resolve_data_dir,
    resolve_default_model,
    resolve_metadata,
    resolve_or_open_persona_stores,
    valid_persona_slug,
)
from corlinman_server.scheduler.builtins.registry import (
    BuiltinContext,
    register_builtin,
)

_logger = logging.getLogger("corlinman_server.scheduler.builtins.qzone_reply")


__all__ = [
    "QZONE_REPLY_BUILTIN_NAME",
    "_qzone_reply_comments_action",
]


#: Registered name; the admin UI filters on this for the "auto-reply"
#: sub-section and the scheduler dispatcher resolves it via the registry.
QZONE_REPLY_BUILTIN_NAME: str = "qzone.reply_comments"


#: Wire-stable tool names we expect the agent to call. Duplicated from
#: ``corlinman_agent.qzone`` on purpose — importing the agent package
#: would force a heavy dependency on this thin scheduler module (same
#: rationale as ``qzone_daily._QZONE_PUBLISH_TOOL``).
_QZONE_LIST_FEED_TOOL: str = "qzone_list_feed"
_QZONE_GET_POST_TOOL: str = "qzone_get_post"
_QZONE_POST_COMMENT_TOOL: str = "qzone_post_comment"

#: Fixed user turn — the task is fully specified by the system tail, so
#: the user message just kicks the turn off in persona-neutral wording.
_USER_TURN: str = "看看你 QQ 空间说说下有没有新评论，用你自己的口吻回复它们。"

#: Chat-drive timeout knob (seconds; default 300 via the shared driver).
_TIMEOUT_ENV: str = "CORLINMAN_QZONE_REPLY_TIMEOUT_SECS"

#: ``max_replies`` metadata clamp (default 3, hard range 1-10 — mirrors
#: the admin ``_validate_qzone_reply`` gate).
_MAX_REPLIES_DEFAULT: int = 3
_MAX_REPLIES_MIN: int = 1
_MAX_REPLIES_MAX: int = 10

#: ``lookback_posts`` metadata clamp (default 5, hard range 1-20).
_LOOKBACK_DEFAULT: int = 5
_LOOKBACK_MIN: int = 1
_LOOKBACK_MAX: int = 20


# ---------------------------------------------------------------------------
# Seen-comments sidecar
# ---------------------------------------------------------------------------

#: Sidecar layout: one JSON file per persona under
#: ``<DATA_DIR>/qzone_seen_comments/<persona_id>.json`` holding, per 说说
#: tid, the ``"<uin>:<unix_ts>"`` records of commenters already replied to.
#: A tiny owned sidecar (mirrors the B4 post-log rationale): it is the only
#: place that always knows which comments this scheduler already answered,
#: with zero network / auth dependency.
_SEEN_DIR: str = "qzone_seen_comments"
_SEEN_VERSION: int = 1
_SEEN_PER_TID_MAX: int = 200
_SEEN_TIDS_MAX: int = 100


def _seen_path(data_dir: Path | None, persona_id: str) -> Path | None:
    """Resolve ``<DATA_DIR>/qzone_seen_comments/<persona_id>.json`` or
    ``None`` when no data dir is wired / the slug guard rejects the id
    (either way the whole dedup feature is skipped for the firing —
    read-side + write-side both funnel through here)."""
    if data_dir is None or not valid_persona_slug(persona_id):
        return None
    return Path(data_dir) / _SEEN_DIR / f"{persona_id}.json"


def _read_seen(path: Path | None) -> dict[str, list[str]]:
    """Read the ``seen`` map from the sidecar.

    Best-effort + total: a missing / unreadable / malformed file yields
    ``{}`` so a corrupt sidecar never blocks the firing. Non-string
    entries and wrong-shaped values are dropped."""
    if path is None:
        return {}
    try:
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    seen = raw.get("seen")
    if not isinstance(seen, dict):
        return {}
    out: dict[str, list[str]] = {}
    for tid, entries in seen.items():
        if not isinstance(tid, str) or not isinstance(entries, list):
            continue
        clean = [e for e in entries if isinstance(e, str) and e]
        if clean:
            out[tid] = clean
    return out


def _record_seen(
    *,
    data_dir: Path | None,
    persona_id: str,
    replies: list[tuple[str, str]],
) -> None:
    """Fold this turn's successful ``(tid, uin)`` replies into the sidecar.

    Fully best-effort: a bad slug / missing data dir / unwritable path
    all skip silently. A tid that receives a new entry is re-inserted at
    the map's tail so the total-tids cap rolls off the least-recently
    updated posts. A ``uin`` already recorded under the tid is not
    duplicated. Atomic ``write tmp + replace`` so a crash mid-write
    can't truncate the sidecar."""
    path = _seen_path(data_dir, persona_id)
    if path is None or not replies:
        return
    seen = _read_seen(path)
    now_ts = int(time.time())
    changed = False
    for tid, uin in replies:
        if not tid or not uin:
            continue
        entries = seen.pop(tid, [])
        if any(e.split(":", 1)[0] == uin for e in entries):
            # Already recorded — keep the tid's recency bump anyway.
            seen[tid] = entries
            continue
        entries.append(f"{uin}:{now_ts}")
        seen[tid] = entries[-_SEEN_PER_TID_MAX:]
        changed = True
    if not changed:
        return
    if len(seen) > _SEEN_TIDS_MAX:
        for stale in list(seen.keys())[: len(seen) - _SEEN_TIDS_MAX]:
            seen.pop(stale, None)
    payload = json.dumps(
        {"version": _SEEN_VERSION, "seen": seen},
        ensure_ascii=False,
        indent=2,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def _seen_block(seen: dict[str, list[str]]) -> str | None:
    """Render the pre-turn seen-map into the "已回复过" prompt block.

    One line per 说说 tid listing the commenter QQ numbers already
    answered. ``None`` when the map is empty (the prompt then omits the
    block)."""
    if not seen:
        return None
    lines: list[str] = []
    for tid, entries in seen.items():
        uins: list[str] = []
        for entry in entries:
            uin = entry.split(":", 1)[0]
            if uin and uin not in uins:
                uins.append(uin)
        if uins:
            lines.append(f"- 说说 {tid}：已回复过 QQ {'、'.join(uins)}")
    if not lines:
        return None
    return (
        "## 已回复过的评论（这些已经回过了，必须跳过，不要重复回复）\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def _reply_tail(
    *,
    max_replies: int,
    lookback_posts: int,
    qq_account: str | None,
) -> str:
    """The load-bearing system tail — the wording IS the contract for
    "scan own posts, skip seen, reply in-voice, end the turn"."""
    if qq_account:
        feed_hint = (
            f"你的 QQ 号是 {qq_account}。先调用 `qzone_list_feed`"
            f"（owner_uin=\"{qq_account}\"）拉取自己的说说。"
        )
    else:
        feed_hint = (
            "先调用 `qzone_list_feed` 拉取动态；返回里的 `my_uin` 就是"
            "你自己的 QQ，只看 `uin` 等于 `my_uin` 的说说（那些才是你发的）。"
        )
    return (
        "\n\n---\n"
        "[scheduler·qzone.reply_comments 指令]\n"
        "你正在以这个角色的身份查看并回复自己 QQ 空间说说下的新评论。\n"
        f"1. {feed_hint}\n"
        f"2. 只看你自己发的说说，取最近 {lookback_posts} 条；每条的评论就在"
        "返回的 `comments` 字段里（需要单独确认某条时可用 `qzone_get_post`）。\n"
        "3. 跳过这些评论：你自己发的（uin 是你自己）、以及上面"
        "「已回复过的评论」里列出的。\n"
        "4. 对剩下的新评论，用你自己的口吻自然地回复：调用 "
        "`qzone_post_comment`（owner_uin=你自己的 QQ，tid=那条说说，"
        "content=回复内容，reply_to_uin/reply_to_name=评论者的 QQ 和昵称）。"
        f"本轮最多回复 {max_replies} 条，挑最新的先回。\n"
        "5. 回复完（或者根本没有新评论）就直接结束本轮：不要发表新说说，"
        "不要调用 `qzone_publish`，禁止向用户提问。"
    )


def _compose_system_prompt(
    persona_prompt: str,
    *,
    seen_block: str | None,
    max_replies: int,
    lookback_posts: int,
    qq_account: str | None,
) -> str:
    """Persona body → seen-block → the reply tail.

    Reading order mirrors ``qzone_daily``: "who I am" → "what I've
    already answered (skip these)" → "what to do this turn"."""
    parts = [(persona_prompt or "").rstrip()]
    if seen_block:
        parts.append(seen_block.rstrip())
    parts.append(
        _reply_tail(
            max_replies=max_replies,
            lookback_posts=lookback_posts,
            qq_account=qq_account,
        )
    )
    return "".join(
        (f"\n\n{p}" if i and not p.startswith("\n") else p)
        for i, p in enumerate(parts)
    )


# ---------------------------------------------------------------------------
# Metadata coercion
# ---------------------------------------------------------------------------


def _clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    """Read an int metadata knob with a default + hard clamp.

    ``bool`` (an ``int`` subclass — a stray ``true`` must not read as 1)
    and non-numeric junk fall back to the default; out-of-range values
    clamp so a hand-edited sidecar can't push the turn past the caps."""
    if value is None or isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


# ---------------------------------------------------------------------------
# Action entry point
# ---------------------------------------------------------------------------


async def _qzone_reply_comments_action(context: BuiltinContext) -> dict[str, Any]:
    """Scheduler builtin entrypoint — drive one comment-reply agent turn.

    Reads the job metadata off ``context`` (see module docstring for the
    keys). Returns an audit dict shaped for direct persistence to the
    scheduler history. Never raises — every failure path folds into the
    dict.
    """
    metadata = resolve_metadata(context, direct_attr="qzone_reply_metadata")
    persona_id = coerce_str(metadata.get("persona_id"))
    qq_account = coerce_optional_str(metadata.get("qq_account"))
    max_replies = _clamp_int(
        metadata.get("max_replies"),
        default=_MAX_REPLIES_DEFAULT,
        lo=_MAX_REPLIES_MIN,
        hi=_MAX_REPLIES_MAX,
    )
    lookback_posts = _clamp_int(
        metadata.get("lookback_posts"),
        default=_LOOKBACK_DEFAULT,
        lo=_LOOKBACK_MIN,
        hi=_LOOKBACK_MAX,
    )

    base: dict[str, Any] = {
        "persona_id": persona_id,
        "qq_account": qq_account,
        "replies_posted": 0,
        "tids_scanned": 0,
        "skipped_seen": 0,
    }

    if not persona_id:
        return {**base, "ok": False, "error": "missing_persona_id"}

    chat_service = resolve_chat_service(context)
    if chat_service is None:
        return {**base, "ok": False, "error": "chat_service_unavailable"}

    store_bundle = await resolve_or_open_persona_stores(context)
    if store_bundle is None:
        return {**base, "ok": False, "error": "persona_store_unavailable"}
    persona_store, _asset_store, owned_handles = store_bundle

    try:
        try:
            persona = await persona_store.get(persona_id)
        except Exception as exc:  # noqa: BLE001 — never raise out of a builtin
            _logger.warning(
                "scheduler.builtin.qzone_reply.persona_get_failed",
                extra={"persona_id": persona_id, "error": repr(exc)},
            )
            return {**base, "ok": False, "error": "persona_store_failed",
                    "message": str(exc)}

        if persona is None:
            return {**base, "ok": False, "error": "persona_not_found"}

        data_dir = resolve_data_dir(context.app_state)
        seen = _read_seen(_seen_path(data_dir, persona_id))
        base["skipped_seen"] = sum(len(v) for v in seen.values())

        system_prompt = _compose_system_prompt(
            persona.system_prompt,
            seen_block=_seen_block(seen),
            max_replies=max_replies,
            lookback_posts=lookback_posts,
            qq_account=qq_account,
        )
        request = build_internal_chat_request(
            model=resolve_default_model(context),
            session_key=build_session_key(persona_id),
            system_prompt=system_prompt,
            user_turn=_USER_TURN,
            persona_id=persona_id,
        )
        if request is None:
            return {**base, "ok": False,
                    "error": "internal_chat_request_unavailable"}

        cancel = asyncio.Event()
        outcome = await drive_chat_turn(
            chat_service=chat_service,
            request=request,
            cancel=cancel,
            timeout_env=_TIMEOUT_ENV,
        )
        result = _shape_audit(
            outcome,
            base=base,
            qq_account=qq_account,
            lookback_posts=lookback_posts,
        )
        if result.get("ok") and result.get("_replies"):
            _record_seen(
                data_dir=data_dir,
                persona_id=persona_id,
                replies=result["_replies"],
            )
        result.pop("_replies", None)
        return result
    finally:
        # Close any handles we opened ourselves (fallback path) — never
        # touch live handles parked on the AppState bundle.
        for handle in owned_handles:
            with contextlib.suppress(Exception):
                await handle.close()


# ---------------------------------------------------------------------------
# Outcome → audit dict
# ---------------------------------------------------------------------------


def _shape_audit(
    outcome: ChatDriveOutcome,
    *,
    base: dict[str, Any],
    qq_account: str | None,
    lookback_posts: int,
) -> dict[str, Any]:
    """Translate the generic drive outcome into the reply audit dict.

    The private ``_replies`` key carries the harvested ``(tid, uin)``
    pairs for the sidecar write; the caller pops it before returning."""
    if outcome.error == "run_failed":
        return {
            **base,
            "ok": False,
            "error": "chat_service_failed",
            "message": outcome.message,
            "duration_ms": outcome.duration_ms,
        }
    if outcome.error == "timeout":
        return {
            **base,
            "ok": False,
            "error": "chat_timeout",
            "tools_called": outcome.tools_called,
            "duration_ms": outcome.duration_ms,
        }
    if outcome.error == "consume_failed":
        return {
            **base,
            "ok": False,
            "error": "chat_service_failed",
            "message": outcome.message,
            "tools_called": outcome.tools_called,
            "duration_ms": outcome.duration_ms,
        }
    if outcome.error == "chat_error":
        return {
            **base,
            "ok": False,
            "error": "chat_error",
            "chat_error_reason": outcome.chat_error_reason,
            "chat_error_message": outcome.chat_error_message,
            "tools_called": outcome.tools_called,
            "duration_ms": outcome.duration_ms,
        }

    calls_by_id: dict[str, ToolCallRecord] = {
        c.call_id: c for c in outcome.calls if c.call_id
    }
    replies: list[tuple[str, str]] = []
    for res in outcome.results:
        if res.tool != _QZONE_POST_COMMENT_TOOL or res.is_error:
            continue
        # The dispatcher's envelope always carries ``ok``; an absent /
        # empty envelope (older gateway without payload_json) counts as
        # success on the strength of ``is_error=False``.
        if res.envelope and res.envelope.get("ok") is not True:
            continue
        call = calls_by_id.get(res.call_id)
        args = (call.args if call else None) or {}
        tid = str(res.envelope.get("tid") or args.get("tid") or "")
        uin = str(args.get("reply_to_uin") or "")
        replies.append((tid, uin))

    return {
        **base,
        "ok": True,
        "replies_posted": len(replies),
        "tids_scanned": _count_scanned(
            outcome, qq_account=qq_account, lookback_posts=lookback_posts
        ),
        "tools_called": outcome.tools_called,
        "finish_reason": outcome.finish_reason,
        "duration_ms": outcome.duration_ms,
        "_replies": replies,
    }


def _count_scanned(
    outcome: ChatDriveOutcome,
    *,
    qq_account: str | None,
    lookback_posts: int,
) -> int:
    """Count the persona's own posts the feed surfaced (≤ lookback).

    Harvested from the first successful ``qzone_list_feed`` result: own
    posts are the feed items whose ``uin`` matches the envelope's
    ``my_uin`` (or the job's ``qq_account`` when the envelope lacks it).
    Zero when the model never listed the feed — honest observability,
    not a hard guarantee of what the model actually read."""
    for res in outcome.results:
        if res.tool != _QZONE_LIST_FEED_TOOL or res.is_error:
            continue
        env = res.envelope
        if not env or env.get("ok") is not True:
            continue
        feed = env.get("feed")
        if not isinstance(feed, list):
            continue
        my_uin = str(env.get("my_uin") or qq_account or "")
        own = [
            item
            for item in feed
            if isinstance(item, dict)
            and (not my_uin or str(item.get("uin") or "") == my_uin)
        ]
        return min(lookback_posts, len(own))
    return 0


# Register at import time. ``__init__.py`` imports this module so any
# ``import corlinman_server.scheduler.builtins`` populates the registry.
register_builtin(QZONE_REPLY_BUILTIN_NAME, _qzone_reply_comments_action)
