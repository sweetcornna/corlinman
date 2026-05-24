# Default seeded persona — credit: openclaw grantley-perspective SKILL.md
# (https://github.com/.../openclaw/.claude/skills/grantley-perspective/SKILL.md)
"""Loader for the built-in ``grantley`` persona body.

The persona system_prompt text lives in a sibling :file:`default_grantley.md`
file rather than as an inline triple-quoted string so future diffs to the
persona body stay readable in code review (a single multi-thousand-char
string blob defeats per-line diffs). The markdown body is read once at
import time and cached for the lifetime of the process.

The openclaw-specific bindings ("admin UID 2104743984 = Elargo") that
appeared in the source SKILL.md have already been replaced with the
neutral placeholder ``channel_owner`` / ``群主`` in the on-disk markdown
file — the loader does not rewrite anything; it returns the file verbatim.
"""

from __future__ import annotations

from pathlib import Path

#: Stable id of the seeded built-in persona. Other code (admin routes,
#: channels config validation, tests) reference this constant rather than
#: re-spelling the literal string so a future rename happens in one place.
DEFAULT_GRANTLEY_ID: str = "grantley"

#: Short summary the admin UI shows in the persona picker. Kept here (not
#: in the markdown body) so the body file can be edited freely without
#: re-flowing into a single-line summary.
DEFAULT_GRANTLEY_SUMMARY: str = (
    "糙汉式温柔 · 嘴硬+行动双轨 · 隐形学霸（蒸馏自 openclaw grantley-perspective）"
)

#: Display name shown alongside the summary.
DEFAULT_GRANTLEY_DISPLAY_NAME: str = "格兰特利·贝尔（Grantley Bell）"

_DEFAULT_BODY_PATH: Path = Path(__file__).with_name("default_grantley.md")


def load_default_grantley_body() -> str:
    """Return the seeded ``grantley`` system_prompt body.

    Reads the sibling :file:`default_grantley.md` file. Raises
    :class:`FileNotFoundError` (with a clear message) if the file is
    missing — that is a packaging bug, not a runtime condition the
    caller should silently recover from.
    """
    try:
        return _DEFAULT_BODY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"default_grantley.md missing alongside default_grantley.py "
            f"(expected at {_DEFAULT_BODY_PATH}); the corlinman-server "
            f"package was built without its persona data files"
        ) from exc


__all__ = [
    "DEFAULT_GRANTLEY_DISPLAY_NAME",
    "DEFAULT_GRANTLEY_ID",
    "DEFAULT_GRANTLEY_SUMMARY",
    "load_default_grantley_body",
]
