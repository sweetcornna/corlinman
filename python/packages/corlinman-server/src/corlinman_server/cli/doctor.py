"""``corlinman doctor`` — diagnostic checks.

Python port of ``rust/crates/corlinman-cli/src/cmd/doctor/mod.rs``.

The Rust crate maintains a large suite of checks (provider HTTPS,
manifest duplicates, scheduler, etc.); the Python port ships a smaller
beachhead set that the Python AI plane can introspect without reaching
into the Rust gateway's config:

* ``data_dir`` — does the data directory exist and is it writable?
* ``config`` — is ``<data_dir>/config.toml`` present?
* ``python`` — interpreter version sanity check (matches the
  ``requires-python = ">=3.12"`` constraint).
* ``packages`` — import smoke for the workspace siblings the AI plane
  depends on at runtime.

Each check returns ``(status, message, hint)`` where ``status`` is one
of ``ok|warn|fail``. Only ``fail`` flips the exit code, matching the
Rust contract ("warnings are informational — we don't want
``doctor`` in a CI loop to fail just because the user hasn't configured
a provider yet").
"""

from __future__ import annotations

import importlib
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from corlinman_server.cli._common import echo_json, resolve_data_dir


@dataclass(slots=True)
class CheckReport:
    """Single check result. Wire shape matches the Rust
    ``CheckReport`` JSON serialisation: ``{name, status, message, hint?}``.
    """

    name: str
    status: str  # "ok" | "warn" | "fail"
    message: str
    hint: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.hint is not None:
            out["hint"] = self.hint
        return out


def _check_data_dir(data_dir: Path) -> CheckReport:
    if not data_dir.exists():
        return CheckReport(
            name="data_dir",
            status="warn",
            message=f"{data_dir} does not exist",
            hint="run `corlinman onboard --non-interactive --accept-risk`",
        )
    if not os.access(data_dir, os.W_OK):
        return CheckReport(
            name="data_dir",
            status="fail",
            message=f"{data_dir} is not writable",
            hint="check ownership / permissions",
        )
    return CheckReport(
        name="data_dir",
        status="ok",
        message=str(data_dir),
    )


def _check_config(data_dir: Path) -> CheckReport:
    config_path = data_dir / "config.toml"
    if not config_path.exists():
        return CheckReport(
            name="config",
            status="warn",
            message=f"{config_path} not present",
            hint="run `corlinman onboard` or `corlinman config init`",
        )
    return CheckReport(
        name="config",
        status="ok",
        message=str(config_path),
    )


def _check_python() -> CheckReport:
    info = sys.version_info
    if info < (3, 12):
        return CheckReport(
            name="python",
            status="fail",
            message=f"python {info.major}.{info.minor} < 3.12 (requires-python)",
            hint="install python 3.12 or newer",
        )
    return CheckReport(
        name="python",
        status="ok",
        message=f"python {info.major}.{info.minor}.{info.micro}",
    )


# Workspace packages the AI plane imports at runtime. Each is asserted
# importable so a missing dep flag-and-fails fast — much friendlier than
# the gRPC server blowing up at boot with an `ImportError`.
_REQUIRED_PACKAGES: tuple[str, ...] = (
    "corlinman_server",
    "corlinman_providers",
    "corlinman_replay",
)


def _check_packages() -> CheckReport:
    missing: list[str] = []
    for pkg in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
        except Exception:  # noqa: BLE001 — any import failure is a fail
            missing.append(pkg)
    if missing:
        return CheckReport(
            name="packages",
            status="fail",
            message=f"missing imports: {', '.join(missing)}",
            hint="run `uv sync` in the repo root",
        )
    return CheckReport(
        name="packages",
        status="ok",
        message=f"all {len(_REQUIRED_PACKAGES)} required packages importable",
    )


