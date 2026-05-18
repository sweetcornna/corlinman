"""``corlinman config`` — typed edits to ``config.toml``.

Python port of ``rust/crates/corlinman-cli/src/cmd/config.rs``.

The Rust port goes through ``corlinman_core::config::Config`` (a typed
struct with redaction, validation, and dotted-key get/set). There is no
typed Python config sibling yet — the AI plane reads the same TOML file
the Rust gateway writes, so the Python port treats the file as opaque
TOML and gives operators the basics: ``show``, ``get``, ``set``,
``init``. ``validate`` and ``diff`` are intentional stubs that exit 2
(``not yet ported``) since the structural validators live in Rust.

Secret redaction (``api_key`` values) is reimplemented locally so
``show`` / ``get`` never echo plaintext keys to stdout — the Rust
``Config::redacted`` pass replaces every ``api_key`` field with
``"***"`` and the Python port follows suit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

try:  # Python 3.11+ stdlib
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    import tomli as tomllib  # type: ignore[no-redef]

from corlinman_server.cli._common import (
    default_config_path,
    echo_json,
    todo_stub,
)

_DEFAULT_CONFIG_BODY = """# corlinman starter config (written by `corlinman config init`)

[server]
port = 6005
bind = "0.0.0.0"

[admin]
# username = "admin"
# password_hash = "$argon2id$..."
"""


def _resolve_path(explicit: Path | None) -> Path:
    return Path(explicit) if explicit is not None else default_config_path()


def _load(path: Path) -> dict[str, object]:
    if not path.exists():
        click.echo(f"error: config not found at {path}", err=True)
        sys.exit(1)
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _redact(value: object) -> object:
    """Recursively replace ``api_key`` values with ``"***"`` to keep
    secrets out of ``show`` / ``get`` output. Mirrors
    ``Config::redacted`` in the Rust port."""
    if isinstance(value, dict):
        return {
            k: ("***" if k == "api_key" and v else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _dump_toml(value: dict[str, object]) -> str:
    """Best-effort TOML serialisation without a third-party dep.

    The stdlib does not ship a TOML writer. For ``show`` output we only
    need a human-readable round-trip — round-trips back through
    ``tomllib`` aren't required (the operator edits via ``config set``
    or by hand). Falls back to ``repr`` for unsupported types.
    """
    lines: list[str] = []
    scalars: dict[str, object] = {}
    tables: dict[str, object] = {}
    for k, v in value.items():
        if isinstance(v, dict):
            tables[k] = v
        else:
            scalars[k] = v

    def _emit_scalar(val: object) -> str:
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, str):
            # Naive escape — good enough for redacted display output.
            escaped = val.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        if isinstance(val, list):
            inner = ", ".join(_emit_scalar(x) for x in val)
            return f"[{inner}]"
        # Inline tables / unsupported — represent as TOML inline.
        return repr(val)

    for k, v in scalars.items():
        lines.append(f"{k} = {_emit_scalar(v)}")
    if scalars and tables:
        lines.append("")

    def _emit_table(prefix: str, table: dict[str, object]) -> None:
        # Split nested tables out so inline scalars stay under the
        # current header.
        nested: dict[str, dict[str, object]] = {}
        flat: dict[str, object] = {}
        for k, v in table.items():
            if isinstance(v, dict):
                nested[k] = v  # type: ignore[assignment]
            else:
                flat[k] = v
        lines.append(f"[{prefix}]")
        for k, v in flat.items():
            lines.append(f"{k} = {_emit_scalar(v)}")
        lines.append("")
        for k, v in nested.items():
            _emit_table(f"{prefix}.{k}", v)

    for k, v in tables.items():
        if isinstance(v, dict):
            _emit_table(k, v)

    return "\n".join(lines).rstrip() + "\n"


def _get_dotted(data: dict[str, object], key: str) -> object:
    """Traverse ``data`` along the dotted ``key`` path."""
    cur: object = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(key)
        cur = cur[part]
    return cur


# --- click commands ------------------------------------------------------


@click.group("config", help="Configuration management for ``config.toml``.")
def config() -> None:
    """``config`` subcommand group."""


@config.command("show")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of TOML.")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def show_cmd(as_json: bool, path: Path | None) -> None:
    """Print the full config (secrets redacted)."""
    p = _resolve_path(path)
    data = _load(p)
    redacted = _redact(data)
    if as_json:
        echo_json(redacted)
    else:
        click.echo(_dump_toml(redacted))  # type: ignore[arg-type]


@config.command("get")
@click.argument("key")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def get_cmd(key: str, path: Path | None) -> None:
    """Read a dotted key (e.g. ``server.port``)."""
    p = _resolve_path(path)
    data = _redact(_load(p))
    try:
        value = _get_dotted(data, key)  # type: ignore[arg-type]
    except KeyError:
        click.echo(f"error: cannot read '{key}': not found", err=True)
        sys.exit(1)
    click.echo(str(value))


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def set_cmd(key: str, value: str, path: Path | None) -> None:
    """Set a dotted scalar key (writes ``config.toml`` line-wise).

    The Rust port uses ``Config::set_dotted`` + ``save_to_path`` (a
    serde round-trip through a typed struct). Without a typed Python
    sibling, this command exits ``2`` with a "not yet ported"
    message — the structural rewrite is best deferred to the typed
    config sibling rather than re-implemented by hand here.
    """
    todo_stub("config set")


@config.command("validate")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def validate_cmd(path: Path | None) -> None:
    """Run every validator; non-zero exit on any issue. STUB — depends on typed config."""
    todo_stub("config validate")


@config.command("init")
@click.option("--path", type=click.Path(path_type=Path), default=None)
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
def init_cmd(path: Path | None, force: bool) -> None:
    """Write a default config to ``~/.corlinman/config.toml`` (or ``--path``)."""
    p = _resolve_path(path)
    if p.exists() and not force:
        click.echo(f"error: {p} already exists; pass --force to overwrite", err=True)
        sys.exit(1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_DEFAULT_CONFIG_BODY, encoding="utf-8")
    click.echo(f"wrote default config to {p}")


@config.command("diff")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def diff_cmd(path: Path | None) -> None:
    """Diff current config against defaults. STUB — depends on typed config."""
    todo_stub("config diff")


__all__ = ["config"]
