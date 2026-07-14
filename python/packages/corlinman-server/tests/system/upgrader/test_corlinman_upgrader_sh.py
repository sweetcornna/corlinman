"""Subprocess tests for ``deploy/corlinman-upgrader.sh`` (W1.2).

We invoke the script with a temp ``CORLINMAN_DATA_DIR`` + a stub
``install.sh`` that just exits 0 (or non-zero, depending on the test).
``UPGRADER_SKIP_TAG_CHECK=1`` skips the live GitHub-releases curl so the
tests are hermetic.

Coverage:

* Happy path: valid request + stub install.sh exit 0 → status =
  ``succeeded``, request file gone, processed file gone.
* Malformed tag (``v1.2.0; rm -rf /``) → status = ``failed`` with
  ``error=tag_invalid`` BEFORE install.sh is called.
* install.sh non-zero exit → status = ``failed`` with
  ``error=install_sh_exit_<code>``, log_excerpt populated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
UPGRADER_SH = REPO_ROOT / "deploy" / "corlinman-upgrader.sh"


# Auto-skip the whole module when jq / bash are unavailable on the test
# host (e.g. a stripped-down CI sandbox). The script hard-requires jq.
pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="jq + bash required for corlinman-upgrader.sh subprocess tests",
)


def _make_install_sh(prefix_root: Path, *, exit_code: int = 0) -> Path:
    """Create a stub ``$INSTALL_PREFIX/repo/deploy/install.sh``.

    The stub echoes the args it was called with (so log_excerpt has
    something we can grep) and exits with the requested code.
    """
    install_sh = prefix_root / "repo" / "deploy" / "install.sh"
    install_sh.parent.mkdir(parents=True, exist_ok=True)
    install_sh.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"stub install.sh called with: $*\"\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    install_sh.chmod(0o755)
    return install_sh


def _write_request(
    data_dir: Path,
    *,
    tag: str,
    request_id: str | None = None,
    allow_downgrade: bool | None = None,
) -> str:
    rid = request_id or str(uuid.uuid4())
    payload = {
        "request_id": rid,
        "tag": tag,
        "requested_at": 1234567890123,
        "requested_by": "test-suite",
        "mode": "native",
    }
    if allow_downgrade is not None:
        payload["allow_downgrade"] = allow_downgrade
    (data_dir / ".upgrade-request").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return rid


def _make_fake_venv_python(prefix_root: Path, version: str) -> None:
    """Fake ``$INSTALL_PREFIX/repo/.venv/bin/python`` reporting ``version``.

    The upgrader pipes a heredoc into it; the stub ignores stdin and
    prints the version — enough for the CURRENT_VERSION probe.
    """
    python_bin = prefix_root / "repo" / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text(
        f"#!/usr/bin/env bash\ncat >/dev/null\necho '{version}'\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o755)


def _write_health_file(tmp_path: Path, version: str) -> str:
    """A ``file://`` health endpoint for the version-assertion step."""
    health = tmp_path / "health.json"
    health.write_text(
        json.dumps({"status": "ok", "version": version}), encoding="utf-8"
    )
    return f"file://{health}"