def _check_runtime_config(data_dir: Path) -> CheckReport:
    """Try to load and parse the gateway ``config.toml`` via the same loader
    the gateway uses at boot (``gateway.core.config.load_from_path``).

    * ``ok`` — config file was found, parsed, and env-refs resolved without
      error. Reports section names discovered.
    * ``warn`` — config file is absent (not created yet); matches the same
      ``warn`` signal :func:`_check_config` emits.
    * ``fail`` — file exists but the loader raised (TOML parse error, or the
      ``gateway.core.config`` module itself failed to import).

    Degrades gracefully: any unexpected error is caught and reported as
    ``fail`` so a broken module never crashes ``corlinman doctor``.
    """
    config_path = data_dir / "config.toml"
    if not config_path.exists():
        return CheckReport(
            name="runtime_config",
            status="warn",
            message=f"{config_path} not present — gateway will boot degraded",
            hint="run `corlinman onboard` or `corlinman config init`",
        )
    try:
        from corlinman_server.gateway.core.config import load_from_path

        cfg: Any = load_from_path(config_path)
    except Exception as exc:  # noqa: BLE001 — report, never crash
        return CheckReport(
            name="runtime_config",
            status="fail",
            message=f"failed to load {config_path}: {exc}",
            hint="check TOML syntax with `python -m tomllib <config.toml>`",
        )
    sections = sorted(cfg.keys()) if isinstance(cfg, dict) else []
    return CheckReport(
        name="runtime_config",
        status="ok",
        message=f"{config_path} — sections: {', '.join(sections) or '(empty)'}",
    )


def _check_provider_registry(data_dir: Path) -> CheckReport:
    """Try to build a :class:`ProviderRegistry` from the gateway config.

    This exercises the same P1 code path the gateway lifespan uses:
    ``gateway.providers.build_registry(cfg)``.

    * ``ok`` — registry built; reports number of provider specs registered.
    * ``warn`` — config absent or ``[providers]`` section missing / empty
      (degraded but not broken — bare ``OPENAI_API_KEY`` deployments are ok).
    * ``fail`` — registry construction raised unexpectedly.

    Degrades gracefully: any unexpected error is caught and reported as
    ``fail`` rather than crashing the doctor command.
    """
    config_path = data_dir / "config.toml"
    if not config_path.exists():
        return CheckReport(
            name="provider_registry",
            status="warn",
            message="config.toml absent — cannot build provider registry",
            hint="run `corlinman onboard` or `corlinman config init`",
        )
    try:
        from corlinman_server.gateway.core.config import load_from_path
        from corlinman_server.gateway.providers import build_registry

        cfg = load_from_path(config_path)
        registry = build_registry(cfg)
        specs = registry.list_specs() if registry is not None else []
        count = len(specs)
    except Exception as exc:  # noqa: BLE001 — report, never crash
        return CheckReport(
            name="provider_registry",
            status="fail",
            message=f"provider registry build failed: {exc}",
            hint="verify [providers] section in config.toml",
        )

    if count == 0:
        return CheckReport(
            name="provider_registry",
            status="warn",
            message=(
                "provider registry built with 0 specs — gateway wired but no"
                " configured providers (bare env-var auth still works)"
            ),
            hint="add a [providers.<name>] section to config.toml",
        )
    return CheckReport(
        name="provider_registry",
        status="ok",
        message=f"provider registry: {count} spec(s) registered",
    )


def _check_runtime_wiring(data_dir: Path) -> CheckReport:
    """Synthetic check mirroring ``GET /health`` ``mode`` field.

    Reports ``ok`` when both ``provider_registry`` and ``chat`` service
    can be constructed from the on-disk config, ``degraded`` when either
    slot would remain ``None`` at runtime (matching the ``/health`` endpoint
    convention), or ``fail`` when an unexpected error prevents the check.

    This is an *offline* check — it does not start the gateway or contact a
    running process. It just simulates the boot-time wiring steps and reports
    what the gateway's ``mode`` would be.
    """
    config_path = data_dir / "config.toml"
    if not config_path.exists():
        return CheckReport(
            name="runtime_wiring",
            status="warn",
            message="config.toml absent — gateway would boot degraded",
            hint="run `corlinman onboard` or `corlinman config init`",
        )
    try:
        from corlinman_server.gateway.core.config import load_from_path
        from corlinman_server.gateway.providers import build_registry
        from corlinman_server.gateway.services.chat_bootstrap import build_chat_service

        cfg = load_from_path(config_path)

        # Simulate P1 wiring
        registry = build_registry(cfg)

        # Simulate P2 wiring via a minimal state-like namespace
        class _MockState:
            pass

        mock_state = _MockState()
        mock_state.provider_registry = registry  # type: ignore[attr-defined]
        mock_state.config = cfg  # type: ignore[attr-defined]
        mock_state.chat = None  # type: ignore[attr-defined]

        chat_service = build_chat_service(mock_state)

    except Exception as exc:  # noqa: BLE001 — report, never crash
        return CheckReport(
            name="runtime_wiring",
            status="fail",
            message=f"runtime wiring simulation failed: {exc}",
            hint="check gateway logs for details after a boot attempt",
        )

    registry_ok = registry is not None and len(registry.list_specs()) > 0
    chat_ok = chat_service is not None

    if registry_ok and chat_ok:
        return CheckReport(
            name="runtime_wiring",
            status="ok",
            message="mode=ok — provider_registry and chat wired",
        )

    missing = []
    if not registry_ok:
        missing.append("provider_registry (no providers configured)")
    if not chat_ok:
        missing.append("chat (no provider registry → no ChatService)")
    return CheckReport(
        name="runtime_wiring",
        status="warn",
        message=f"mode=degraded — not wired: {', '.join(missing)}",
        hint=(
            "add providers to config.toml; see docs/contracts/runtime-wiring.md"
        ),
    )


