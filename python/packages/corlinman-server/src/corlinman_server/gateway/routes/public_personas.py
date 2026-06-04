"""Public, **unauthenticated** persona-art routes (F2).

A persona's art — its ``emoji`` stickers and its ``reference`` 立绘 — is not
secret: it is the character's public face, the same way an avatar on a profile
page is public. These routes expose that art at ROOT with **NO auth** so the
public **agent status card** (and any other public surface) can render a
persona's avatar without an admin session.

Two URL shapes, both mounted at ROOT (auth only gates ``/v1`` + ``/admin``):

* ``GET /public/personas/{persona_id}/assets/{asset_id}`` — serve one asset
  blob. Mirrors the admin blob-serve handler's ``FileResponse`` + ETag +
  content-type behaviour (the read logic is duplicated here rather than shared,
  so the admin route's auth gate is never weakened). 404 for an unknown
  ``persona_id`` / ``asset_id`` pair, or when the metadata row outlives its
  on-disk blob.
* ``GET /public/personas/{persona_id}/avatar`` — 302-redirect to the persona's
  *first* ``emoji`` asset, falling back to its first ``reference`` 立绘. 404
  when the persona has no art. This is the URL the status card embeds: it needs
  only the ``persona_id`` (which rides inside the signed status token), not the
  opaque asset id.

The asset store is read **lazily** from ``request.app.state`` at request time
(same pattern the public status routes use for the journal) — it is wired onto
the admin_a state slot in the lifespan *after* routes are mounted, so capturing
it at construction would always yield ``None``. We probe both the direct
``persona_asset_store`` attribute and the ``corlinman_admin_a_state`` slot the
gateway actually populates (mirroring ``scheduler.builtins.qzone_daily``).
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response

from corlinman_server.persona import AssetKind, PersonaAssetStore

__all__ = ["router"]

#: Asset kinds, in avatar-preference order. ``emoji`` is the persona's
#: expressive face; the ``reference`` 立绘 is the fallback when no emoji exists.
#: Matches ``_avatar_url_for`` in the admin persona lib so the avatar a logged-in
#: operator sees and the one a public viewer sees resolve identically.
_AVATAR_KIND_ORDER: tuple[AssetKind, ...] = ("emoji", "reference")


def _probe_asset_store(app_state: Any | None) -> PersonaAssetStore | None:
    """Resolve the live :class:`PersonaAssetStore` off ``app.state``.

    The store is opened at boot and stashed on the admin_a state slot
    (``corlinman_admin_a_state``); some boot paths also expose it directly on
    ``app.state`` / in an ``extras`` dict. Probe all three so the route works
    regardless of which seam wired it. Returns ``None`` on a degraded boot that
    never opened the store — the route then 404s (the asset genuinely can't be
    served) rather than 500ing.
    """
    if app_state is None:
        return None
    # The stores are held as ``Any`` on every state slot (AdminState avoids
    # import-coupling to the persona package), so cast the probe result back to
    # the concrete type for callers — it's a real PersonaAssetStore at runtime.
    store = getattr(app_state, "persona_asset_store", None)
    if store is not None:
        return cast("PersonaAssetStore", store)
    admin_a = getattr(app_state, "corlinman_admin_a_state", None) or getattr(
        app_state, "admin_a_state", None
    )
    if admin_a is not None:
        store = getattr(admin_a, "persona_asset_store", None)
        if store is not None:
            return cast("PersonaAssetStore", store)
    extras = getattr(app_state, "extras", None)
    if isinstance(extras, dict):
        got = extras.get("persona_asset_store")
        return cast("PersonaAssetStore | None", got)
    return None


def _not_found(persona_id: str, asset_id: str | None) -> HTTPException:
    detail: dict[str, Any] = {"error": "asset_not_found", "persona_id": persona_id}
    if asset_id is not None:
        detail["id"] = asset_id
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def router() -> APIRouter:
    """Build the public persona-art router (mount at ROOT, NO auth)."""
    api = APIRouter(tags=["public-personas"])

    @api.get(
        "/public/personas/{persona_id}/assets/{asset_id}",
        summary="Publicly serve one persona asset blob (no auth; ETag cacheable)",
    )
    async def serve_public_asset(
        request: Request,
        persona_id: str,
        asset_id: str,
    ) -> Response:
        store = _probe_asset_store(request.app.state)
        if store is None:
            # No store wired (degraded boot) — the art can't be served. 404 is
            # the right shape: a healthy boot would heal it, and a public
            # caller shouldn't be able to distinguish "missing" from "disabled".
            raise _not_found(persona_id, asset_id)
        record = await store.get_by_id(asset_id)
        # Guard the (persona, asset) pairing so a caller can't read persona B's
        # asset id under persona A's path — same path-confusion guard the admin
        # serve route applies.
        if record is None or record.persona_id != persona_id:
            raise _not_found(persona_id, asset_id)
        path = store.path_for(record)
        if not path.is_file():
            # Metadata row outlived its blob (manual ``rm`` on the data dir).
            # 404 because a re-upload would heal it.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "asset_blob_missing", "id": asset_id},
            )
        # Duplicated verbatim from the admin serve handler so the public and
        # admin surfaces stay byte-for-byte identical (same media type, ETag,
        # cache policy) — persona art is immutable per sha256, so a long
        # immutable cache is safe.
        return FileResponse(
            path,
            media_type=record.mime,
            filename=record.file_name,
            headers={
                "ETag": f'"{record.sha256}"',
                "Cache-Control": "public, max-age=86400, immutable",
            },
        )

    @api.get(
        "/public/personas/{persona_id}/avatar",
        summary="Publicly redirect to a persona's avatar asset (emoji else 立绘)",
    )
    async def serve_public_avatar(
        request: Request,
        persona_id: str,
    ) -> Response:
        store = _probe_asset_store(request.app.state)
        if store is None:
            raise _not_found(persona_id, None)
        # Resolve the avatar the same way the admin ``_avatar_url_for`` helper
        # does: first ``emoji``, else first ``reference``. ``list()`` returns
        # assets sorted by ``label ASC`` so "first" is stable across calls.
        for kind in _AVATAR_KIND_ORDER:
            assets = await store.list(persona_id, kind=kind)
            if assets:
                target = (
                    f"/public/personas/{persona_id}/assets/{assets[0].id}"
                )
                # 302 (not 301): a persona's avatar can change when its art is
                # re-uploaded, so the redirect target must not be cached as
                # permanent. The blob it points at is itself immutably cached.
                return RedirectResponse(
                    url=target,
                    status_code=status.HTTP_302_FOUND,
                )
        raise _not_found(persona_id, None)

    return api
