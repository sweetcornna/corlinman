"""Instance-bound NapCat login attempts, history, and diagnostics."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from corlinman_server.gateway.qq_instances import QqAdminError, QqInstanceAdminService
from corlinman_server.gateway.routes_admin_b._napcat_lib import (
    AccountsOut,
    NapcatError,
    QqAccount,
    QrcodeOut,
    QuickLoginBody,
    StatusOut,
    _accounts_path_for_instance,
    _ensure_onebot_websocket_server,
    _load_accounts,
    _manager_http_error,
    _napcat_http_error,
    _NapcatClient,
    _onebot_websocket_server_from_instance_config,
    _resolve_napcat_endpoint_for_instance,
    _upsert_account,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

_ATTEMPT_TTL_MS = 120_000


class LoginAttemptOut(BaseModel):
    attempt_id: str
    instance_id: str
    status: str
    image_base64: str | None = None
    qrcode_url: str | None = None
    expires_at: int
    revision: int = 1
    account: QqAccount | None = None
    message: str | None = None


@dataclass(slots=True)
class _LoginAttempt:
    digest: str
    instance_id: str
    owner_digest: str
    generation: int | None
    image_base64: str | None
    qrcode_url: str | None
    expires_at: int
    revision: int = 1
    status: str = "waiting"
    account: QqAccount | None = None
    message: str | None = None
    qr_digest: str | None = None


_TERMINAL_ATTEMPT_STATUSES = frozenset({"confirmed", "error", "superseded"})


class QqLoginAttemptStore:
    """Opaque attempt IDs mapped to hashed, exact-instance server state."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], _LoginAttempt] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, instance_id: str) -> asyncio.Lock:
        return self._locks.setdefault(instance_id, asyncio.Lock())

    def create(
        self,
        instance_id: str,
        qrcode: QrcodeOut,
        *,
        owner: str,
        generation: int | None,
    ) -> LoginAttemptOut:
        raw = secrets.token_urlsafe(32)
        digest = _digest(raw)
        row = _LoginAttempt(
            digest=digest,
            instance_id=instance_id,
            owner_digest=_digest(owner),
            generation=generation,
            image_base64=qrcode.image_base64,
            qrcode_url=qrcode.qrcode_url,
            expires_at=min(qrcode.expires_at, _now_ms() + _ATTEMPT_TTL_MS),
            qr_digest=_qrcode_digest(qrcode),
        )
        self.supersede_instance(instance_id)
        self._rows[(instance_id, digest)] = row
        return self._out(raw, row)

    def get(
        self,
        instance_id: str,
        attempt_id: str,
        *,
        owner: str,
    ) -> _LoginAttempt | None:
        digest = _digest(attempt_id)
        row = self._rows.get((instance_id, digest))
        if (
            row is None
            or not hmac.compare_digest(row.digest, digest)
            or not hmac.compare_digest(row.owner_digest, _digest(owner))
        ):
            return None
        return row

    def update_status(
        self,
        attempt_id: str,
        row: _LoginAttempt,
        status_out: StatusOut,
    ) -> LoginAttemptOut:
        row.status = status_out.status
        row.account = status_out.account
        row.message = status_out.message
        return self._out(attempt_id, row)

    def mark_error(
        self,
        attempt_id: str,
        row: _LoginAttempt,
        message: str,
    ) -> LoginAttemptOut:
        row.status = "error"
        row.message = message
        return self._out(attempt_id, row)

    def supersede_instance(
        self,
        instance_id: str,
        *,
        message: str = "a newer login attempt replaced this QR code",
    ) -> None:
        for (candidate_id, _digest_value), row in self._rows.items():
            if candidate_id != instance_id or row.status in _TERMINAL_ATTEMPT_STATUSES:
                continue
            row.status = "superseded"
            row.message = message

    def refresh(
        self,
        attempt_id: str,
        row: _LoginAttempt,
        qrcode: QrcodeOut,
    ) -> LoginAttemptOut:
        row.image_base64 = qrcode.image_base64
        row.qrcode_url = qrcode.qrcode_url
        row.expires_at = min(qrcode.expires_at, _now_ms() + _ATTEMPT_TTL_MS)
        row.qr_digest = _qrcode_digest(qrcode)
        row.revision += 1
        row.status = "waiting"
        row.message = None
        return self._out(attempt_id, row)

    def delete(self, instance_id: str, attempt_id: str, *, owner: str) -> bool:
        row = self.get(instance_id, attempt_id, owner=owner)
        if row is None:
            return False
        self._rows.pop((instance_id, row.digest), None)
        return True

    def has_instance(self, instance_id: str) -> bool:
        return any(
            candidate_id == instance_id
            and row.status not in _TERMINAL_ATTEMPT_STATUSES
            for (candidate_id, _digest_value), row in self._rows.items()
        )

    @staticmethod
    def _out(attempt_id: str, row: _LoginAttempt) -> LoginAttemptOut:
        return LoginAttemptOut(
            attempt_id=attempt_id,
            instance_id=row.instance_id,
            status=row.status,
            image_base64=row.image_base64,
            qrcode_url=row.qrcode_url,
            expires_at=row.expires_at,
            revision=row.revision,
            account=row.account,
            message=row.message,
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _qrcode_digest(qrcode: QrcodeOut) -> str | None:
    payload = qrcode.image_base64 or qrcode.qrcode_url
    return _digest(payload) if payload else None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _attempt_owner(request: Request) -> str:
    token = request.cookies.get("corlinman_session")
    if token:
        return f"session:{token}"
    authorization = request.headers.get("authorization", "")
    if authorization:
        return f"authorization:{authorization}"
    # require_admin has already authenticated the request. This fallback keeps
    # reverse-proxy/test integrations bound to their direct client identity.
    client = request.client
    return f"peer:{client.host if client is not None else 'unknown'}"


def _attempt_store(state: AdminState) -> QqLoginAttemptStore:
    store = state.qq_login_attempts
    if isinstance(store, QqLoginAttemptStore):
        return store
    store = QqLoginAttemptStore()
    state.qq_login_attempts = store
    return store


def _qq_service(state: AdminState) -> QqInstanceAdminService:
    service = state.qq_instance_admin
    if isinstance(service, QqInstanceAdminService):
        return service
    app_state = state.extras.get("app_state")
    try:
        from corlinman_server.gateway.routes_admin_a import get_admin_state

        admin_a = get_admin_state()
    except Exception:
        admin_a = None
    if admin_a is None and app_state is None:
        raise QqAdminError(
            503,
            "qq_admin_unavailable",
            "QQ instance control plane is unavailable",
        )
    service = QqInstanceAdminService(admin_a or app_state)
    state.qq_instance_admin = service
    return service


def _admin_a_state(state: AdminState) -> Any:
    service = _qq_service(state)
    return service.state


def _raise(error: QqAdminError) -> NoReturn:
    raise HTTPException(
        status_code=error.status_code,
        detail=error.public_detail(),
    ) from error


async def _client_for(
    state: AdminState,
    instance_id: str,
) -> tuple[Any, Any, Any]:
    admin_state = _admin_a_state(state)
    service = _qq_service(state)
    snapshot = service.get_instance(instance_id)
    endpoint = await _resolve_napcat_endpoint_for_instance(admin_state, instance_id)
    if endpoint.url is None:  # pragma: no cover - resolver fails closed
        raise QqAdminError(503, "napcat_not_configured", "NapCat URL is unavailable")
    return (
        _NapcatClient(endpoint.url, endpoint.access_token),
        snapshot,
        endpoint,
    )


async def _complete_login(
    state: AdminState,
    instance_id: str,
    client: Any,
    snapshot: Any,
    endpoint: Any,
    account: QqAccount | None,
) -> None:
    if (
        account is not None
        and snapshot.expected_uin is not None
        and int(account.uin) != snapshot.expected_uin
    ):
        raise QqAdminError(
            409,
            "identity_mismatch",
            "the logged-in QQ account does not match this instance",
            expected_uin=snapshot.expected_uin,
        )
    desired = _onebot_websocket_server_from_instance_config(
        instance_id,
        _qq_service(state).resolved_instance_config(instance_id),
        endpoint=endpoint,
    )
    await _ensure_onebot_websocket_server(client, desired)
    if account is not None:
        manager = getattr(_admin_a_state(state), "napcat_manager", None)
        if manager is not None and snapshot.connection_mode == "managed":
            response = await manager.request(
                "bind_uin",
                instance_id,
                generation=endpoint.generation,
                expected_uin=int(account.uin),
            )
            if not response.ok:
                error_status, error_code, error_message = _manager_http_error(
                    response,
                    fallback_code="identity_mismatch",
                    fallback_message="failed to bind the logged-in QQ identity",
                    fallback_status=409,
                )
                raise QqAdminError(error_status, error_code, error_message)
        path = _accounts_path_for_instance(
            _admin_a_state(state),
            instance_id,
            default_instance=snapshot.is_default,
        )
        await _upsert_account(path, account)
    registry = getattr(_admin_a_state(state), "qq_runtime_registry", None)
    if registry is not None:
        await registry.restart(instance_id)


async def create_login_attempt(
    state: AdminState,
    instance_id: str,
    request: Request,
) -> LoginAttemptOut:
    """Create an exact-instance attempt for canonical and compatibility routes."""
    store = _attempt_store(state)
    async with store.lock(instance_id):
        client, _snapshot, endpoint = await _client_for(state, instance_id)
        async with client:
            qrcode = await client.request_qrcode()
        return store.create(
            instance_id,
            qrcode,
            owner=_attempt_owner(request),
            generation=endpoint.generation,
        )


async def poll_login_attempt(
    state: AdminState,
    instance_id: str,
    attempt_id: str,
    request: Request,
) -> LoginAttemptOut:
    """Poll one exact attempt without allowing cross-instance fallback."""
    store = _attempt_store(state)
    async with store.lock(instance_id):
        row = store.get(instance_id, attempt_id, owner=_attempt_owner(request))
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "login_attempt_not_found"},
            )
        if row.status in _TERMINAL_ATTEMPT_STATUSES:
            return store._out(attempt_id, row)
        client, snapshot, endpoint = await _client_for(state, instance_id)
        if row.generation != endpoint.generation:
            store.mark_error(
                attempt_id,
                row,
                "the NapCat runtime changed; start a new login attempt",
            )
            raise QqAdminError(
                409,
                "login_attempt_stale",
                "the NapCat runtime changed; start a new login attempt",
            )
        async with client:
            if _now_ms() >= row.expires_at:
                qrcode = await client.request_qrcode()
                return store.refresh(attempt_id, row, qrcode)
            result = await client.check_status()
            if result.status == "confirmed":
                try:
                    await _complete_login(
                        state,
                        instance_id,
                        client,
                        snapshot,
                        endpoint,
                        result.account,
                    )
                except Exception:
                    store.mark_error(
                        attempt_id,
                        row,
                        "QQ login completion failed; start a new login attempt",
                    )
                    raise
        return store.update_status(attempt_id, row, result)


