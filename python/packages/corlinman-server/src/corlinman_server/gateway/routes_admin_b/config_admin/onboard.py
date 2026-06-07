"""``/admin/onboard*`` — stateless onboard-wizard endpoints.

Original two routes:

* ``POST /admin/onboard/finalize``      — confirm; atomic write of a
  generic ``[providers.<name>]`` block + ``[models]`` default alias +
  optional ``[embedding]`` section, hot-swap of the in-memory snapshot.
* ``POST /admin/onboard/finalize-skip`` — wire up the built-in mock
  provider (zero-credential path).

First-run wizard additions (Wave 2 — see
``docs/PLAN_FIRST_RUN_WIZARD.md``):

* ``POST /admin/onboard/finalize-account``         — rename the admin
  user (B1). Reuses the username-change service logic from
  :mod:`corlinman_server.gateway.routes_admin_a.auth`.
* ``POST /admin/onboard/finalize-password``        — rotate password
  (B2). Wraps ``change_password`` from the same module.
* ``POST /admin/onboard/finalize-persona``         — capture the
  persona choice (B3). Three branches: ``skip`` / ``default`` (delegate
  to B5) / ``custom`` (return redirect).
* ``POST /admin/onboard/finalize-image-provider``  — capture the image
  provider choice (B4). Three branches: ``skip`` / ``reuse`` (probe
  current provider via Agent C's ``capabilities.probe_image_capability``)
  / ``separate`` (upsert a provider with ``image_capable=true``).

The wizard is intentionally stateless server-side; the UI carries the
full ``(kind, base_url, api_key, model, ...)`` payload on every call.
Per-provider probe/channel-pick endpoints live elsewhere; provider
management goes through ``/admin/credentials`` + ``/admin/providers``
(see ``docs/PLAN_PROVIDER_AUTH.md`` §1.2).
"""

from __future__ import annotations

from typing import Any

from corlinman_providers.specs import list_supported_kinds
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from corlinman_server.gateway.core.config_mutation import (
    publish_config_mutation as _publish_config_mutation_core,
)
from corlinman_server.gateway.core.config_mutation import (
    write_config_atomic as _write_config_atomic,
)
from corlinman_server.gateway.routes_admin_b.config_admin._onboard_lib import (
    _USERNAME_MAX_LEN,
    _USERNAME_RE,
    FinalizeAccountBody,
    FinalizeAccountResponse,
    FinalizeBody,
    FinalizeImageProviderBody,
    FinalizeImageProviderResponse,
    FinalizePasswordBody,
    FinalizePasswordResponse,
    FinalizePersonaBody,
    FinalizePersonaResponse,
    FinalizeResponse,
    FinalizeSkipResponse,
    _bad,
    _read_session_cookie_from_request,
    _resolve_auth_state,
)
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)


def _py_config_writer():
    from corlinman_server.gateway.lifecycle import write_py_config  # noqa: PLC0415

    return write_py_config


