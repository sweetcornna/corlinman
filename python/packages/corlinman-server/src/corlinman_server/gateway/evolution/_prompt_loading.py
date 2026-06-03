"""Prompt-template I/O for the background review system.

Extracted verbatim from
:mod:`corlinman_server.gateway.evolution.background_review` as part of a
behaviour-preserving god-file split. This module performs pure prompt
file resolution and reading; it MUST NOT import the source module
(``background_review``) to avoid an import cycle — instead the source
module re-imports the public ``ReviewKind`` / ``load_prompt`` names from
here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

# ─── Public types ────────────────────────────────────────────────────


ReviewKind = Literal[
    "memory", "skill", "combined", "curator", "user-correction", "darwin",
]


# ─── Prompt loading ─────────────────────────────────────────────────


_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# Map kind -> filename. Kept as a const so callers can iterate it for docs.
_PROMPT_FILES: dict[ReviewKind, str] = {
    "memory": "memory_review.md",
    "skill": "skill_review.md",
    "combined": "combined_review.md",
    "curator": "curator_review.md",
    "user-correction": "user_preference_patch.md",
    "darwin": "darwin_review.md",
}


def load_prompt(kind: ReviewKind) -> str:
    """Read the markdown prompt template for ``kind``.

    Templates ship inside the package next to this module; we resolve
    relative to ``__file__`` so editable installs and built wheels both
    work without packaging gymnastics.
    """
    filename = _PROMPT_FILES.get(kind)
    if filename is None:
        raise ValueError(f"unknown review kind: {kind!r}")
    return (_PROMPT_DIR / filename).read_text(encoding="utf-8")
