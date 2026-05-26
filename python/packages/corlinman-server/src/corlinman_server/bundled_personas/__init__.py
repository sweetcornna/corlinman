"""Bundled persona templates shipped with corlinman-server.

Each subdirectory here is one persona's "starter kit": today the only
mandatory file is ``daily_job.json`` (the W6 QZone daily-publish
template) but future bundles can add ``assets/`` or a fresh
``SYSTEM_PROMPT.md`` body alongside it without changing the seeder
contract.

The directory ships as package data; Hatch's default wheel target
(``packages = ["src/corlinman_server"]``) picks the ``*.json`` /
``*.md`` files up automatically. Override the location at runtime with
the ``CORLINMAN_BUNDLED_PERSONAS_DIR`` environment variable.

On first boot the gateway lifecycle calls
:func:`corlinman_server.gateway.lifecycle.starter_skills.
seed_bundled_personas` (W6 sibling of ``seed_starter_skills``) to copy
this tree into ``<DATA_DIR>/bundled_personas/`` so an operator can
inspect / hand-edit the templates without re-installing the wheel. The
copy is idempotent — existing target files always win.

Important: this seeder copies the templates **only** — it does NOT
activate any of the embedded daily jobs. Grantley's daily-说说 is
deliberately opt-in (a fresh deploy must not start posting to QZone),
so activation goes through the explicit admin route
``POST /admin/scheduler/qzone/templates/grantley/enable``.
"""
