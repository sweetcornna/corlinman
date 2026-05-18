"""``/admin/onboard*`` — stateless onboard-wizard endpoints.

Two routes:

* ``POST /admin/onboard/finalize``      — confirm; atomic write of a
  generic ``[providers.<name>]`` block + ``[models]`` default alias +
  optional ``[embedding]`` section, hot-swap of the in-memory snapshot.
* ``POST /admin/onboard/finalize-skip`` — wire up the built-in mock
  provider (zero-credential path).

The wizard is intentionally stateless server-side; the UI carries the
full ``(kind, base_url, api_key, model, ...)`` payload on every call.
Per-provider probe/channel-pick endpoints live elsewhere; provider
management goes through ``/admin/credentials`` + ``/admin/providers``
(see ``docs/PLAN_PROVIDER_AUTH.md`` §1.2).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_providers.specs import list_supported_kinds
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class FinalizeBody(BaseModel):
    """Generic provider-finalize payload.

    ``provider_name`` is the slot key used in ``[providers.<name>]``.
    ``kind`` must be one of :func:`list_supported_kinds` (e.g.
    ``"openai_compatible"``, ``"openai"``, ``"anthropic"``).
    ``model`` is set as the default model alias; ``embedding_model``
    (when present) seeds the ``[embedding]`` block pointed at the same
    provider.
    """

    provider_name: str
    kind: str
    base_url: str | None = None
    api_key: str | None = None
    model: str
    embedding_model: str | None = None


class FinalizeResponse(BaseModel):
    ok: bool = True
    redirect: str = "/login"


class FinalizeSkipResponse(BaseModel):
    """Response payload for ``POST /admin/onboard/finalize-skip``."""

    status: str = "ok"
    mode: str = "mock"


def _bad(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _write_config_atomic(path: Any, cfg: dict[str, Any]) -> JSONResponse | None:
    """Serialise ``cfg`` to TOML and atomically replace ``path``.

    Pick the ``tomli_w`` writer with a ``toml`` fallback, dump to a
    sibling ``.new`` file, then rename onto the target. Returns ``None``
    on success, or a :class:`JSONResponse` describing the failure for
    callers to short-circuit with.
    """
    try:
        try:
            import tomli_w  # noqa: PLC0415
        except ImportError:  # pragma: no cover — fallback path
            import toml as tomli_w  # type: ignore  # noqa: PLC0415
        serialised = tomli_w.dumps(cfg)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": "serialise_failed", "message": str(exc)},
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "write_failed", "message": str(exc)},
        )
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "onboard"])

    @r.post("/admin/onboard/finalize", response_model=FinalizeResponse)
    async def post_finalize(body: FinalizeBody):
        state = get_admin_state()

        # Validate kind against the registry of supported provider shapes
        # so the UI cannot write an unknown ``kind`` into the on-disk
        # config (would silently disable the provider at boot).
        if body.kind not in list_supported_kinds():
            return _bad("invalid_kind")

        if state.config_path is None:
            return _bad("config_path_unset", status=503)

        # Build a generic [providers.<name>] entry. The api_key shape
        # ({"value": "..."}) mirrors what /admin/credentials writes so
        # the redactor + display surfaces all stay consistent.
        new_entry: dict[str, Any] = {
            "kind": body.kind,
            "enabled": True,
            "params": {},
        }
        if body.base_url is not None:
            new_entry["base_url"] = body.base_url
        if body.api_key is not None:
            new_entry["api_key"] = {"value": body.api_key}

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            providers[body.provider_name] = new_entry
            cfg["providers"] = providers

            # [models] default + alias.
            models_cfg = dict(cfg.get("models") or {})
            models_cfg["default"] = body.model
            aliases = dict(models_cfg.get("aliases") or {})
            aliases[body.model] = {
                "model": body.model,
                "provider": body.provider_name,
                "params": {},
            }
            models_cfg["aliases"] = aliases
            cfg["models"] = models_cfg

            # [embedding] — optional, only when the operator picked one.
            if body.embedding_model is not None:
                cfg["embedding"] = {
                    "provider": body.provider_name,
                    "model": body.embedding_model,
                    "dimension": 1536,
                    "enabled": True,
                    "params": {},
                }

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        return FinalizeResponse()

    @r.post(
        "/admin/onboard/finalize-skip",
        response_model=FinalizeSkipResponse,
        summary="Finish onboarding with mock provider",
    )
    async def post_finalize_skip(
        body: dict[str, Any] | None = Body(default=None),
    ):
        """Skip-path finalizer — wire up the built-in mock provider.

        Wave 2.2 of the easy-setup plan: when a new user can't / doesn't
        want to configure a real LLM yet, this endpoint provisions a
        ``[providers.mock]`` entry and points the default model alias at
        it. The mock provider echoes user input (reversed, prefixed with
        a sentinel banner) so the agent loop, chat UI, and embedding
        pipeline all work end-to-end without upstream credentials.

        Body is intentionally optional; callers MAY send ``{}``. The
        write is idempotent — calling twice merges the same block back
        in without duplicating it, and leaves the config valid TOML.
        """
        del body  # Reserved for future flags (e.g. preferred model id).
        state = get_admin_state()
        if state.config_path is None:
            return JSONResponse(
                status_code=503,
                content={"error": "config_path_unset"},
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())

            providers = dict(cfg.get("providers") or {})
            existing = providers.get("mock")
            mock_entry: dict[str, Any] = (
                dict(existing) if isinstance(existing, dict) else {}
            )
            mock_entry["kind"] = "mock"
            mock_entry["enabled"] = True
            providers["mock"] = mock_entry
            cfg["providers"] = providers

            # Point the default model alias at the mock provider so that
            # ``/v1/chat/completions`` resolves without a Configured
            # ``[models]`` block from the operator.
            models_cfg = dict(cfg.get("models") or {})
            models_cfg["default"] = "mock"
            aliases = dict(models_cfg.get("aliases") or {})
            aliases["mock"] = {
                "model": "mock",
                "provider": "mock",
                "params": {},
            }
            models_cfg["aliases"] = aliases
            cfg["models"] = models_cfg

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err

        return FinalizeSkipResponse()

    return r