async def accounts_for_instance(state: AdminState, instance_id: str) -> AccountsOut:
    """Read only the selected instance's account history."""
    snapshot = _qq_service(state).get_instance(instance_id)
    path = _accounts_path_for_instance(
        _admin_a_state(state),
        instance_id,
        default_instance=snapshot.is_default,
    )
    return AccountsOut(accounts=await _load_accounts(path))


async def quick_login_instance(
    state: AdminState,
    instance_id: str,
    body: QuickLoginBody,
) -> StatusOut:
    """Run quick-login against one exact configured instance."""
    if not body.uin.strip():
        raise HTTPException(status_code=422, detail={"error": "invalid_uin"})
    store = _attempt_store(state)
    async with store.lock(instance_id):
        client, snapshot, endpoint = await _client_for(state, instance_id)
        async with client:
            result = await client.quick_login(body.uin)
            if result.status == "confirmed":
                await _complete_login(
                    state,
                    instance_id,
                    client,
                    snapshot,
                    endpoint,
                    result.account,
                )
                store.supersede_instance(
                    instance_id,
                    message="quick login completed for this QQ instance",
                )
        return cast("StatusOut", result)


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "napcat"])

    @r.post(
        "/admin/channels/qq/instances/{instance_id}/login-attempts",
        response_model=LoginAttemptOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_attempt(instance_id: str, request: Request) -> LoginAttemptOut:
        state = get_admin_state()
        try:
            return await create_login_attempt(state, instance_id, request)
        except QqAdminError as exc:
            _raise(exc)
        except NapcatError as exc:
            error_status, detail = _napcat_http_error(exc)
            raise HTTPException(status_code=error_status, detail=detail) from exc

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/login-attempts/{attempt_id}",
        response_model=LoginAttemptOut,
    )
    async def poll_attempt(
        instance_id: str,
        attempt_id: str,
        request: Request,
    ) -> LoginAttemptOut:
        state = get_admin_state()
        try:
            return await poll_login_attempt(
                state,
                instance_id,
                attempt_id,
                request,
            )
        except QqAdminError as exc:
            _raise(exc)
        except NapcatError as exc:
            error_status, detail = _napcat_http_error(exc)
            raise HTTPException(status_code=error_status, detail=detail) from exc

    @r.delete(
        "/admin/channels/qq/instances/{instance_id}/login-attempts/{attempt_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def revoke_attempt(
        instance_id: str,
        attempt_id: str,
        request: Request,
    ) -> Response:
        store = _attempt_store(get_admin_state())
        async with store.lock(instance_id):
            if not store.delete(
                instance_id,
                attempt_id,
                owner=_attempt_owner(request),
            ):
                raise HTTPException(
                    status_code=404,
                    detail={"error": "login_attempt_not_found"},
                )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/accounts",
        response_model=AccountsOut,
    )
    async def instance_accounts(instance_id: str) -> AccountsOut:
        state = get_admin_state()
        try:
            return await accounts_for_instance(state, instance_id)
        except QqAdminError as exc:
            _raise(exc)

    @r.get(
        "/admin/channels/qq/instances/{instance_id}/login-history",
        response_model=AccountsOut,
        include_in_schema=False,
    )
    async def login_history(instance_id: str) -> AccountsOut:
        return await instance_accounts(instance_id)

    @r.post(
        "/admin/channels/qq/instances/{instance_id}/quick-login",
        response_model=StatusOut,
    )
    async def quick_login(instance_id: str, body: QuickLoginBody) -> StatusOut:
        state = get_admin_state()
        try:
            return await quick_login_instance(state, instance_id, body)
        except QqAdminError as exc:
            _raise(exc)
        except NapcatError as exc:
            error_status, detail = _napcat_http_error(exc)
            raise HTTPException(status_code=error_status, detail=detail) from exc

    return r


__all__ = [
    "LoginAttemptOut",
    "QqLoginAttemptStore",
    "accounts_for_instance",
    "create_login_attempt",
    "poll_login_attempt",
    "quick_login_instance",
    "router",
]