def _check_must_change_password(data_dir: Path) -> CheckReport:
    """Warn when the seeded ``admin/root`` default has not been rotated.

    * ``warn`` — ``[admin].must_change_password`` is ``true`` in the on-disk
      config (the bootstrap default is still active).
    * ``ok`` — flag is absent or set to ``false`` (the operator either
      hand-wrote credentials or rotated through ``/account/security``).
    * ``warn`` (degraded) — config file absent altogether; we can't tell.

    Never raises; any unexpected parse failure is reported as ``warn`` so
    ``doctor`` stays green-tinted on a healthy host.
    """
    config_path = data_dir / "config.toml"
    if not config_path.exists():
        return CheckReport(
            name="must_change_password",
            status="warn",
            message="config.toml absent — cannot verify admin credentials",
            hint="run `corlinman init` to seed credentials",
        )
    try:
        import tomllib

        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — never crash doctor
        return CheckReport(
            name="must_change_password",
            status="warn",
            message=f"failed to parse {config_path}: {exc}",
            hint="check TOML syntax with `python -m tomllib <config.toml>`",
        )
    admin = parsed.get("admin")
    if not isinstance(admin, dict):
        return CheckReport(
            name="must_change_password",
            status="warn",
            message="[admin] block missing — credentials will be seeded on next boot",
            hint="run `corlinman init` or start the gateway once to seed",
        )
    if bool(admin.get("must_change_password", False)):
        return CheckReport(
            name="must_change_password",
            status="warn",
            message="default password still active (admin/root)",
            hint="run `corlinman init` or change in /account/security",
        )
    return CheckReport(
        name="must_change_password",
        status="ok",
        message="custom credentials set",
    )


def _resolve_port(data_dir: Path) -> int:
    """Best-effort port resolution mirroring entrypoint.py's
    :func:`_resolve_bind`: ``[server].port`` in TOML → ``$CORLINMAN_PORT`` →
    ``$PORT`` → default ``6005``.
    """
    config_path = data_dir / "config.toml"
    if config_path.exists():
        try:
            import tomllib

            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            server = parsed.get("server")
            if isinstance(server, dict):
                port = server.get("port")
                if isinstance(port, int) and 0 < port < 65536:
                    return port
                # ``bind`` form: e.g. "0.0.0.0:6005"
                bind = server.get("bind")
                if isinstance(bind, str) and ":" in bind:
                    tail = bind.rsplit(":", 1)[1]
                    if tail.isdigit():
                        parsed_port = int(tail)
                        if 0 < parsed_port < 65536:
                            return parsed_port
        except Exception:  # noqa: BLE001 — fall through to env / default
            pass
    for env_var in ("CORLINMAN_PORT", "PORT"):
        v = os.environ.get(env_var)
        if v and v.isdigit():
            parsed_port = int(v)
            if 0 < parsed_port < 65536:
                return parsed_port
    return 6005


