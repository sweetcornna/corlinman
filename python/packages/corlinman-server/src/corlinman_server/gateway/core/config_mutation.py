"""Atomic admin-config persistence — the single writer for the on-disk TOML.

Extracted from ``routes_admin_b.onboard`` so every admin route that mutates
``config.toml`` shares one definition of the serialise → temp-file → rename
sequence (atomicity guarantee, ``tomli_w`` writer with a ``toml`` fallback,
and the error-code shape callers short-circuit on). Modularization roadmap
Phase 1 — see ``docs/modularization-plan.md`` §3.2.

This is a leaf in the gateway ``core`` layer: it imports only the TOML writer
and Starlette's ``JSONResponse`` — never any ``routes_admin_*`` module — so the
``boundary-check`` (import-linter) contract stays satisfied and the config
modules depend on this neutral seam rather than on ``onboard``.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from fastapi.responses import JSONResponse

__all__ = ["publish_config_mutation", "write_config_atomic"]
logger = logging.getLogger(__name__)


def write_config_atomic(path: Any, cfg: dict[str, Any]) -> JSONResponse | None:
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


async def publish_config_mutation(
    state: Any,
    cfg: dict[str, Any],
    *,
    py_config_writer: Any | None = None,
) -> None:
    """Publish a saved config mutation to live readers and the Python sidecar.

    Admin routes that atomically rewrite ``config.toml`` must also update the
    in-process config snapshot and re-emit the ``py-config.json`` provider drop.
    The sidecar resolver watches that JSON file's mtime, so this is what makes
    provider/model edits visible without a gateway restart.
    """
    extras = getattr(state, "extras", None)
    get_extra = getattr(extras, "get", None)
    swap_fn = get_extra("config_swap_fn") if callable(get_extra) else None
    if swap_fn is not None:
        res = swap_fn(cfg)
        if inspect.isawaitable(res):
            await res

    py_config_path = getattr(state, "py_config_path", None)
    if py_config_path is None or py_config_writer is None:
        return
    try:
        res = py_config_writer(cfg, py_config_path)
        if inspect.isawaitable(res):
            await res
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "config_mutation.py_config_write_failed",
            extra={"error": str(exc), "path": str(py_config_path)},
        )
