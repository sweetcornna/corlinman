"""``corlinman init`` — interactive headless-server first-run wizard.

Where :mod:`corlinman_server.cli.onboard` (a Rust 1:1 port) only writes
a skeleton directory + stub TOML, ``corlinman init`` is the
*operator-facing* setup CLI for a fresh server with no browser. It
walks the same steps the web ``/onboard`` wizard does:

1. Rotate the default ``admin/root`` password (if ``must_change_password``
   is still set).
2. Pick a built-in provider kind and paste its API key.
3. Write ``[providers.<name>]`` + ``[models]`` default alias to the
   on-disk ``config.toml``.
4. Optionally enable an embedding provider.

The TOML write shape is intentionally identical to what
``POST /admin/onboard/finalize`` emits in
:mod:`corlinman_server.gateway.routes_admin_b.onboard` so an operator
can mix the CLI and the UI without diverging on-disk layouts.

We don't go through the HTTP endpoint — that requires a running
gateway + an authenticated session. Instead we use the same
``tomli_w`` writer + atomic-rename pattern the finalize handler uses
(:func:`_write_config_atomic` in that module) and the same
:func:`corlinman_server.gateway.lifecycle.admin_seed._hash_password`
helper for argon2id hashing. The two surfaces stay aligned because they
write the same structured data; they just route the inputs differently.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from corlinman_server.cli._common import resolve_data_dir


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------

#: Hard-coded fallback list used when the provider registry cannot be
#: imported (CLI is supposed to keep working even when downstream packages
#: aren't on the path). The order matches the ``ProviderKind`` enum's
#: declaration order so the UI dropdown is identical to the CLI menu.
_FALLBACK_KINDS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "gemini",
    "deepseek",
    "qwen",
    "glm",
    "openai_compatible",
    "mock",
)


def _supported_kinds() -> list[str]:
    """Return canonical provider kinds; degrade to ``_FALLBACK_KINDS`` on
    import failure so the CLI doesn't hard-fail on a missing sibling."""
    try:
        from corlinman_providers.specs import list_supported_kinds

        kinds = list_supported_kinds()
        if kinds:
            return kinds
    except Exception:  # noqa: BLE001 — best-effort import
        pass
    return list(_FALLBACK_KINDS)


# ---------------------------------------------------------------------------
# Config IO — mirrors gateway/routes_admin_b/onboard.py::_write_config_atomic
# ---------------------------------------------------------------------------


