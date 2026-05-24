"""``routes_admin_b`` — Python port of half of the Rust admin route tree.

Mirrors these Rust modules from
``rust/crates/corlinman-gateway/src/routes/admin/``:

* :mod:`config`      — runtime config view + edit (``/admin/config*``)
* :mod:`evolution`   — proposal queue mgmt (``/admin/evolution*``)
* :mod:`logs`        — SSE log stream (``/admin/logs/stream``)
* :mod:`memory`      — operator escape hatches (``/admin/memory/*``)
* :mod:`models`      — model alias / provider snapshot (``/admin/models*``)
* :mod:`napcat`      — QQ scan-login proxy (``/admin/channels/qq/*``)
* :mod:`onboard`     — onboarding wizard backend (``/admin/onboard/*``)
* :mod:`plugins`     — plugin registry inspector (``/admin/plugins*``)
* :mod:`providers`   — LLM provider CRUD (``/admin/providers*``)
* :mod:`rag`         — RAG store admin (``/admin/rag*``)
* :mod:`scheduler`   — cron mgmt (``/admin/scheduler*``)

Each submodule exposes:

* ``router()`` — a :class:`fastapi.APIRouter` ready to be mounted under
  ``/admin``-flavoured paths. Each router takes no positional arguments;
  state is plumbed through :class:`~.state.AdminState` via FastAPI's
  dependency-override mechanism. Bootstrappers should populate the
  module-level ``_state`` slot before mounting via :func:`set_admin_state`.

The composed parent router is :func:`build_router`, which merges every
submodule's router under the same ``/`` prefix the Rust mod does (each
sub-router declares its own ``/admin/...`` paths).

Admin auth is plumbed via a lazy import of
``corlinman_server.gateway.middleware.require_admin`` — when that module
isn't installed yet (parallel agent work-in-progress), the dependency
falls through as a no-op so the routers are still importable + testable.
"""

from __future__ import annotations

from fastapi import APIRouter

from corlinman_server.gateway.routes_admin_b import (
    agents as _agents,
    config as _config,
    credentials as _credentials,
    curator as _curator,
    evolution as _evolution,
    logs as _logs,
    memory as _memory,
    models as _models,
    napcat as _napcat,
    oauth as _oauth,
    onboard as _onboard,
    plugins as _plugins,
    providers as _providers,
    rag as _rag,
    scheduler as _scheduler,
)
from corlinman_server.gateway.routes_admin_b.state import AdminState, set_admin_state

__all__ = [
    "AdminState",
    "build_router",
    "set_admin_state",
]


def build_router() -> APIRouter:
    """Compose every admin-B sub-router into one parent APIRouter.

    Mirrors :func:`corlinman_gateway::routes::admin::router_with_state`
    on the Rust side, minus the auth/tenant scope middleware (the
    middleware layer is the bootstrapper's responsibility).
    """
    root = APIRouter()
    for mod in (
        _agents,
        _config,
        _credentials,
        _curator,
        _evolution,
        _logs,
        _memory,
        _models,
        _napcat,
        _oauth,
        _onboard,
        _plugins,
        _providers,
        _rag,
        _scheduler,
    ):
        root.include_router(mod.router())
    return root
