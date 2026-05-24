"""Builtin ``ask_user`` tool — pause and ask the user a question.

Mirrors Claude Code's ``AskUserQuestion`` pattern adapted for chat-bot
channels: when the model is missing information or is between two
materially different interpretations, it calls ``ask_user`` instead of
guessing. The dispatch side returns a JSON marker
(``{"ok": true, "status": "awaiting_user_reply", ...}``) so the
reasoning loop's tool-result envelope is well-formed and the model can
finalise the turn with the question text.

Channel-side rendering is handled by the channel handlers:

* **Telegram** — the channel handler sees the matching ``tool_call``
  frame, captures ``question`` + ``options``, and routes the final reply
  through ``send_message_with_buttons`` so each option becomes a
  Telegram inline-keyboard button.
* **QQ / Discord / Slack / Feishu / WeChat-official** — no clickable
  buttons; the spinner / op-summary block renders the options as a
  bulleted list under the question. The user types their answer as a
  normal message which arrives as the next chat turn.

The user's reply lands in the next ``Chat`` RPC and the agent picks it
up via normal session-history recall — no special routing is needed.
"""

from __future__ import annotations

import json
from typing import Any

#: Public tool name — the agent servicer registers this in
#: :data:`BUILTIN_TOOLS` and advertises the schema to every model on
#: every chat round.
ASK_USER_TOOL: str = "ask_user"

#: Per-Telegram-API limit for an inline-keyboard ``callback_data``
#: payload — 1..64 bytes UTF-8. The channel handler enforces this when
#: serialising the buttons, but capping the option label up-front means
#: a runaway model can't blow past it on either rendering path.
_MAX_OPTION_LEN: int = 120

#: Max number of canned answer choices we'll surface. Telegram's inline
#: keyboard renders fine with ~8 rows; QQ-family numbered lists get
#: cluttered past that point. Hard cap so the user never sees a 50-line
#: option dump from a confused model.
_MAX_OPTIONS: int = 8

#: Max question length. Long enough for a real clarification prompt,
#: short enough to stay well under every channel's message cap even when
#: wrapped with bullets + an inline keyboard.
_MAX_QUESTION_LEN: int = 1000


def ask_user_tool_schema() -> dict[str, Any]:
    """OpenAI tool descriptor for ``ask_user``.

    Mirrors the shape of ``todo_write`` / ``send_attachment`` so the
    cached-schema injector picks it up unchanged.
    """
    return {
        "type": "function",
        "function": {
            "name": ASK_USER_TOOL,
            "description": (
                "Ask the user a clarification question and STOP work for "
                "this turn. Use this only when information is missing or "
                "the request is genuinely ambiguous between materially "
                "different options — never guess. After calling, "
                "immediately finalize your reply with the question text "
                "(verbatim). Do NOT call any other tool after this in the "
                "same turn — the user's next message will arrive as a "
                "new turn and you can act on the answer then."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The question to ask, in the same language "
                            "the user wrote in (Chinese or English). "
                            "Should be one or two short sentences."
                        ),
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional canned answer choices. Max 8. "
                            "Telegram renders these as clickable inline-"
                            "keyboard buttons; QQ / Discord / Slack / "
                            "Feishu / WeChat-official render them as a "
                            "bulleted list under the question."
                        ),
                    },
                    "multiple": {
                        "type": "boolean",
                        "description": (
                            "Allow multiple options to be selected. "
                            "Default false (radio behaviour)."
                        ),
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    }


def parse_ask_user_args(args_json: bytes | str) -> dict[str, Any]:
    """Parse + validate the ``ask_user`` args JSON.

    Returns a normalised ``{"question", "options", "multiple"}`` dict.
    Pure: never raises and never reads I/O. On any decode / shape error
    returns ``{"question": "", "options": [], "multiple": False}`` so
    callers can branch on the empty question string.

    Shared with the channel handlers — the Telegram handler reads this
    to discover the option labels, and the spinner/summary path on QQ-
    family channels uses it to render the bulleted list.
    """
    raw: str
    if isinstance(args_json, (bytes, bytearray)):
        try:
            raw = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError:
            return {"question": "", "options": [], "multiple": False}
    else:
        raw = args_json or ""
    try:
        obj = json.loads(raw or "{}")
    except (ValueError, json.JSONDecodeError):
        return {"question": "", "options": [], "multiple": False}
    if not isinstance(obj, dict):
        return {"question": "", "options": [], "multiple": False}

    q_raw = obj.get("question")
    question = q_raw.strip() if isinstance(q_raw, str) else ""
    if len(question) > _MAX_QUESTION_LEN:
        question = question[:_MAX_QUESTION_LEN - 1] + "…"

    opts_raw = obj.get("options") or []
    options: list[str] = []
    if isinstance(opts_raw, list):
        for o in opts_raw[:_MAX_OPTIONS]:
            label = str(o).replace("\n", " ").strip()
            if not label:
                continue
            if len(label) > _MAX_OPTION_LEN:
                label = label[:_MAX_OPTION_LEN - 1] + "…"
            options.append(label)

    multiple = bool(obj.get("multiple", False))
    return {"question": question, "options": options, "multiple": multiple}


def dispatch_ask_user(args_json: bytes | str) -> str:
    """Stub dispatch — JSON envelope marker. Never raises.

    Returns a JSON-encoded string the reasoning loop feeds back as the
    ``ToolResult`` content. The marker tells the model that the question
    has been "accepted"; the model is then expected to finalise the
    reply with the question text and not call any further tools this
    turn (the system prompt carries the same instruction).

    Empty ``question`` is the only error condition — returns
    ``{"ok": false, "error": ...}`` so a misuse round produces a
    diagnosable envelope instead of a silent stall.
    """
    parsed = parse_ask_user_args(args_json)
    question = parsed["question"]
    if not question:
        return json.dumps(
            {"ok": False, "error": "ask_user requires a non-empty `question`"}
        )
    return json.dumps(
        {
            "ok": True,
            "status": "awaiting_user_reply",
            "question": question,
            "options": parsed["options"],
            "multiple": parsed["multiple"],
            "note": (
                "Question has been queued for the user. Finalize your "
                "reply with the question text and do not call any more "
                "tools this turn."
            ),
        },
        ensure_ascii=False,
    )


__all__ = [
    "ASK_USER_TOOL",
    "ask_user_tool_schema",
    "dispatch_ask_user",
    "parse_ask_user_args",
]
