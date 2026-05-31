"""``/admin/config*`` — live config view + edit.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/config.rs``.

Routes:

* ``GET    /admin/config``         — current snapshot (redacted), version, meta.
* ``POST   /admin/config``         — submit a TOML edit. ``dry_run`` validates
  only; otherwise writes to disk + hot-swaps.
* ``GET    /admin/config/schema``  — JSON-Schema document for the config.
* ``POST   /admin/config/reload``  — manually trigger a hot-reload from disk.

State requirements:

* ``state.config_loader``  — must return the current dict snapshot.
* ``state.config_path``    — required for non-dry-run POST + reload.
* ``state.extras["config_swap_fn"]`` (optional) — async callable
  ``(new_cfg: dict) -> None`` that publishes the new snapshot to live
  consumers (e.g. swaps an ArcSwap-equivalent).
* ``state.extras["config_watcher"]`` (optional) — exposes
  ``trigger_reload() -> dict`` for ``POST /admin/config/reload``.
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)

REDACTED_SENTINEL = "***REDACTED***"


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class GetConfigResponse(BaseModel):
    toml: str
    version: str
    meta: dict[str, Any] = {}


class PostConfigBody(BaseModel):
    toml: str
    dry_run: bool = False


class ValidationIssue(BaseModel):
    path: str
    code: str
    message: str
    level: str = "error"


class PostConfigResponse(BaseModel):
    status: str  # "ok" | "invalid"
    issues: list[ValidationIssue] = []
    requires_restart: list[str] = []
    version: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _toml_dumps(cfg: dict[str, Any]) -> str:
    try:
        import tomli_w  # noqa: PLC0415
        return tomli_w.dumps(cfg)
    except ImportError:  # pragma: no cover
        import toml  # type: ignore  # noqa: PLC0415
        # ``toml`` ships no type stubs; its return is dynamically typed.
        return cast("str", toml.dumps(cfg))


def _toml_loads(text: str) -> dict[str, Any]:
    try:
        import tomllib  # noqa: PLC0415
        return tomllib.loads(text)
    except ImportError:  # pragma: no cover — py<3.11
        import toml  # type: ignore  # noqa: PLC0415
        # ``toml`` ships no type stubs; its return is dynamically typed.
        return cast("dict[str, Any]", toml.loads(text))


def _hash8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _redact(cfg: Any) -> Any:
    """Walk the config dict and replace literal secret values with the
    redaction sentinel. Mirrors the Rust ``Config::redacted`` shape:
    only ``api_key.value`` / ``admin.password_hash`` style fields are
    redacted; ``api_key.env`` references stay readable."""
    if isinstance(cfg, dict):
        out: dict[str, Any] = {}
        for k, v in cfg.items():
            if k == "api_key" and isinstance(v, dict) and "value" in v:
                out[k] = {**v, "value": REDACTED_SENTINEL}
            elif k in {"password_hash", "secret_key"} and v:
                out[k] = REDACTED_SENTINEL
            elif isinstance(v, dict):
                out[k] = _redact(v)
            elif isinstance(v, list):
                out[k] = [_redact(item) for item in v]
            else:
                out[k] = v
        return out
    return cfg


def _has_redacted(cfg: Any) -> bool:
    if isinstance(cfg, dict):
        return any(_has_redacted(v) for v in cfg.values())
    if isinstance(cfg, list):
        return any(_has_redacted(v) for v in cfg)
    if isinstance(cfg, str):
        return cfg == REDACTED_SENTINEL
    return False


def _merge_secrets_from(new: Any, base: Any) -> Any:
    """Replace any ``REDACTED_SENTINEL`` values in ``new`` with the real
    value from ``base`` at the same path. Mirrors Rust
    ``Config::merge_redacted_secrets_from`` semantics."""
    if isinstance(new, dict) and isinstance(base, dict):
        out = {}
        for k, v in new.items():
            if k in base:
                out[k] = _merge_secrets_from(v, base[k])
            else:
                out[k] = v
        return out
    if isinstance(new, str) and new == REDACTED_SENTINEL and isinstance(base, str):
        return base
    if isinstance(new, str) and new == REDACTED_SENTINEL:
        return base
    return new


def _detect_restart_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def cmp(path: str, oa: Any, ob: Any) -> None:
        if oa != ob:
            out.append(path)

    old_server = old.get("server") or {}
    new_server = new.get("server") or {}
    cmp("server.port", old_server.get("port"), new_server.get("port"))
    cmp("server.bind", old_server.get("bind"), new_server.get("bind"))
    cmp("server.data_dir", old_server.get("data_dir"), new_server.get("data_dir"))

    old_ch = (old.get("channels") or {}).get("qq") or {}
    new_ch = (new.get("channels") or {}).get("qq") or {}
    cmp("channels.qq.enabled", old_ch.get("enabled", False), new_ch.get("enabled", False))
    old_tg = (old.get("channels") or {}).get("telegram") or {}
    new_tg = (new.get("channels") or {}).get("telegram") or {}
    cmp("channels.telegram.enabled", old_tg.get("enabled", False), new_tg.get("enabled", False))

    old_log = old.get("logging") or {}
    new_log = new.get("logging") or {}
    cmp("logging.level", old_log.get("level"), new_log.get("level"))
    cmp("logging.format", old_log.get("format"), new_log.get("format"))

    return out


async def _publish_snapshot(state: AdminState, cfg: dict[str, Any]) -> None:
    swap_fn = state.extras.get("config_swap_fn")
    if swap_fn is None:
        return
    res = swap_fn(cfg)
    if hasattr(res, "__await__"):
        await res


async def _rewrite_py_config(state: AdminState, cfg: dict[str, Any]) -> None:
    """Mirror the Rust ``state.rewrite_py_config`` hook — best-effort
    re-render of the Python-side JSON drop after a successful swap."""
    if state.py_config_path is None:
        return
    try:
        from corlinman_server.gateway.lifecycle import (
            write_py_config,  # type: ignore  # noqa: PLC0415
        )
    except ImportError:
        return
    res = write_py_config(cfg, state.py_config_path)
    if hasattr(res, "__await__"):
        await res


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "config"])

    @r.get("/admin/config", response_model=GetConfigResponse)
    async def get_config():
        snap = dict(config_snapshot())
        version = _hash8(_toml_dumps(snap))
        try:
            redacted_toml = _toml_dumps(_redact(snap))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "serialise_failed", "message": str(exc)},
            )
        return GetConfigResponse(
            toml=redacted_toml,
            version=version,
            meta=dict(snap.get("meta") or {}),
        )

    @r.post("/admin/config", response_model=PostConfigResponse)
    async def post_config(body: PostConfigBody):
        state = get_admin_state()
        try:
            new_cfg = _toml_loads(body.toml)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content=PostConfigResponse(
                    status="invalid",
                    issues=[
                        ValidationIssue(
                            path="toml",
                            code="decode_failed",
                            message=str(exc),
                            level="error",
                        )
                    ],
                ).model_dump(),
            )

        current = dict(config_snapshot())
        merged = _merge_secrets_from(new_cfg, current)
        if _has_redacted(merged):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "redacted_sentinel_in_payload",
                    "message": (
                        "POST payload contains the literal `***REDACTED***`"
                        " placeholder for at least one secret. Replace it"
                        " with a real value (or omit the field)."
                    ),
                },
            )

        restart_fields = _detect_restart_fields(current, merged)

        if body.dry_run:
            return PostConfigResponse(
                status="ok", issues=[], requires_restart=restart_fields, version=None
            )

        if state.config_path is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "config_path_unset",
                    "message": "gateway booted without a config file path",
                },
            )

        async with state.admin_write_lock:
            try:
                serialised = _toml_dumps(merged)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    status_code=500,
                    content={"error": "serialise_failed", "message": str(exc)},
                )
            path = state.config_path
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
            await _publish_snapshot(state, merged)
            await _rewrite_py_config(state, merged)
        version = _hash8(serialised)
        return PostConfigResponse(
            status="ok", issues=[], requires_restart=restart_fields, version=version
        )

    @r.get("/admin/config/schema")
    async def get_schema():
        # gap-fill v1.15 (config-admin-reload-dead): emit a JSON-Schema
        # document for the config. There is still no central Pydantic
        # Config model in Python, so we derive a best-effort object schema
        # from the live snapshot's top-level sections plus the well-known
        # corlinman sections. The schema advertises each section as an
        # ``object`` with ``additionalProperties: true`` (the inner shapes
        # are still loosely-typed) so the admin UI's form generator can at
        # least surface the section list + restart-required hints. The
        # Rust side serialises a schemars-derived document; this is the
        # Python parity surface until a typed model lands.
        try:
            from corlinman_server.gateway.core.config_watcher import (
                RESTART_REQUIRED_SECTIONS,
            )

            restart_sections = sorted(RESTART_REQUIRED_SECTIONS)
        except Exception:  # noqa: BLE001 — schema must never 500
            restart_sections = []

        # Union of live-snapshot keys + the canonical corlinman sections so
        # the schema is stable even on a degraded (empty-snapshot) boot.
        known_sections = (
            "server",
            "admin",
            "providers",
            "models",
            "channels",
            "scheduler",
            "logging",
            "tenants",
            "hooks",
            "memory",
            "system",
            "identity",
            "persona",
        )
        snap = dict(config_snapshot())
        sections = sorted(set(known_sections) | set(snap.keys()))
        properties = {
            name: {"type": "object", "additionalProperties": True}
            for name in sections
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "CorlinmanConfig",
            "type": "object",
            "additionalProperties": True,
            "properties": properties,
            "x-restart-required-sections": restart_sections,
            "$comment": (
                "best-effort section schema; inner shapes are loosely "
                "typed until a central Pydantic Config model lands"
            ),
        }

    @r.post("/admin/config/reload")
    async def post_reload():
        state = get_admin_state()
        watcher = state.extras.get("config_watcher")
        if watcher is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "config_reload_disabled",
                    "message": "gateway booted without a ConfigWatcher",
                },
            )
        try:
            report = watcher.trigger_reload()
            if hasattr(report, "__await__"):
                report = await report
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "reload_failed", "message": str(exc)},
            )
        return report

    return r
