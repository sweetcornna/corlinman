"""Bundled per-persona event-seed packs for ``persona_life_event_seed``.

Each ``<persona_id>.yaml`` is a mapping of seed-category → list of short
Chinese keyword cues. The ``persona_life`` tool resolves a persona's
library as: operator override (``<DATA_DIR>/persona_life/<id>.events.yaml``)
→ a bundled pack here → a generic neutral fallback. Shipping
``grantley.yaml`` keeps the built-in ``grantley`` persona's 骑士学院 world
working out-of-the-box; new personas either drop in their own override or
get the generic scaffold.

This module exists only to make the directory an importable package so
:func:`importlib.resources.files` can locate the data files in both
editable and wheel installs.
"""

from __future__ import annotations

__all__: list[str] = []