async def _publish_config_mutation(state: Any, cfg: dict[str, Any]) -> None:
    await _publish_config_mutation_core(
        state,
        cfg,
        py_config_writer=_py_config_writer(),
    )

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
            await _publish_config_mutation(state, cfg)

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
            await _publish_config_mutation(state, cfg)

        return FinalizeSkipResponse()

    # -----------------------------------------------------------------
    # B1 — POST /admin/onboard/finalize-account
    # -----------------------------------------------------------------

    @r.post(
        "/admin/onboard/finalize-account",
        response_model=FinalizeAccountResponse,
        summary="First-run: rename the admin user (session-trusted)",
    )
    async def post_finalize_account(
        body: FinalizeAccountBody,
        request: Request,
    ):
        # Lazy-import the auth helpers; keeping them at function scope
        # avoids cycle risk in the (unlikely) case that auth.py grows a
        # back-reference to this module.
        from corlinman_server.gateway.routes_admin_a.auth import (
            _persist_admin_credentials,
            _rename_active_session,
        )

        new_username = body.new_username.strip()
        if not new_username or len(new_username) > _USERNAME_MAX_LEN:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_username",
                    "message": (
                        f"username must be 1..{_USERNAME_MAX_LEN} characters"
                    ),
                },
            )
        if _USERNAME_RE.match(new_username) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_username",
                    "message": (
                        "username must contain only ASCII letters, "
                        "digits, underscores, and hyphens"
                    ),
                },
            )

        auth_state = _resolve_auth_state()
        if auth_state.session_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "session_store_missing"},
            )
        token = _read_session_cookie_from_request(request)
        session = (
            auth_state.session_store.validate(token) if token else None
        )
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthenticated"},
            )

        # Use the auth state's lock so we serialise against a concurrent
        # ``POST /admin/username`` from elsewhere in the gateway.
        import asyncio as _asyncio

        lock = auth_state.admin_write_lock or _asyncio.Lock()
        async with lock:
            if (
                auth_state.admin_username is None
                or auth_state.admin_password_hash is None
            ):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": "admin_not_configured"},
                )
            if session.user != auth_state.admin_username:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": "session_user_mismatch"},
                )

            # Idempotence-by-409: contract says the username-unchanged
            # branch returns 409 ``username_unchanged`` so the FE wizard
            # surfaces "pick a different name" rather than silently
            # advancing to the next step.
            if new_username == auth_state.admin_username:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "username_unchanged",
                        "username": new_username,
                    },
                )

            await _persist_admin_credentials(
                auth_state,
                new_username,
                None,
                precomputed_hash=auth_state.admin_password_hash,
                must_change_password=getattr(
                    auth_state, "must_change_password", False
                ),
            )
            _rename_active_session(
                auth_state, session.user, new_username
            )

        return FinalizeAccountResponse(username=new_username)

    # -----------------------------------------------------------------
    # B2 — POST /admin/onboard/finalize-password
    # -----------------------------------------------------------------

    @r.post(
        "/admin/onboard/finalize-password",
        response_model=FinalizePasswordResponse,
        summary="First-run: rotate the admin password",
    )
    async def post_finalize_password(
        body: FinalizePasswordBody,
        request: Request,
    ):
        from corlinman_server.gateway.routes_admin_a.auth import (
            MIN_PASSWORD_LEN,
            _persist_admin_credentials,
            argon2_verify,
        )

        auth_state = _resolve_auth_state()
        if auth_state.session_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "session_store_missing"},
            )
        token = _read_session_cookie_from_request(request)
        session = (
            auth_state.session_store.validate(token) if token else None
        )
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthenticated"},
            )

        import asyncio as _asyncio

        lock = auth_state.admin_write_lock or _asyncio.Lock()
        async with lock:
            if (
                auth_state.admin_username is None
                or auth_state.admin_password_hash is None
            ):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": "admin_not_configured"},
                )
            if session.user != auth_state.admin_username:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": "session_user_mismatch"},
                )
            if not argon2_verify(
                body.old_password, auth_state.admin_password_hash
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": "invalid_old_password"},
                )
            if len(body.new_password) < MIN_PASSWORD_LEN:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": "weak_password",
                        "message": (
                            f"password must be at least "
                            f"{MIN_PASSWORD_LEN} characters"
                        ),
                    },
                )
            await _persist_admin_credentials(
                auth_state,
                auth_state.admin_username,
                body.new_password,
                must_change_password=False,
            )
            # Clear the first-boot warning so /admin/me reflects the
            # rotation immediately. Field is on admin_a's state but the
            # write below is a no-op when the attribute is missing.
            if hasattr(auth_state, "must_change_password"):
                auth_state.must_change_password = False

        return FinalizePasswordResponse(must_change_password=False)

    # -----------------------------------------------------------------
    # B3 — POST /admin/onboard/finalize-persona
    # -----------------------------------------------------------------

    @r.post(
        "/admin/onboard/finalize-persona",
        response_model=FinalizePersonaResponse,
        summary="First-run: capture the persona choice",
    )
    async def post_finalize_persona(body: FinalizePersonaBody):
        choice = body.choice
        if choice == "skip":
            return FinalizePersonaResponse(choice="skip")
        if choice == "custom":
            # The persona wizard owns the actual creation flow; the FE
            # navigates there once it sees ``redirect`` come back.
            return FinalizePersonaResponse(
                choice="custom", redirect="/persona"
            )
        # choice == "default" — delegate to the B5 use-default flow so
        # the side effects (ensure grantley exists, mark active) stay in
        # one place. We call the personas module's worker directly rather
        # than via HTTP so the auth dependency doesn't re-run.
        from corlinman_server.gateway.routes_admin_b.personas import (
            ensure_default_persona_active,
        )

        persona_id = await ensure_default_persona_active()
        return FinalizePersonaResponse(
            choice="default", persona_id=persona_id
        )

    # -----------------------------------------------------------------
    # B4 — POST /admin/onboard/finalize-image-provider
    # -----------------------------------------------------------------

    @r.post(
        "/admin/onboard/finalize-image-provider",
        response_model=FinalizeImageProviderResponse,
        summary="First-run: capture the image-provider choice",
    )
    async def post_finalize_image_provider(
        body: FinalizeImageProviderBody,
    ):
        choice = body.choice

        if choice == "skip":
            return FinalizeImageProviderResponse(choice="skip")

        state = get_admin_state()

        if choice == "reuse":
            if not body.provider_name:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": "provider_name_required",
                        "message": "choice=reuse requires provider_name",
                    },
                )
            # Probe the slot via corlinman_providers.capabilities. The
            # except branches below stay permissive (supported=True) only
            # for the defensive case where the module/symbol is somehow
            # unavailable — a missing probe shouldn't brick the wizard.
            supported: bool = True
            evidence: str = "stub:capabilities_module_pending"
            try:
                from corlinman_providers import (  # type: ignore[attr-defined]
                    capabilities as _cap,
                )

                probe = getattr(_cap, "probe_image_capability", None)
                if probe is None:
                    # Defensive: the module is present but the symbol is
                    # gone (shouldn't happen — capabilities.py exports it).
                    # Stay permissive so the wizard can move forward.
                    supported = True
                    evidence = "stub:probe_symbol_missing"
                else:
                    # Pull the provider entry from the live config so we
                    # can hand the probe a fully-formed spec/dict.
                    cfg = dict(config_snapshot())
                    providers_cfg = cfg.get("providers") or {}
                    entry = providers_cfg.get(body.provider_name)
                    if entry is None:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail={
                                "error": "provider_not_found",
                                "name": body.provider_name,
                            },
                        )
                    # ``probe_image_capability`` is ``async def`` and
                    # returns the ``{supported, evidence, models}`` wire
                    # shape — mirror image_provider.py's awaited usage.
                    result = await probe(entry)
                    # Tolerate either a dict or a dataclass return shape.
                    if isinstance(result, dict):
                        supported = bool(result.get("supported", False))
                        evidence = str(
                            result.get("evidence")
                            or "probe_image_capability"
                        )
                    else:
                        supported = bool(
                            getattr(result, "supported", False)
                        )
                        evidence = str(
                            getattr(
                                result,
                                "evidence",
                                "probe_image_capability",
                            )
                        )
            except ImportError:
                # Defensive: corlinman_providers.capabilities should always
                # import in this workspace. If it somehow can't, stay
                # permissive so the wizard can move forward; the operator can
                # flip the toggle later via the providers admin surface.
                supported = True
                evidence = "stub:capabilities_module_missing"
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 — probe is best-effort
                supported = False
                evidence = f"probe_failed:{exc!s}"

            if not supported:
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={
                        "error": "image_not_supported",
                        "supported": False,
                        "hint": (
                            "current provider does not expose an "
                            "image-generation surface; pick the "
                            "'separate' branch or configure a new "
                            "image-capable provider"
                        ),
                        "evidence": evidence,
                    },
                )

            # Mark the chat provider as image_capable=true so the agent
            # image dispatcher prefers it.
            async with state.admin_write_lock:
                cfg = dict(config_snapshot())
                providers = dict(cfg.get("providers") or {})
                entry = dict(providers.get(body.provider_name) or {})
                entry["image_capable"] = True
                providers[body.provider_name] = entry
                cfg["providers"] = providers
                if state.config_path is not None:
                    err = _write_config_atomic(state.config_path, cfg)
                    if err is not None:
                        return err
                    await _publish_config_mutation(state, cfg)

            return FinalizeImageProviderResponse(
                choice="reuse",
                image_provider=body.provider_name,
                evidence=evidence,
            )

        # choice == "separate"
        spec = body.spec
        if spec is None or not spec.name or not spec.kind:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "spec_required",
                    "message": (
                        "choice=separate requires spec.{name,kind}"
                    ),
                },
            )
        if spec.kind not in list_supported_kinds():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "invalid_kind", "kind": spec.kind},
            )

        if state.config_path is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "config_path_unset"},
            )

        async with state.admin_write_lock:
            cfg = dict(config_snapshot())
            providers = dict(cfg.get("providers") or {})
            existing = dict(providers.get(spec.name) or {})
            existing["kind"] = spec.kind
            existing["enabled"] = existing.get("enabled", True)
            existing["image_capable"] = True
            if spec.base_url is not None:
                existing["base_url"] = spec.base_url
            if spec.api_key is not None:
                existing["api_key"] = {"value": spec.api_key}
            if spec.image_model is not None:
                existing["image_model"] = spec.image_model
            existing.setdefault("params", {})
            providers[spec.name] = existing
            cfg["providers"] = providers

            err = _write_config_atomic(state.config_path, cfg)
            if err is not None:
                return err
            await _publish_config_mutation(state, cfg)

        return FinalizeImageProviderResponse(
            choice="separate",
            image_provider=spec.name,
        )

    return r
