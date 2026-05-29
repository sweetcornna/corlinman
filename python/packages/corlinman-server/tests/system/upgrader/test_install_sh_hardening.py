"""Static-analysis tests for ``deploy/install.sh`` security hardening (S3).

These cover two CONFIRMED deploy security defects that cannot be exercised
on a live VM in CI, so we assert on the rendered systemd unit text and the
chown/chmod ownership logic that ``install.sh`` *emits*:

S3a (HIGH) — the generated ``corlinman.service`` gateway unit had no
``User=`` / ``Group=``, so the internet-facing gateway
(``Environment=BIND=0.0.0.0``) ran as ROOT. The Docker path already drops
to an unprivileged ``corlinman`` user (docker/Dockerfile). The native path
must do the same: create a dedicated unprivileged system user/group and add
``User=corlinman`` / ``Group=corlinman`` to the gateway ``[Service]`` block,
plus chown the runtime-writable ``DATA_DIR`` to it.

S3b (HIGH) — LPE via writable root-executed upgrade scripts. The two
``sudo chown -R "$(id -u):$(id -g)" "$PREFIX"`` calls made
``$PREFIX/repo/deploy/corlinman-upgrader.sh`` and ``$PREFIX/repo/deploy/
install.sh`` owned + writable by the *unprivileged* install user, yet
``corlinman-upgrader.service`` runs them as ``User=root``. The unprivileged
user could therefore rewrite a root-executed script → root code exec. After
the recursive chown the root-executed scripts must be re-chowned
``root:root`` and made non-group/other-writable.

Full verification still requires a native install on a real Linux host (the
main agent notes this); these tests pin the script *source* so the
regression can't silently come back.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[6]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"

SERVICE_USER = "corlinman"

# The install script uses a ``SERVICE_USER`` shell variable (set to
# "corlinman") as the single source of truth, so rendered commands/units may
# carry ``$SERVICE_USER`` / ``${SERVICE_USER}`` rather than the literal. Match
# either form.
USER_TOKEN = rf"(?:{SERVICE_USER}|\$\{{?SERVICE_USER\}}?)"


def _read_install_sh() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _extract_heredoc(text: str, anchor: str) -> str:
    """Return the body of the first ``tee ... <<EOF ... EOF`` heredoc whose
    body contains ``anchor`` (a unit-identifying substring)."""
    # Heredocs in install.sh are all ``<<EOF`` ... a line that is exactly EOF.
    bodies = re.findall(r"<<EOF\n(.*?)\nEOF\n", text, flags=re.DOTALL)
    for body in bodies:
        if anchor in body:
            return body
    raise AssertionError(f"no heredoc body containing {anchor!r} found")


# ---------------------------------------------------------------------------
# S3a — gateway unit must drop privileges to an unprivileged service user
# ---------------------------------------------------------------------------


def test_gateway_unit_runs_as_unprivileged_user() -> None:
    text = _read_install_sh()
    # The gateway unit is the heredoc carrying the gateway ExecStart.
    gateway_unit = _extract_heredoc(text, "corlinman-gateway --config")

    # Sanity: this is indeed the internet-facing gateway unit.
    assert "BIND=0.0.0.0" in gateway_unit, (
        "expected to be inspecting the gateway unit (BIND=0.0.0.0)"
    )

    assert re.search(rf"^User={USER_TOKEN}$", gateway_unit, re.MULTILINE), (
        "gateway [Service] unit has no 'User=corlinman' line — the "
        "internet-facing gateway (BIND=0.0.0.0) runs as ROOT.\n"
        f"--- rendered unit body ---\n{gateway_unit}"
    )
    assert re.search(rf"^Group={USER_TOKEN}$", gateway_unit, re.MULTILINE), (
        "gateway [Service] unit has no 'Group=corlinman' line.\n"
        f"--- rendered unit body ---\n{gateway_unit}"
    )


def test_service_user_variable_is_corlinman() -> None:
    """The single-source-of-truth SERVICE_USER var must resolve to the same
    unprivileged account the Docker image uses ('corlinman')."""
    text = _read_install_sh()
    assert re.search(rf'^SERVICE_USER="{SERVICE_USER}"', text, re.MULTILINE), (
        "SERVICE_USER is not defined as 'corlinman' (must match the Docker "
        "image's unprivileged user)"
    )


def test_creates_unprivileged_system_user_guarded() -> None:
    text = _read_install_sh()
    # Collapse bash line-continuations so a multi-line `useradd ... \` command
    # reads as one logical line.
    joined = text.replace("\\\n", " ")
    # A guarded useradd: only create the account if it doesn't already exist.
    assert "useradd" in joined, (
        "install.sh never creates the unprivileged service user"
    )
    # The useradd must be system + no-login + no home so it's not a usable
    # interactive account (matches docker/Dockerfile semantics). The account
    # name is carried via $SERVICE_USER.
    useradd_cmds = [
        ln
        for ln in joined.splitlines()
        if "useradd" in ln and re.search(USER_TOKEN, ln)
    ]
    assert useradd_cmds, (
        "no useradd command targeting the service user ($SERVICE_USER)"
    )
    for cmd in useradd_cmds:
        assert "--system" in cmd, (
            f"service user must be a --system account: {cmd!r}"
        )
        assert "nologin" in cmd or "--shell" in cmd, (
            f"service user must have a nologin shell: {cmd!r}"
        )
    # Guarded by an existence check (idempotent re-runs must not fail).
    assert re.search(
        rf"(getent\s+passwd\s+\"?{USER_TOKEN}|id\s+(-u\s+)?\"?{USER_TOKEN})",
        joined,
    ), "useradd is not guarded by an existence check (getent/id)"


def test_data_dir_chowned_to_service_user() -> None:
    text = _read_install_sh()
    # After creating the user the runtime-writable DATA_DIR must be handed
    # to it, else the de-privileged gateway can't read/write its own data.
    # Accept the chown spec either bare or wrapped in one set of quotes:
    #   chown -R corlinman:corlinman "$DATA_DIR"
    #   chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
    assert re.search(
        rf'chown\s+(-R\s+)?"?{USER_TOKEN}:{USER_TOKEN}"?\s+"?\$\{{?DATA_DIR\}}?"?',
        text,
    ), (
        "DATA_DIR is never chowned to the corlinman service user — the "
        "de-privileged gateway would fail to read/write its data dir"
    )


# ---------------------------------------------------------------------------
# S3b — root-executed upgrade scripts must be root-owned + non-writable
# ---------------------------------------------------------------------------


def test_recursive_chown_present_for_runtime_files() -> None:
    """The broad ``chown -R <install-user> $PREFIX`` is what creates the
    LPE window. Keep asserting it exists so the S3b mitigation below has
    something to clamp down on (and we notice if the layout changes)."""
    text = _read_install_sh()
    assert re.search(r'chown\s+-R\s+"?\$\(id -u\):\$\(id -g\)"?\s+"\$PREFIX"', text), (
        "expected the recursive 'chown -R $(id -u):$(id -g) $PREFIX' "
        "(the LPE window S3b mitigates)"
    )


def _extract_function_body(text: str, name: str) -> str:
    """Return the body of a top-level bash function ``name() { ... }``.

    install.sh defines helpers at column 0 (``name() {``) and closes them
    with a ``}`` at column 0, so a brace-depth scan from the opening line
    is unnecessary — match to the next line that is exactly ``}``.
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


