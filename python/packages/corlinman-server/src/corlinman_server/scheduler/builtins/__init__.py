"""Scheduler builtin actions — process-local callables wired by name.

W2.2 of ``docs/PLAN_AUTO_UPDATE.md`` §2 Wave 2 (``system.update_check``)
landed the registry pattern; later waves stack more entries onto the
same dict via :func:`register_builtin`. The split-package layout here
mirrors the convention used elsewhere in the codebase (one submodule
per builtin so each one's surface is reviewable in isolation while the
shared registry stays at the package root):

* :mod:`.registry` — the :class:`BuiltinContext` dataclass, the
  :data:`BUILTIN_ACTIONS` map, and the :func:`register_builtin` /
  :func:`run_builtin` helpers. Pure registry plumbing — never grows a
  builtin body of its own.
* :mod:`.system_update_check` — W2.2 — the GitHub-releases poll wired
  into the gateway's :class:`UpdateChecker`.
* :mod:`.qzone_daily` — W6 Persona Studio — drives a one-turn agent
  chat under a persona's system prompt and asserts it ends by calling
  ``qzone_publish``; captures the resulting ``tid`` / ``qzone_url``
  into the scheduler history payload.

Importing this package executes every submodule, which is what
populates :data:`BUILTIN_ACTIONS` — the gateway's scheduler tick loop
imports :mod:`corlinman_server.scheduler.builtins` once at boot and
trusts that every wave's registration ran.

The contract is deliberately permissive:

* Builtins **must not raise**. Any exception is caught at the registry
  boundary (:func:`run_builtin`) and turned into a
  ``{"ok": False, "reason": "..."}`` envelope so the scheduler tick
  loop never dies on a bad poll / sqlite write / agent crash.
* Builtins should be idempotent at the *poll* level — the
  individual implementations short-circuit on their own TTL when the
  cron fires under the interval window.

Backwards-compat note: the historical
``corlinman_server.scheduler.builtins`` was a single module re-exporting
the same surface. Existing imports (``from corlinman_server.scheduler
.builtins import BUILTIN_ACTIONS, BuiltinContext, register_builtin,
run_builtin, _system_update_check_action``) keep working because this
``__init__`` re-exports every symbol they reach for.
"""

from __future__ import annotations

from corlinman_server.scheduler.builtins.evolution_darwin_curate import (
    EVOLUTION_DARWIN_CURATE_BUILTIN_NAME,
    _evolution_darwin_curate_action,
)
from corlinman_server.scheduler.builtins.evolution_engine_run_once import (
    EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME,
    _evolution_engine_run_once_action,
)
from corlinman_server.scheduler.builtins.evolution_shadow_test import (
    EVOLUTION_SHADOW_TEST_BUILTIN_NAME,
    _evolution_shadow_test_action,
)
from corlinman_server.scheduler.builtins.memory_dream import (
    MEMORY_DREAM_BUILTIN_NAME,
    _memory_dream_action,
)
from corlinman_server.scheduler.builtins.memory_reconcile import (
    MEMORY_RECONCILE_BUILTIN_NAME,
    _memory_reconcile_action,
)
from corlinman_server.scheduler.builtins.persona_decay import (
    PERSONA_DECAY_BUILTIN_NAME,
    _persona_decay_action,
)
from corlinman_server.scheduler.builtins.persona_life_advance import (
    PERSONA_LIFE_ADVANCE_BUILTIN_NAME,
    _persona_life_advance_action,
)
from corlinman_server.scheduler.builtins.qzone_daily import (
    QZONE_DAILY_BUILTIN_NAME,
    _qzone_daily_publish_action,
)
from corlinman_server.scheduler.builtins.registry import (
    BUILTIN_ACTIONS,
    BuiltinAction,
    BuiltinContext,
    register_builtin,
    run_builtin,
)
from corlinman_server.scheduler.builtins.system_update_check import (
    _resolve_update_checker,
    _system_update_check_action,
)

__all__ = [
    "BUILTIN_ACTIONS",
    "EVOLUTION_DARWIN_CURATE_BUILTIN_NAME",
    "EVOLUTION_ENGINE_RUN_ONCE_BUILTIN_NAME",
    "EVOLUTION_SHADOW_TEST_BUILTIN_NAME",
    "MEMORY_DREAM_BUILTIN_NAME",
    "MEMORY_RECONCILE_BUILTIN_NAME",
    "PERSONA_DECAY_BUILTIN_NAME",
    "PERSONA_LIFE_ADVANCE_BUILTIN_NAME",
    "QZONE_DAILY_BUILTIN_NAME",
    "BuiltinAction",
    "BuiltinContext",
    "_evolution_darwin_curate_action",
    "_evolution_engine_run_once_action",
    "_evolution_shadow_test_action",
    "_memory_dream_action",
    "_memory_reconcile_action",
    "_persona_decay_action",
    "_persona_life_advance_action",
    "_qzone_daily_publish_action",
    "_resolve_update_checker",
    "_system_update_check_action",
    "register_builtin",
    "run_builtin",
]
