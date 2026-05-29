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

import re
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_providers.specs import list_supported_kinds
from corlinman_server.gateway.routes_admin_b.state import (
    config_snapshot,
    get_admin_state,
    require_admin,
)


# ---------------------------------------------------------------------------
# First-run wizard helpers — pull in the username/password service logic from
# the routes_admin_a.auth module so we don't duplicate hashing, validation,
# and atomic-write semantics. These imports are deliberately lazy-friendly:
# the module is part of the same gateway package that boots admin_a and
# admin_b together, so the import always succeeds at runtime.
# ---------------------------------------------------------------------------


# Mirror the username constraints from ``routes_admin_a.auth`` so we can
# perform the same shape-level rejection without taking an indirect cookie
# dependency on the auth module's private name bindings. Kept as local
# module constants because the auth module re-exports them via the request
# dataclasses but not as standalone symbols.
_USERNAME_MAX_LEN = 64
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


# ---------------------------------------------------------------------------
# First-run wizard wire shapes (B1–B4)
# ---------------------------------------------------------------------------


class FinalizeAccountBody(BaseModel):
    """B1: ``POST /admin/onboard/finalize-account`` request body.

    First-run flow trusts the authed session for the *old* password —
    operator authenticated with the default ``admin``/``root`` creds and
    we don't want to make them re-type their default password just to
    pick a username. The session-cookie check is the gatekeeper.
    """

    new_username: str = Field(min_length=1, max_length=_USERNAME_MAX_LEN)


class FinalizeAccountResponse(BaseModel):
    status: str = "ok"
    username: str


class FinalizePasswordBody(BaseModel):
    """B2: ``POST /admin/onboard/finalize-password`` request body."""

    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class FinalizePasswordResponse(BaseModel):
    status: str = "ok"
    must_change_password: bool = False


class FinalizePersonaBody(BaseModel):
    """B3: ``POST /admin/onboard/finalize-persona`` request body."""

    choice: Literal["skip", "default", "custom"]


class FinalizePersonaResponse(BaseModel):
    status: str = "ok"
    choice: str
    persona_id: str | None = None
    redirect: str | None = None


class ImageProviderSpec(BaseModel):
    """Slim wire shape for the ``separate`` branch of B4.

    Mirrors the canonical ``ProviderUpsert`` payload used by
    ``/admin/providers`` but keeps the field set narrow to the bits the
    image-provider configuration form actually surfaces. The handler
    upserts a ``[providers.<name>]`` block with ``image_capable=true``.
    """

    name: str = Field(min_length=1, max_length=64)
    kind: str
    base_url: str | None = None
    api_key: str | None = None
    image_model: str | None = None


class FinalizeImageProviderBody(BaseModel):
    """B4: ``POST /admin/onboard/finalize-image-provider`` request body.

    Schema is a discriminated union flavoured by ``choice``. The contract
    deliberately keeps every leaf optional so a single Pydantic class can
    parse all three branches; handler-side validation enforces the
    per-branch required fields.
    """

    choice: Literal["skip", "reuse", "separate"]
    provider_name: str | None = None
    spec: ImageProviderSpec | None = None


class FinalizeImageProviderResponse(BaseModel):
    status: str = "ok"
    choice: str
    image_provider: str | None = None
    evidence: str | None = None


def _bad(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


def _resolve_auth_state() -> Any:
    """Return the admin_a :class:`AdminState` (where the credentials +
    session store live).

    The first-run wizard endpoints run under the ``routes_admin_b``
    router but the canonical username / password / session state is
    populated on the admin_a side by the gateway lifecycle. Falling back
    to the admin_b state lets test harnesses that only build one of the
    two states still drive these endpoints.
    """
    try:
        from corlinman_server.gateway.routes_admin_a.state import (
            get_admin_state as _get_admin_a_state,
        )
    except Exception:  # pragma: no cover — admin_a missing
        return get_admin_state()
    try:
        state_a = _get_admin_a_state()
    except RuntimeError:
        # admin_a state not installed; admin_b's get_admin_state defaults
        # to an empty AdminState which the handler will recognise as
        # "no credentials" and 503 with a clean envelope.
        return get_admin_state()
    if state_a.admin_username is not None or state_a.admin_password_hash is not None:
        return state_a
    # admin_a is empty (e.g. degraded boot) — try admin_b which carries
    # the same field names.
    return get_admin_state()


def _read_session_cookie_from_request(request: Request) -> str | None:
    """Local copy of ``routes_admin_a.auth._read_session_cookie``.

    Re-implemented here so we don't reach into a sibling module's
    private name; the cookie name comes from the shared ``_session_store``
    constant which is the only stable hook.
    """
    from corlinman_server.gateway.routes_admin_a._session_store import (
        SESSION_COOKIE_NAME,
        extract_cookie,
    )

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return token
    raw = request.headers.get("cookie")
    if raw is None:
        return None
    return extract_cookie(raw, SESSION_COOKIE_NAME)


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

        return FinalizeImageProviderResponse(
            choice="separate",
            image_provider=spec.name,
        )

    return r
