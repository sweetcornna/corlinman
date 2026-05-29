"""``/admin/providers/{name}/probe-image`` — image-capability probe.

Wave 2 first-run wizard, contract §C1 in
``docs/PLAN_FIRST_RUN_WIZARD.md``. The wizard's "image generation API"
step gives the operator three choices:

* **skip**     — no image surface; agent loop reports
  ``ImageProviderUnavailable`` on first tool call.
* **reuse**    — the wizard hits this route to ask whether the
  operator's already-configured chat provider can also do image
  generation. If yes, we flip ``image_capable = true`` on the slot
  (the wizard handles the write); if not, the wizard prompts for a
  separate slot.
* **separate** — the operator configures a brand-new provider slot
  with the image-gen credentials.

The probe itself never invokes real image generation — see
:mod:`corlinman_providers.capabilities` for the
``GET /v1/models`` + HEAD-fallback strategy. We just unwrap the
provider config off the live ``config_snapshot`` and forward to it.
"""

from __future__ import annotations

from typing import Any

from corlinman_providers.capabilities import probe_image_capability
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    require_admin,
)

# ---------------------------------------------------------------------------
# Helpers — keep them module-level so unit tests can import them without
# materialising the FastAPI router.
# ---------------------------------------------------------------------------


def _provider_entry(name: str) -> dict[str, Any] | None:
    """Return the live ``[providers.<name>]`` dict, or ``None`` when missing.

    Reads from :func:`config_snapshot` so a slot added via
    ``/admin/providers`` (or the first-run wizard's finalize call)
    becomes probe-able on the very next call without a process
    restart.
    """
    cfg = dict(config_snapshot())
    providers_cfg = cfg.get("providers") or {}
    if not isinstance(providers_cfg, dict):
        return None
    entry = providers_cfg.get(name)
    if not isinstance(entry, dict):
        return None
    return entry


def _resolve_api_key(entry: dict[str, Any]) -> str:
    """Mirror ``routes_admin_b/providers.py::_resolve_api_key``.

    Avoids the cross-module import (and the matching circular-import
    risk) by inlining the three-shape resolution: literal string,
    ``{"value": "..."}``, ``{"env": "..."}``.
    """
    import os  # local import keeps the module's top-level imports light.

    raw_key = entry.get("api_key")
    if isinstance(raw_key, dict):
        if "value" in raw_key:
            return str(raw_key.get("value") or "")
        if "env" in raw_key:
            env_name = str(raw_key.get("env") or "")
            return os.environ.get(env_name, "") if env_name else ""
    elif isinstance(raw_key, str):
        return raw_key
    return ""


class _ProviderShim:
    """Tiny attribute-bag that satisfies :func:`probe_image_capability`.

    The capabilities probe accepts either a real
    :class:`~corlinman_providers.base.CorlinmanProvider` adapter or any
    object exposing ``api_key`` / ``base_url`` (or the private
    underscore variants). Building a full adapter just to introspect a
    config block would force every supported kind through its real
    constructor — and most of them require live credentials at
    construction time. The shim is the pragmatic seam between the
    config layer (dicts) and the capability layer (provider-shaped
    objects).
    """

    __slots__ = ("api_key", "base_url", "name")

    def __init__(self, name: str, api_key: str, base_url: str | None) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Build the ``/admin/providers/{name}/probe-image`` router.

    Registered alongside the other ``routes_admin_b`` sub-routers via
    :func:`routes_admin_b.build_router`. The probe is admin-gated to
    match the rest of the bundle — even though the response is benign
    (no secrets, no mutation), the route hits an upstream that the
    operator's credentials reach, so anonymous access would let an
    attacker fingerprint internal provider configs.
    """
    r = APIRouter(
        dependencies=[Depends(require_admin)],
        tags=["admin", "providers", "image"],
    )

    @r.post("/admin/providers/{name}/probe-image", response_model=None)
    async def probe_image_endpoint(name: str) -> dict[str, Any] | JSONResponse:
        """Run a non-destructive image-capability probe on ``name``.

        Returns ``{supported: bool, evidence: str, models: list[str]}``
        — matching the contract §C1 wire shape consumed by the
        first-run wizard. On a missing slot we 404 so the UI shows
        "configure a provider first" rather than reporting the slot as
        unsupported.
        """
        entry = _provider_entry(name)
        if entry is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "provider_not_found",
                    "resource": "provider",
                    "id": name,
                    "supported": False,
                    "evidence": f"provider {name!r} not in config",
                    "models": [],
                },
            )

        api_key = _resolve_api_key(entry)
        base_url = entry.get("base_url")
        base_url_str = (
            str(base_url) if isinstance(base_url, str) and base_url.strip() else None
        )

        shim = _ProviderShim(name=name, api_key=api_key, base_url=base_url_str)
        result = await probe_image_capability(shim)
        # ``probe_image_capability`` already returns the wire shape.
        return result

    return r
