"""Which hook events have a live production emit site (Dim 9).

The declarative config accepts every canonical event name, but only the
events listed here are actually fired by the current server wiring —
``/hooks`` and ``GET /admin/hooks`` surface this so an operator can tell
"configured but dormant" from "configured and live" without reading code.

Update this set when a new emit site lands (e.g. ``pre_compact`` /
``session_*`` — accepted in config today, no emitter yet).
"""

from __future__ import annotations

LIVE_HOOK_EVENTS: frozenset[str] = frozenset(
    {
        "pre_tool",  # blocking gate in agent_servicer._dispatch_builtin_inner
        "post_tool",  # fire-and-forget in the _dispatch_builtin wrapper
        "stop",  # ReasoningLoop turn-end veto/inject (servicer-wired)
        "user_prompt_submit",  # Chat entry (deny/inject → system note)
        "pre_compact",  # ReasoningLoop before an imminent compaction (deny defers)
        "post_compact",  # ReasoningLoop after a real compaction
        "session_start",  # Chat entry, first turn of a session_key per process
        "session_reset",  # console /new + /clear (embedded brain runner)
        "notification",  # ask_user needs-input + subagent terminal states
        "file_changed",  # after post_tool for write_file/edit_file/notebook_edit
        "setup",  # once per process after boot (gateway lifespan / embedded start)
    }
)