def test_root_executed_scripts_reowned_to_root() -> None:
    text = _read_install_sh()

    # The two scripts that corlinman-upgrader.service runs as User=root:
    #   * deploy/corlinman-upgrader.sh (ExecStart)
    #   * deploy/install.sh (invoked by the upgrader as root)
    # Both live under $PREFIX/repo/deploy and after the recursive chown end
    # up owned/writable by the unprivileged install user. The hardening
    # helper must re-chown both root:root and strip group/other write.
    body = _extract_function_body(text, "secure_root_executed_scripts")

    assert "corlinman-upgrader.sh" in body, (
        "secure_root_executed_scripts does not target corlinman-upgrader.sh "
        f"(LPE: upgrader script left install-user-writable).\n{body}"
    )
    assert "deploy/install.sh" in body, (
        "secure_root_executed_scripts does not target deploy/install.sh — "
        "the upgrader invokes it as root, so it must also be root-owned.\n"
        f"{body}"
    )
    assert re.search(r"chown\s+root:root", body), (
        "secure_root_executed_scripts must chown the root-executed scripts "
        f"to root:root.\n{body}"
    )

    # A chmod that strips group/other write (0755 / go-w / a-w).
    chmod_modes = re.findall(r"chmod\s+(0?[0-7]{3,4})\b", body)
    assert chmod_modes, (
        "no chmod tightening the root-executed scripts found — they must "
        f"be non-group/other-writable (e.g. chmod 0755).\n{body}"
    )
    for mode in chmod_modes:
        m = (mode.lstrip("0") or "0").zfill(3)
        # group + other write bits are the 2 in each of the last two octal
        # digits; both must be clear.
        assert not (int(m[-2]) & 0o2), f"group-writable mode {mode!r}"
        assert not (int(m[-1]) & 0o2), f"other-writable mode {mode!r}"


def test_root_reown_happens_after_recursive_chown_in_both_paths() -> None:
    """Ordering matters: the lock-down of the upgrade scripts must run
    *after* the broad ``chown -R ... $PREFIX`` in each install path
    (docker + native), otherwise the recursive chown re-grants the
    unprivileged user write and re-opens the LPE window. The fix invokes
    ``secure_root_executed_scripts`` after each recursive chown."""
    text = _read_install_sh()
    # Ignore comment lines so the helper's own docstring (which quotes the
    # recursive chown command) isn't mistaken for a real command.
    lines = [
        ln if not re.match(r"\s*#", ln) else "" for ln in text.splitlines()
    ]

    recursive_idxs = [
        i
        for i, ln in enumerate(lines)
        if re.search(r'chown\s+-R\s+"?\$\(id -u\):\$\(id -g\)"?\s+"\$PREFIX"', ln)
    ]
    # Calls to the lock-down helper (not its own definition).
    call_idxs = [
        i
        for i, ln in enumerate(lines)
        if re.match(r"\s+secure_root_executed_scripts\s*$", ln)
    ]
    assert recursive_idxs, "no recursive chown found (precondition)"
    assert call_idxs, (
        "secure_root_executed_scripts is defined but never called — the "
        "LPE window is never actually closed"
    )

    # Each recursive chown must be followed (before the next recursive chown
    # or EOF) by a call to the lock-down helper.
    boundaries = [*recursive_idxs, len(lines)]
    for k, ri in enumerate(recursive_idxs):
        next_recursive = boundaries[k + 1]
        following = [x for x in call_idxs if ri < x < next_recursive]
        assert following, (
            "a recursive 'chown -R ... $PREFIX' at line "
            f"{ri + 1} is not followed by a secure_root_executed_scripts "
            "call before the next path — LPE window left open there"
        )