def _port_holder_is_corlinman(port: int) -> bool | None:
    """Best-effort: ask ``lsof`` whether a corlinman process holds ``port``.

    Returns ``True`` / ``False`` when we got a clean answer, or ``None`` when
    ``lsof`` is unavailable / errored — callers should treat ``None`` as
    "can't tell" rather than as a definitive "not corlinman".
    """
    import shutil
    import subprocess

    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    try:
        # ``-iTCP:<port>`` + ``-sTCP:LISTEN`` + ``-P -n -F c`` → one ``c<cmd>``
        # line per holding process.
        result = subprocess.run(
            [lsof, f"-iTCP:{port}", "-sTCP:LISTEN", "-P", "-n", "-F", "c"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:  # noqa: BLE001 — lsof flaky → unknown
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("c") and "corlinman" in line.lower():
            return True
    return False


def _check_port_bindable(data_dir: Path) -> CheckReport:
    """Try to bind the gateway port; report whether it's free or taken.

    * ``ok`` — port is bindable (free) OR is held by a corlinman process
      (gateway is already running).
    * ``warn`` — port is held by a non-corlinman process; suggest stopping
      it or overriding ``$CORLINMAN_PORT``.

    Never raises; any unexpected socket error is reported as ``warn``.
    """
    port = _resolve_port(data_dir)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        holder = _port_holder_is_corlinman(port)
        if holder is True:
            return CheckReport(
                name="port_bindable",
                status="ok",
                message=f"gateway running on port {port}",
            )
        if holder is False:
            return CheckReport(
                name="port_bindable",
                status="warn",
                message=f"port {port} held by another process",
                hint="stop the other process or set CORLINMAN_PORT",
            )
        # holder is None — lsof unavailable; degrade gracefully
        return CheckReport(
            name="port_bindable",
            status="warn",
            message=f"port {port} occupied (holder unknown)",
            hint="stop the other process or set CORLINMAN_PORT",
        )
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    return CheckReport(
        name="port_bindable",
        status="ok",
        message=f"port {port} available",
    )


# Registered checks; keep the names stable so ``--module`` filtering is
# scriptable. Insertion order is the display order.
_CHECK_FNS = {
    "data_dir": lambda dd: _check_data_dir(dd),
    "config": lambda dd: _check_config(dd),
    "python": lambda _dd: _check_python(),
    "packages": lambda _dd: _check_packages(),
    "runtime_config": lambda dd: _check_runtime_config(dd),
    "provider_registry": lambda dd: _check_provider_registry(dd),
    "runtime_wiring": lambda dd: _check_runtime_wiring(dd),
    "must_change_password": lambda dd: _check_must_change_password(dd),
    "port_bindable": lambda dd: _check_port_bindable(dd),
}


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human-readable output.")
@click.option(
    "--module",
    "module",
    default=None,
    help="Run a single check by name (e.g. `data_dir`, `config`, `python`, `packages`).",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override data-dir (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
)
def doctor(as_json: bool, module: str | None, data_dir: Path | None) -> None:
    """Run diagnostic checks across data-dir / config / runtime."""
    dd = resolve_data_dir(data_dir)

    items = list(_CHECK_FNS.items())
    if module is not None:
        items = [(name, fn) for name, fn in items if name == module]
        if not items:
            click.echo(f"error: no check named '{module}'", err=True)
            sys.exit(2)

    reports = [fn(dd) for _name, fn in items]

    if as_json:
        echo_json([r.to_dict() for r in reports])
    else:
        _print_human(reports)

    if any(r.status == "fail" for r in reports):
        sys.exit(1)


def _print_human(reports: list[CheckReport]) -> None:
    name_w = max((len(r.name) for r in reports), default=0)
    name_w = max(name_w, 8)
    fails = warns = oks = 0
    for r in reports:
        if r.status == "ok":
            glyph = "✓"
            oks += 1
        elif r.status == "warn":
            glyph = "!"
            warns += 1
        else:
            glyph = "✗"
            fails += 1
        click.echo(f"{glyph} {r.name:<{name_w}}  {r.message}")
        if r.hint:
            click.echo(f"  {'':<{name_w}}  hint: {r.hint}")
    click.echo("")
    click.echo(f"{fails} fail, {warns} warn, {oks} ok")


__all__ = ["CheckReport", "doctor"]