def _load_existing(config_path: Path) -> dict[str, Any]:
    """Return parsed TOML or an empty dict if the file is absent / unreadable."""
    if not config_path.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover — Python <3.11
        return {}
    try:
        return dict(tomllib.loads(config_path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 — operator typo'd; treat as empty
        return {}


def _atomic_write_toml(path: Path, cfg: dict[str, Any]) -> None:
    """Serialise ``cfg`` to TOML and ``<path>.new`` → ``os.replace`` swap.

    Mirrors :func:`_write_config_atomic` from
    ``gateway/routes_admin_b/onboard.py`` so the on-disk shape is
    bit-identical between the CLI and the web wizard.
    """
    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover — declared dep
        raise click.ClickException(f"tomli_w unavailable: {exc}") from exc
    serialised = tomli_w.dumps(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(serialised, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Admin password rotation — mirrors admin_seed.py
# ---------------------------------------------------------------------------


def _hash_password(plaintext: str) -> str:
    """Argon2id hash. Lazy-import keeps the CLI usable without argon2-cffi
    pre-installed (the function only runs on the password-change branch)."""
    from corlinman_server.gateway.routes_admin_a.auth import hash_password

    return hash_password(plaintext)


# ---------------------------------------------------------------------------
# Wizard step helpers — each is a small unit so tests can drive them.
# ---------------------------------------------------------------------------


def _print_state(cfg: dict[str, Any]) -> None:
    """Print a short banner describing what's already configured."""
    admin = cfg.get("admin") or {}
    providers = cfg.get("providers") or {}
    models = cfg.get("models") or {}

    click.echo("corlinman init — interactive setup")
    click.echo("─" * 48)
    click.echo(f"  admin user            : {admin.get('username') or '(unset)'}")
    click.echo(
        f"  must_change_password  : {bool(admin.get('must_change_password', False))}"
    )
    if isinstance(providers, dict) and providers:
        click.echo(f"  providers configured  : {', '.join(sorted(providers))}")
    else:
        click.echo("  providers configured  : (none)")
    click.echo(f"  default model alias   : {models.get('default') or '(unset)'}")
    click.echo("─" * 48)


def _maybe_rotate_admin(cfg: dict[str, Any]) -> bool:
    """Prompt for a new admin password when ``must_change_password`` is set.

    Returns ``True`` when ``cfg`` was modified in place.
    """
    admin = dict(cfg.get("admin") or {})
    must_change = bool(admin.get("must_change_password", False))
    if not must_change:
        if click.confirm("Change admin password now?", default=False):
            new_pw = click.prompt(
                "New admin password",
                hide_input=True,
                confirmation_prompt=True,
            )
            admin["password_hash"] = _hash_password(new_pw)
            admin["must_change_password"] = False
            if "username" not in admin:
                admin["username"] = "admin"
            cfg["admin"] = admin
            click.echo("  ✓ admin password updated")
            return True
        return False

    click.echo("! default admin password is still active (admin/root)")
    if not click.confirm("Set a new admin password now?", default=True):
        click.echo("  (skipped — leaving default credentials in place)")
        return False
    new_pw = click.prompt(
        "New admin password",
        hide_input=True,
        confirmation_prompt=True,
    )
    admin["password_hash"] = _hash_password(new_pw)
    admin["must_change_password"] = False
    if "username" not in admin:
        admin["username"] = "admin"
    cfg["admin"] = admin
    click.echo("  ✓ admin password updated")
    return True


def _maybe_configure_provider(cfg: dict[str, Any]) -> bool:
    """Prompt the operator to pick a provider kind + paste an API key.

    Writes a ``[providers.<name>]`` block + sets a default model alias
    matching the wire shape used by ``POST /admin/onboard/finalize``.

    Returns ``True`` when ``cfg`` was modified.
    """
    if not click.confirm("Configure an LLM provider now?", default=True):
        click.echo("  (skipped — no provider configured)")
        return False

    kinds = _supported_kinds()
    click.echo("")
    click.echo("Built-in provider kinds:")
    for idx, kind in enumerate(kinds, start=1):
        click.echo(f"  [{idx}] {kind}")
    click.echo("")

    while True:
        choice = click.prompt(
            "Pick a kind (number or name)",
            default=kinds[0],
        )
        kind: str | None = None
        if choice.isdigit() and 1 <= int(choice) <= len(kinds):
            kind = kinds[int(choice) - 1]
        elif choice in kinds:
            kind = choice
        if kind is not None:
            break
        click.echo(f"  invalid choice: {choice!r}; try again")

    provider_name = click.prompt(
        "Provider slot name (the X in [providers.X])", default=kind
    )

    base_url: str | None = None
    if kind == "openai_compatible":
        base_url = click.prompt(
            "Base URL (e.g. https://api.example.com/v1)", default=""
        )
        base_url = base_url or None

    api_key: str | None = None
    if kind != "mock":
        api_key = click.prompt(
            f"{kind} API key",
            hide_input=True,
            default="",
            show_default=False,
        )
        api_key = api_key or None

    new_entry: dict[str, Any] = {
        "kind": kind,
        "enabled": True,
        "params": {},
    }
    if base_url is not None:
        new_entry["base_url"] = base_url
    if api_key is not None:
        new_entry["api_key"] = {"value": api_key}

    providers = dict(cfg.get("providers") or {})
    providers[provider_name] = new_entry
    cfg["providers"] = providers

    model_alias = click.prompt(
        "Default model alias (e.g. gpt-4o-mini, claude-3-5-sonnet-latest)",
        default="default",
    )

    models_cfg = dict(cfg.get("models") or {})
    models_cfg["default"] = model_alias
    aliases = dict(models_cfg.get("aliases") or {})
    aliases[model_alias] = {
        "model": model_alias,
        "provider": provider_name,
        "params": {},
    }
    models_cfg["aliases"] = aliases
    cfg["models"] = models_cfg

    click.echo(f"  ✓ provider '{provider_name}' ({kind}) configured")
    click.echo(f"  ✓ default model alias → {model_alias}")

    if click.confirm("Enable an embedding provider?", default=False):
        embedding_model = click.prompt(
            "Embedding model (e.g. text-embedding-3-small)",
            default="text-embedding-3-small",
        )
        cfg["embedding"] = {
            "provider": provider_name,
            "model": embedding_model,
            "dimension": 1536,
            "enabled": True,
            "params": {},
        }
        click.echo(f"  ✓ embedding → {embedding_model}")

    return True


# ---------------------------------------------------------------------------
# Public command
# ---------------------------------------------------------------------------


@click.command("init")
@click.option(
    "--config",
    "config_arg",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to config.toml (default: $CORLINMAN_DATA_DIR/config.toml or ~/.corlinman/config.toml).",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override data-dir (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
)
def init(config_arg: Path | None, data_dir: Path | None) -> None:
    """Interactive headless setup — pick provider, set admin password, write config.

    Walks the operator through the same steps as the web ``/onboard`` wizard
    but without needing a browser. Safe to re-run; existing sections are
    preserved verbatim except for the ones the operator chooses to update.
    """
    config_path = _resolve_config_path(config_arg, data_dir)
    cfg = _load_existing(config_path)

    _print_state(cfg)
    click.echo("")

    changed_admin = _maybe_rotate_admin(cfg)
    click.echo("")
    changed_provider = _maybe_configure_provider(cfg)
    click.echo("")

    if not (changed_admin or changed_provider):
        click.echo("Nothing to write — config left unchanged.")
        return

    try:
        _atomic_write_toml(config_path, cfg)
    except OSError as exc:
        click.echo(f"error: failed to write {config_path}: {exc}", err=True)
        sys.exit(1)

    click.echo(f"✓ wrote {config_path}")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  • restart corlinman:  systemctl restart corlinman")
    click.echo("    (docker mode:        docker compose restart corlinman)")
    click.echo("  • verify health:       curl -fsS http://localhost:6005/health")


def _resolve_config_path(cli_config: Path | None, data_dir: Path | None) -> Path:
    """Mirror :func:`admin_seed.resolve_admin_config_path` resolution order.

    1. explicit ``--config`` flag
    2. ``<data_dir>/config.toml`` (data_dir via ``--data-dir`` or env)
    """
    if cli_config is not None:
        return cli_config
    return resolve_data_dir(data_dir) / "config.toml"


__all__ = [
    "init",
    "_atomic_write_toml",
    "_load_existing",
    "_maybe_configure_provider",
    "_maybe_rotate_admin",
    "_resolve_config_path",
    "_supported_kinds",
]
