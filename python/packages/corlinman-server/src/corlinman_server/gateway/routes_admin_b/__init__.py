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
)
from corlinman_server.gateway.routes_admin_b import (
    config as _config,
)
from corlinman_server.gateway.routes_admin_b import (
    corlinman_channel as _corlinman_channel,
)
from corlinman_server.gateway.routes_admin_b import (
    credentials as _credentials,
)
from corlinman_server.gateway.routes_admin_b import (
    curator as _curator,
)
from corlinman_server.gateway.routes_admin_b import (
    evolution as _evolution,
)
from corlinman_server.gateway.routes_admin_b import (
    hooks as _hooks,
)
from corlinman_server.gateway.routes_admin_b import (
    image_provider as _image_provider,
)
from corlinman_server.gateway.routes_admin_b import (
    logs as _logs,
)
from corlinman_server.gateway.routes_admin_b import (
    marketplace_settings as _marketplace_settings,
)
from corlinman_server.gateway.routes_admin_b import (
    mcp_market as _mcp_market,
)
from corlinman_server.gateway.routes_admin_b import (
    memory as _memory,
)
from corlinman_server.gateway.routes_admin_b import (
    models as _models,
)
from corlinman_server.gateway.routes_admin_b import (
    napcat as _napcat,
)
from corlinman_server.gateway.routes_admin_b import (
    oauth as _oauth,
)
from corlinman_server.gateway.routes_admin_b import (
    onboard as _onboard,
)
from corlinman_server.gateway.routes_admin_b import (
    personas as _personas,
)
from corlinman_server.gateway.routes_admin_b import (
    plugin_market as _plugin_market,
)
from corlinman_server.gateway.routes_admin_b import (
    plugins as _plugins,
)
from corlinman_server.gateway.routes_admin_b import (
    providers as _providers,
)
from corlinman_server.gateway.routes_admin_b import (
    rag as _rag,
)
from corlinman_server.gateway.routes_admin_b import (
    scheduler as _scheduler,
)
from corlinman_server.gateway.routes_admin_b import (
    sessions_cost as _sessions_cost,
)
from corlinman_server.gateway.routes_admin_b import (
    sessions_events as _sessions_events,
)
from corlinman_server.gateway.routes_admin_b import (
    skills as _skills,
)
from corlinman_server.gateway.routes_admin_b import (
    subagents as _subagents,
)
from corlinman_server.gateway.routes_admin_b import (
    system as _system,
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
        _hooks,
        _image_provider,
        _logs,
        _marketplace_settings,
        _mcp_market,
        _memory,
        _models,
        _napcat,
        _oauth,
        _onboard,
        _personas,
        _plugin_market,
        _plugins,
        _providers,
        _rag,
        _scheduler,
        _sessions_cost,
        _sessions_events,
        _skills,
        _subagents,
        _system,
        _corlinman_channel,
    ):
        root.include_router(mod.router())
    return root
