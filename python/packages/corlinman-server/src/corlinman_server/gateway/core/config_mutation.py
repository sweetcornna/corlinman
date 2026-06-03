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

from typing import Any

from fastapi.responses import JSONResponse

__all__ = ["write_config_atomic"]


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