def _run_upgrader(
    data_dir: Path,
    install_prefix: Path,
    *,
    skip_tag_check: bool = True,
    extra_env: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CORLINMAN_DATA_DIR"] = str(data_dir)
    env["INSTALL_PREFIX"] = str(install_prefix)
    # Bypass the live GitHub release whitelist call — we don't want
    # the test suite to depend on a network round-trip.
    if skip_tag_check:
        env["UPGRADER_SKIP_TAG_CHECK"] = "1"
    # Default to allowing downgrades for tests so the stub install.sh
    # (no .venv → no version detection) doesn't trip the gate.
    env.setdefault("UPGRADER_ALLOW_DOWNGRADE", "1")
    # Hermetic default: no gateway is listening in tests, so skip the
    # post-upgrade /health version assertion unless a test opts in by
    # pre-setting UPGRADER_HEALTH_URL (file:// works) in extra_env.
    env.setdefault("UPGRADER_SKIP_VERSION_ASSERT", "1")
    env["UPGRADER_LOG_FILE"] = str(data_dir / "upgrader.log")
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(UPGRADER_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_happy_path_writes_succeeded(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    install_prefix = tmp_path / "fake-install"
    data_dir.mkdir()
    _make_install_sh(install_prefix, exit_code=0)

    request_id = _write_request(data_dir, tag="v1.2.1")
    result = _run_upgrader(data_dir, install_prefix)

    assert result.returncode == 0, (
        f"upgrader exited non-zero\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    status_path = data_dir / ".upgrade-status"
    assert status_path.exists()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["state"] == "succeeded"
    assert payload["request_id"] == request_id
    assert payload["error"] is None
    assert payload["finished_at"] is not None
    # log_excerpt should include the stub install.sh's echo.
    assert "stub install.sh called" in (payload.get("log_excerpt") or "")
    # Success branch cleans up both the request and processed markers.
    assert not (data_dir / ".upgrade-request").exists()
    assert not (data_dir / ".upgrade-request.processed").exists()


def test_unprefixed_tag_is_canonicalized_to_release_form(
    tmp_path: Path,
) -> None:
    """Gateways < v1.20.1 wrote the update checker's stripped display
    tag ("1.2.1") into the request file. The script must accept it and
    pass the canonical release form ("v1.2.1") to install.sh."""
    data_dir = tmp_path / "data"
    install_prefix = tmp_path / "fake-install"
    data_dir.mkdir()
    _make_install_sh(install_prefix, exit_code=0)

    request_id = _write_request(data_dir, tag="1.2.1")
    result = _run_upgrader(data_dir, install_prefix)

    assert result.returncode == 0, (
        f"upgrader exited non-zero\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    payload = json.loads(
        (data_dir / ".upgrade-status").read_text(encoding="utf-8")
    )
    assert payload["state"] == "succeeded"
    assert payload["request_id"] == request_id
    # The stub echoes its argv — assert install.sh saw the v-prefixed tag.
    assert "--version v1.2.1" in (payload.get("log_excerpt") or "")


def test_malformed_tag_aborts_before_install_sh(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    install_prefix = tmp_path / "fake-install"
    data_dir.mkdir()
    install_sh = _make_install_sh(install_prefix, exit_code=0)

    # Sentinel: if install.sh runs, it'll write this canary file. Test
    # asserts the canary is NOT present after the run.
    canary = data_dir / "canary"
    install_sh.write_text(
        "#!/usr/bin/env bash\n"
        f'touch "{canary}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    install_sh.chmod(0o755)

    bad_tag = "v1.2.0; rm -rf /"
    request_id = _write_request(data_dir, tag=bad_tag)
    result = _run_upgrader(data_dir, install_prefix)

    # Script must exit non-zero on tag-invalid (it calls fail()).
    assert result.returncode != 0
    assert not canary.exists(), "install.sh was called despite malformed tag"

    status_path = data_dir / ".upgrade-status"
    assert status_path.exists()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert payload["error"] == "tag_invalid"
    assert payload["request_id"] == request_id
    # Failure branch moves the bad request aside so the path unit
    # doesn't loop on it.
    assert not (data_dir / ".upgrade-request").exists()
    assert (data_dir / ".upgrade-request.processed").exists()


def test_install_sh_failure_propagates_to_status(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    install_prefix = tmp_path / "fake-install"
    data_dir.mkdir()
    _make_install_sh(install_prefix, exit_code=42)

    request_id = _write_request(data_dir, tag="v1.2.1")
    result = _run_upgrader(data_dir, install_prefix)

    assert result.returncode != 0, (
        f"unexpected exit\nstdout: {result.stdout}\nstderr: {result.stderr}\n"
        f"data_dir: {list(data_dir.iterdir())}"
    )
    status_path = data_dir / ".upgrade-status"
    assert status_path.exists(), (
        f"status file missing\nstdout: {result.stdout}\nstderr: {result.stderr}\n"
        f"data_dir: {list(data_dir.iterdir())}"
    )
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert payload["error"] == "install_sh_exit_42"
    assert payload["request_id"] == request_id
    assert payload["log_excerpt"] is not None


if __name__ == "__main__":  # pragma: no cover
    # Convenience: `python -m tests...` runs them.
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Version assertion (7.5) + request-level allow_downgrade
# ---------------------------------------------------------------------------


def test_version_assertion_pass_marks_verified(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    install_prefix = tmp_path / "fake-install"
    _make_install_sh(install_prefix, exit_code=0)
    health_url = _write_health_file(tmp_path, "1.2.1")

    request_id = _write_request(data_dir, tag="v1.2.1")
    result = _run_upgrader(
        data_dir,
        install_prefix,
        extra_env={
            "UPGRADER_SKIP_VERSION_ASSERT": None,  # opt back in
            "UPGRADER_HEALTH_URL": health_url,
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (data_dir / ".upgrade-status").read_text(encoding="utf-8")
    )
    assert payload["state"] == "succeeded"
    assert payload["request_id"] == request_id
    assert payload["version_verified"] is True


def test_version_assertion_mismatch_rolls_back(tmp_path: Path) -> None:
    """install.sh exits 0 but /health reports the WRONG version → the
    helper must fail the request AND roll back to the pre-upgrade
    version (explicit downgrade install)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    install_prefix = tmp_path / "fake-install"
    _make_install_sh(install_prefix, exit_code=0)
    _make_fake_venv_python(install_prefix, "1.0.0")  # pre-upgrade version
    health_url = _write_health_file(tmp_path, "1.0.0")  # stuck on old

    request_id = _write_request(data_dir, tag="v1.2.1")
    result = _run_upgrader(
        data_dir,
        install_prefix,
        extra_env={
            "UPGRADER_SKIP_VERSION_ASSERT": None,
            "UPGRADER_HEALTH_URL": health_url,
        },
    )

    assert result.returncode != 0
    payload = json.loads(
        (data_dir / ".upgrade-status").read_text(encoding="utf-8")
    )
    assert payload["state"] == "failed"
    assert payload["error"] == "version_assertion_failed"
    assert payload["version_verified"] is False
    assert payload["rolled_back"] is True
    # The rollback re-invoked install.sh with the pre-upgrade tag.
    log = (data_dir / "upgrader.log").read_text(encoding="utf-8")
    assert "--version v1.0.0" in log
    assert payload["request_id"] == request_id
    # Failure parks the request out of the watched path.
    assert not (data_dir / ".upgrade-request").exists()
    assert (data_dir / ".upgrade-request.processed").exists()


def test_missing_health_version_field_skips_assertion(tmp_path: Path) -> None:
    """Older gateways expose no `version` on /health — the assertion must
    self-skip (unknown), never fail the upgrade."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    install_prefix = tmp_path / "fake-install"
    _make_install_sh(install_prefix, exit_code=0)
    health = tmp_path / "health.json"
    health.write_text(json.dumps({"status": "ok"}), encoding="utf-8")

    _write_request(data_dir, tag="v1.2.1")
    result = _run_upgrader(
        data_dir,
        install_prefix,
        extra_env={
            "UPGRADER_SKIP_VERSION_ASSERT": None,
            "UPGRADER_HEALTH_URL": f"file://{health}",
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (data_dir / ".upgrade-status").read_text(encoding="utf-8")
    )
    assert payload["state"] == "succeeded"
    assert "version_verified" not in payload  # tri-state: unknown


def test_request_level_allow_downgrade_relaxes_gate(tmp_path: Path) -> None:
    """A rollback request carries allow_downgrade=true; the helper must
    honour it without the UPGRADER_ALLOW_DOWNGRADE env override."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    install_prefix = tmp_path / "fake-install"
    _make_install_sh(install_prefix, exit_code=0)
    _make_fake_venv_python(install_prefix, "9.9.9")  # current > target

    _write_request(data_dir, tag="v1.2.1", allow_downgrade=True)
    result = _run_upgrader(
        data_dir,
        install_prefix,
        extra_env={"UPGRADER_ALLOW_DOWNGRADE": None},  # no env override
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (data_dir / ".upgrade-status").read_text(encoding="utf-8")
    )
    assert payload["state"] == "succeeded"


def test_downgrade_refused_without_allow_flag(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    install_prefix = tmp_path / "fake-install"
    _make_install_sh(install_prefix, exit_code=0)
    _make_fake_venv_python(install_prefix, "9.9.9")

    request_id = _write_request(data_dir, tag="v1.2.1")
    result = _run_upgrader(
        data_dir,
        install_prefix,
        extra_env={"UPGRADER_ALLOW_DOWNGRADE": None},
    )

    assert result.returncode != 0
    payload = json.loads(
        (data_dir / ".upgrade-status").read_text(encoding="utf-8")
    )
    assert payload["state"] == "failed"
    assert payload["error"] == "downgrade_refused"
    assert payload["request_id"] == request_id
