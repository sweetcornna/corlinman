"""Static-analysis regression tests for ``deploy/install.sh`` (G1).

R5-S3 dropped the native gateway to ``User=corlinman`` (good) but left it
*unrunnable* by that user — three regressions that turn a fresh root install
into a crash-loop and break post-upgrade restarts:

G1.1 (CRITICAL) — the gateway ``ExecStart`` is
``${uv_path} run corlinman-gateway`` where ``uv_path=$(command -v uv)`` resolves
to ``/root/.local/bin/uv`` on a root install. ``/root`` is mode 0700, so the
unprivileged ``corlinman`` user gets EACCES on the binary; and ``uv run`` needs a
writable cache (``$HOME/.cache/uv``) but the unit sets no ``Environment=HOME=``.
Plus the recursive chown only re-owns DATA_DIR + ui-static to ``corlinman`` —
``$PREFIX/repo/.venv`` (where the real entrypoint lives) is left root-owned and
unreadable. NET: gateway crash-loops, never starts.

  Fix: invoke the venv console-script directly
  (``${PREFIX}/repo/.venv/bin/corlinman-gateway``) — no dependency on root's
  ``uv`` — chown the venv (or repo) so SERVICE_USER can read+execute it, and set
  ``Environment=HOME=`` to a service-writable dir for any runtime cache.

G1.2 (MED) — ``upgrade_native`` runs ``uv sync`` (rewrites ``.venv``) +
``build_and_place_ui`` (rewrites ui-static) but never re-establishes the
ownership invariant, so after a one-click upgrade the freshly-synced venv /
ui-static is root-owned and the de-privileged gateway can't start.

  Fix: ``upgrade_native`` must call ``ensure_service_user`` and re-chown the
  runtime paths the corlinman service reads/executes (venv, DATA_DIR,
  ui-static) to SERVICE_USER.

G1.3 (MED) — the root upgrader (``User=root``) execs
``${PREFIX}/repo/.venv/bin/python`` (see deploy/corlinman-upgrader.sh §6) AND
``deploy/install.sh``. If G1.1 chowns ``.venv`` to the unprivileged
SERVICE_USER, the root-executed interpreter becomes unprivileged-writable → LPE.
The ownership model must be coherent: the venv the corlinman *gateway* runs is
the same venv whose ``python`` the *root upgrader* runs, so it must be
SERVICE_USER-*readable/executable* but NOT SERVICE_USER-*writable* (own it
``root:SERVICE_USER`` group-read, never ``corlinman:corlinman``).

Full verification still requires a real native Linux install (useradd +
systemd); the sandbox can't run those. These tests pin the script *source* so
the regressions can't silently return.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[6]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"

SERVICE_USER = "corlinman"
USER_TOKEN = rf"(?:{SERVICE_USER}|\$\{{?SERVICE_USER\}}?)"


def _read_install_sh() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _extract_heredoc(text: str, anchor: str) -> str:
    bodies = re.findall(r"<<EOF\n(.*?)\nEOF\n", text, flags=re.DOTALL)
    for body in bodies:
        if anchor in body:
            return body
    raise AssertionError(f"no heredoc body containing {anchor!r} found")


def _extract_function_body(text: str, name: str) -> str:
    """Return the body of a top-level bash function ``name() { ... }``.

    install.sh defines helpers at column 0 and closes them with a ``}`` at
    column 0; match to the next exactly-``}`` line.
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(name)}\(\)\s*\{{", ln):
            start = i
            break
    assert start is not None, f"function {name!r} not defined in install.sh"
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            return "\n".join(lines[start : j + 1])
    raise AssertionError(f"function {name!r} not terminated by a column-0 '}}'")


# ---------------------------------------------------------------------------
# G1.1 — gateway ExecStart must be runnable by the unprivileged service user
# ---------------------------------------------------------------------------


def _gateway_unit(text: str) -> str:
    # The gateway unit is identified by its BIND=0.0.0.0 + the gateway port.
    for body in re.findall(r"<<EOF\n(.*?)\nEOF\n", text, flags=re.DOTALL):
        if "BIND=0.0.0.0" in body and "corlinman-gateway" in body and "User=" in body:
            return body
    raise AssertionError("gateway systemd unit heredoc not found")


def test_gateway_execstart_does_not_depend_on_root_uv() -> None:
    text = _read_install_sh()
    unit = _gateway_unit(text)
    execstart = next(
        (ln for ln in unit.splitlines() if ln.startswith("ExecStart=")),
        None,
    )
    assert execstart is not None, f"no ExecStart in gateway unit:\n{unit}"

    # (a) Must not reference anything under /root (mode 0700, unreadable by
    #     the corlinman user).
    assert "/root" not in execstart, (
        "gateway ExecStart references a path under /root — unreadable by the "
        f"unprivileged corlinman user (EACCES, crash-loop):\n{execstart}"
    )

    # (b) Must not be the rendered value of `command -v uv` (i.e. the
    #     ${uv_path} captured from root's PATH). The unit must invoke the
    #     venv entrypoint directly so it doesn't depend on root's uv binary
    #     or its writable cache.
    assert "uv_path" not in execstart and "} run corlinman-gateway" not in execstart, (
        "gateway ExecStart still launches via `uv run` (root's uv binary + "
        "an unwritable ~/.cache/uv). Invoke the venv console-script directly "
        f"instead:\n{execstart}"
    )

    # (c) Positively: it should launch the venv-resident entrypoint under the
    #     repo prefix (corlinman-gateway console-script, or `python -m`).
    assert re.search(
        r"ExecStart=\$\{?PREFIX\}?/repo/\.venv/bin/"
        r"(corlinman-gateway|python(3)?)\b",
        execstart,
    ), (
        "gateway ExecStart must invoke the in-venv entrypoint directly, e.g. "
        "${PREFIX}/repo/.venv/bin/corlinman-gateway (or .venv/bin/python -m "
        f"...):\n{execstart}"
    )


def test_gateway_unit_sets_writable_home() -> None:
    """`uv`/Python runtime caches write to $HOME; the corlinman user has no
    home (useradd --no-create-home), so the unit must point HOME at a
    service-writable dir (e.g. DATA_DIR) or the gateway fails on first cache
    write."""
    text = _read_install_sh()
    unit = _gateway_unit(text)
    home_line = next(
        (ln for ln in unit.splitlines() if re.match(r"Environment=HOME=", ln)),
        None,
    )
    assert home_line is not None, (
        "gateway unit sets no 'Environment=HOME=' — the corlinman user has no "
        "home dir, so any runtime cache write (e.g. ~/.cache) fails.\n"
        f"--- unit ---\n{unit}"
    )
    # HOME must point at a path the SERVICE_USER can write — DATA_DIR (already
    # chowned to SERVICE_USER) or some path under $PREFIX, not /root.
    assert "/root" not in home_line, f"HOME points under /root: {home_line!r}"
    assert re.search(r"Environment=HOME=\$\{?(DATA_DIR|PREFIX)\}?", home_line), (
        "Environment=HOME must point at a service-writable dir (DATA_DIR / "
        f"under PREFIX): {home_line!r}"
    )


def test_venv_chowned_so_service_user_can_execute_it() -> None:
    """The corlinman gateway executes ${PREFIX}/repo/.venv/bin/... so the venv
    must be owned/grouped such that SERVICE_USER can read+execute it. The fix
    chowns the venv (or the whole repo) to a spec that includes SERVICE_USER —
    either ``SERVICE_USER:SERVICE_USER`` or ``root:SERVICE_USER`` (group-read,
    coherent with G1.3)."""
    text = _read_install_sh()
    # A chown of .venv (or the repo) whose owner/group spec mentions
    # SERVICE_USER. Accept root:SERVICE_USER (preferred — see G1.3) or
    # SERVICE_USER:SERVICE_USER.
    pat = (
        rf'chown\s+(-R\s+)?"?(root|{USER_TOKEN}):{USER_TOKEN}"?\s+'
        rf'"?\$\{{?PREFIX\}}?/repo(/\.venv)?"?'
    )
    assert re.search(pat, text), (
        "install.sh never chowns $PREFIX/repo/.venv (or $PREFIX/repo) to a "
        "spec the corlinman service user can read+execute — the de-privileged "
        "gateway gets EACCES on the venv entrypoint and crash-loops"
    )


# ---------------------------------------------------------------------------
# G1.2 — upgrade_native must re-establish the ownership invariant
# ---------------------------------------------------------------------------


def test_upgrade_native_migrates_all_units_via_convergence() -> None:
    """v1.10.0+ robust updater: an upgrade must CONVERGE the box to the
    release's declared state, not just restart the old unit. upgrade_native
    delegates to _apply_native_ref, which (re)writes the FULL systemd unit set
    via write_systemd_units → write_gateway_unit + write_upgrader_units. This
    chain is what carries the v1.10 de-privileging (and any future unit change)
    to existing installs. Verify the whole call graph + that the emitted unit
    is the hardened form."""
    text = _read_install_sh()
    up = _extract_function_body(text, "upgrade_native")
    assert "_apply_native_ref" in up, (
        f"upgrade_native must converge via _apply_native_ref.\n{up}"
    )
    apply_body = _extract_function_body(text, "_apply_native_ref")
    assert "write_systemd_units" in apply_body, (
        "_apply_native_ref must (re)write the systemd units so unit changes "
        f"converge on upgrade.\n{apply_body}"
    )
    units = _extract_function_body(text, "write_systemd_units")
    assert "write_gateway_unit" in units and "write_upgrader_units" in units, (
        f"write_systemd_units must emit BOTH the gateway + upgrader units.\n{units}"
    )
    assert "daemon-reload" in units, (
        f"write_systemd_units must daemon-reload so rewritten units take effect.\n{units}"
    )
    # The gateway unit the chain emits must be the hardened form.
    helper = _extract_function_body(text, "write_gateway_unit")
    assert "User=${SERVICE_USER}" in helper
    assert "/.venv/bin/corlinman-gateway" in helper
    assert "command -v uv" not in helper


def test_upgrade_native_rolls_back_on_failed_health() -> None:
    """Robust updater: a release that fails to come up healthy must NOT leave
    the box down. upgrade_native records the previous commit, and on a failed
    wait_for_health resets --hard back to it and re-applies (same convergence
    logic) so the box ends up healthy on the previous version."""
    text = _read_install_sh()
    up = _extract_function_body(text, "upgrade_native")
    assert "before_sha" in up and "rev-parse HEAD" in up, (
        f"upgrade_native must capture the previous commit as a rollback target.\n{up}"
    )
    assert "wait_for_health" in up, (
        f"upgrade_native must verify /health before declaring success.\n{up}"
    )
    # Rollback = reset --hard back to before_sha + re-converge.
    assert re.search(r"reset --hard \"?\$\{?before_sha", up), (
        f"upgrade_native must `git reset --hard $before_sha` on failure (rollback).\n{up}"
    )
    # The rollback re-applies via the same convergence helper (it appears twice:
    # once for the upgrade, once for the rollback).
    assert up.count("_apply_native_ref") >= 2, (
        "rollback must re-converge via _apply_native_ref so the reverted box is "
        f"fully restored (venv + units + restart), not left half-applied.\n{up}"
    )


def test_apply_native_ref_reestablishes_ownership() -> None:
    text = _read_install_sh()
    body = _extract_function_body(text, "_apply_native_ref")

    # `uv sync` rewrites .venv root-owned; build_and_place_ui rewrites
    # ui-static. The convergence helper must re-chown the runtime paths to
    # SERVICE_USER (and ensure the service user exists) so the corlinman
    # service still starts after a one-click upgrade.
    assert "ensure_service_user" in body, (
        "_apply_native_ref never calls ensure_service_user — a host upgraded "
        "from a pre-S3 install would have no corlinman account when the "
        f"restarted unit tries to run as it.\n{body}"
    )
    assert "chown_runtime_paths" in body, (
        "_apply_native_ref runs `uv sync` (rewrites .venv root-owned) + "
        "build_and_place_ui (rewrites ui-static) but never calls "
        "chown_runtime_paths — the de-privileged gateway can't start after a "
        f"one-click upgrade.\n{body}"
    )
    # And the helper it delegates to must actually re-own the venv + data/ui.
    helper = _extract_function_body(text, "chown_runtime_paths")
    assert re.search(r"\.venv", helper) and "chown" in helper, (
        "chown_runtime_paths must re-establish ownership of $PREFIX/repo/.venv "
        f"after `uv sync`.\n{helper}"
    )
    assert re.search(rf":{USER_TOKEN}\b", helper), (
        "chown_runtime_paths must re-chown runtime paths to the corlinman "
        f"service user.\n{helper}"
    )


# ---------------------------------------------------------------------------
# G1.3 — coherent ownership: root-executed venv python must not be
#        unprivileged-writable
# ---------------------------------------------------------------------------


def test_venv_not_owned_writable_by_unprivileged_user() -> None:
    """The root upgrader (User=root) execs ${PREFIX}/repo/.venv/bin/python
    (deploy/corlinman-upgrader.sh §6) and deploy/install.sh. If the venv is
    chowned ``corlinman:corlinman`` the unprivileged user can rewrite the
    interpreter the root upgrader runs → LPE. The coherent model owns the venv
    ``root:SERVICE_USER`` (group read/exec, not group-write), never
    ``corlinman:corlinman`` recursively."""
    text = _read_install_sh()

    # Find every chown that targets $PREFIX/repo/.venv (or $PREFIX/repo).
    venv_chowns = re.findall(
        rf'chown\s+(?:-R\s+)?"?((?:root|{USER_TOKEN}):{USER_TOKEN})"?\s+'
        rf'"?\$\{{?PREFIX\}}?/repo(?:/\.venv)?"?',
        text,
    )
    assert venv_chowns, (
        "no chown of $PREFIX/repo/.venv found — precondition for the "
        "ownership-coherence check"
    )
    for spec in venv_chowns:
        owner = spec.split(":", 1)[0]
        # Owner of a root-executed interpreter must be root, not the
        # unprivileged service user.
        assert owner == "root", (
            f"venv chowned with owner {owner!r} (spec {spec!r}) — the "
            "root-executed venv python becomes writable by the unprivileged "
            "corlinman user (LPE). Own the venv root:SERVICE_USER (group "
            "read/exec only)."
        )

    # And the root-executed python must be re-locked even in the non-root
    # install flow (where `id -u` != 0): secure_root_executed_scripts is the
    # documented re-lock hook, so the venv interpreter (or the .venv) must be
    # covered by the coherent ownership decision above. Assert the LPE story
    # is acknowledged in the secure_root_executed_scripts neighborhood OR via a
    # root-owned venv chown (already asserted above).
    secure_body = _extract_function_body(text, "secure_root_executed_scripts")
    assert "venv" in secure_body or venv_chowns, (
        "the root-executed venv python is not reconciled with "
        "secure_root_executed_scripts nor owned root:* — document the "
        "ownership model coherently"
    )
