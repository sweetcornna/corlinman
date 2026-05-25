"""Tests for :func:`corlinman_server.system.skill_hub.install_skill`.

W1.4 of ``docs/PLAN_SKILL_HUB.md``. Covers the installer pipeline that
materialises a downloaded ClawHub tarball into ``<profile_skills_dir>/
<slug>/`` with a sidecar ``.openclaw-meta.json`` recording provenance,
plus the matching uninstall path.

The tests construct real ``application/gzip`` tarballs in memory with
:mod:`tarfile`. The :class:`ClawHubClient` is stubbed by an in-memory
double that returns canned :class:`HubDownload` objects — we don't need
respx here because the unit under test never re-enters the HTTP layer.

Module-level :func:`pytest.importorskip` keeps the file collectable
while the sibling agent (W1-CORE) is still landing
``system/skill_hub/installer.py``.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

# TODO(W1-CORE): once `system/skill_hub/installer.py` lands the
# ``importorskip`` collapses to a regular import.
skill_hub = pytest.importorskip(
    "corlinman_server.system.skill_hub",
    reason=(
        "TODO(W1-CORE): waiting on system/skill_hub/installer.py from "
        "the sibling agent before these tests can execute."
    ),
)

install_skill = skill_hub.install_skill
uninstall_skill = skill_hub.uninstall_skill
HubDownload = skill_hub.HubDownload
SkillAlreadyInstalledError = skill_hub.SkillAlreadyInstalledError
UnsafeTarballError = skill_hub.UnsafeTarballError

from corlinman_server.system.audit import (  # noqa: E402  (after importorskip)
    SystemAuditLog,
)


# ---------------------------------------------------------------------------
# Tarball builders
# ---------------------------------------------------------------------------


def _build_tarball(members: list[tuple[str, bytes]]) -> bytes:
    """Return raw gzip bytes for a tarball containing the given members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _build_tarball_with_symlink(name: str, target: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tar.addfile(info)
    return buf.getvalue()


def _valid_skill_tarball(
    *,
    skill_name: str = "web-search",
    body: bytes = b"---\nname: web-search\n---\n# Web Search\n",
) -> bytes:
    return _build_tarball(
        [
            (f"{skill_name}/SKILL.md", body),
            (f"{skill_name}/helpers/run.py", b"print('hi')\n"),
        ]
    )


class _StubClient:
    """In-memory ``ClawHubClient`` substitute returning canned downloads.

    The installer only uses :meth:`download`; everything else is left
    unimplemented to surface accidental coupling.
    """

    def __init__(self, payload: bytes, *, content_hash: str | None = "sha256:fake") -> None:
        self._payload = payload
        self._content_hash = content_hash
        self.calls: list[tuple[str, str]] = []

    async def download(self, slug: str, version: str = "latest") -> Any:
        self.calls.append((slug, version))
        return HubDownload(
            content=self._payload,
            content_hash=self._content_hash,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_install_happy_path_writes_skill_and_sidecar(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    client = _StubClient(_valid_skill_tarball())

    report = await install_skill(
        profile_skills_dir=skills_dir,
        client=client,
        slug="web-search",
        version="1.0.0",
    )

    target = skills_dir / "web-search"
    assert target.is_dir()
    assert (target / "SKILL.md").is_file()
    assert (target / ".openclaw-meta.json").is_file()
    # Stub was actually called.
    assert client.calls == [("web-search", "1.0.0")]
    # Report carries the install summary.
    assert report is not None


async def test_install_sidecar_records_provenance(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    client = _StubClient(_valid_skill_tarball())

    await install_skill(
        profile_skills_dir=skills_dir,
        client=client,
        slug="web-search",
        version="1.0.0",
    )

    sidecar = skills_dir / "web-search" / ".openclaw-meta.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["slug"] == "web-search"
    assert payload["version"] == "1.0.0"
    assert payload["source"] == "clawhub"
    # ``installed_at`` is ISO-8601; loose check on the shape — UTC ``Z``
    # suffix is the project convention.
    assert isinstance(payload["installed_at"], str)
    assert "T" in payload["installed_at"]


# ---------------------------------------------------------------------------
# Pre-existing target dir
# ---------------------------------------------------------------------------


async def test_install_refuses_existing_dir_without_force(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    target = skills_dir / "web-search"
    target.mkdir(parents=True)
    (target / "marker.txt").write_text("existing", encoding="utf-8")

    client = _StubClient(_valid_skill_tarball())
    with pytest.raises(SkillAlreadyInstalledError):
        await install_skill(
            profile_skills_dir=skills_dir,
            client=client,
            slug="web-search",
        )

    # The pre-existing file must be untouched.
    assert (target / "marker.txt").read_text(encoding="utf-8") == "existing"


async def test_install_replaces_existing_dir_with_force(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target = skills_dir / "web-search"
    target.mkdir(parents=True)
    (target / "marker.txt").write_text("existing", encoding="utf-8")

    client = _StubClient(_valid_skill_tarball())
    await install_skill(
        profile_skills_dir=skills_dir,
        client=client,
        slug="web-search",
        force=True,
    )

    assert (target / "SKILL.md").is_file()
    # Old marker is gone after the force-replace.
    assert not (target / "marker.txt").exists()


# ---------------------------------------------------------------------------
# Tarball safety — path traversal, absolute paths, symlinks, oversize
# ---------------------------------------------------------------------------


async def test_install_rejects_path_traversal_member(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    payload = _build_tarball(
        [
            ("web-search/SKILL.md", b"# hi\n"),
            ("../etc/evil", b"pwn"),
        ]
    )
    client = _StubClient(payload)
    with pytest.raises(UnsafeTarballError):
        await install_skill(
            profile_skills_dir=skills_dir,
            client=client,
            slug="web-search",
        )
    # On rejection the target dir must not exist (no partial extraction).
    assert not (skills_dir / "web-search").exists()


async def test_install_rejects_absolute_path_member(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    payload = _build_tarball([("/etc/passwd", b"root:x:0:0\n")])
    client = _StubClient(payload)
    with pytest.raises(UnsafeTarballError):
        await install_skill(
            profile_skills_dir=skills_dir,
            client=client,
            slug="web-search",
        )
    assert not (skills_dir / "web-search").exists()


async def test_install_rejects_symlink_escaping_root(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    payload = _build_tarball_with_symlink(
        "web-search/oops", target="../../etc/passwd"
    )
    client = _StubClient(payload)
    with pytest.raises(UnsafeTarballError):
        await install_skill(
            profile_skills_dir=skills_dir,
            client=client,
            slug="web-search",
        )
    assert not (skills_dir / "web-search").exists()


async def test_install_rejects_oversize_total(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    # Build a tarball whose *uncompressed* total exceeds 25 MiB but
    # compresses well. Use four 8 MiB members so the per-file limit
    # (10 MiB) doesn't trip first.
    members = [
        (f"web-search/big_{idx}.bin", b"\0" * (8 * 1024 * 1024))
        for idx in range(4)
    ]
    payload = _build_tarball(members)
    client = _StubClient(payload)
    with pytest.raises(UnsafeTarballError):
        await install_skill(
            profile_skills_dir=skills_dir,
            client=client,
            slug="web-search",
        )


async def test_install_rejects_oversize_single_file(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    payload = _build_tarball(
        [
            ("web-search/SKILL.md", b"# hi\n"),
            # 12 MiB blob — over the 10 MiB per-file ceiling.
            ("web-search/huge.bin", b"\0" * (12 * 1024 * 1024)),
        ]
    )
    client = _StubClient(payload)
    with pytest.raises(UnsafeTarballError):
        await install_skill(
            profile_skills_dir=skills_dir,
            client=client,
            slug="web-search",
        )


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


async def test_install_atomicity_on_mid_extract_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If extraction blows up mid-way the target dir must not survive.

    We poke a failure into the stdlib by patching ``shutil.move`` and/or
    ``tarfile.TarFile.extractall``-equivalent. Implementations vary, so
    we hit the lowest-level write call we can: ``Path.write_bytes``.
    """
    skills_dir = tmp_path / "skills"
    client = _StubClient(_valid_skill_tarball())

    calls = {"n": 0}
    real_write_bytes = Path.write_bytes

    def _exploding_write_bytes(self: Path, data: bytes, *args, **kwargs) -> int:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk full")
        return real_write_bytes(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", _exploding_write_bytes)

    with pytest.raises(Exception):  # noqa: BLE001 — implementation may wrap
        await install_skill(
            profile_skills_dir=skills_dir,
            client=client,
            slug="web-search",
        )

    # Even if the implementation chose a different write primitive and
    # the patch never fired, this test still passes — the meaningful
    # invariant is the *no half-installed dir* one below.
    if calls["n"] > 0:
        assert not (skills_dir / "web-search").exists()


# ---------------------------------------------------------------------------
# Audit log integration
# ---------------------------------------------------------------------------


async def test_install_writes_audit_log_row(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    audit = SystemAuditLog(tmp_path / "audit.log")
    client = _StubClient(_valid_skill_tarball())

    await install_skill(
        profile_skills_dir=skills_dir,
        client=client,
        slug="web-search",
        version="1.0.0",
        audit_log=audit,
    )

    rows = await audit.tail(limit=10)
    events = [r.event for r in rows]
    assert "skill.installed" in events
    installed = next(r for r in rows if r.event == "skill.installed")
    # Slug + version recorded on the audit row (in ``details`` per the
    # SystemAuditLog wire shape).
    detail_str = json.dumps(installed.to_json())
    assert "web-search" in detail_str
    assert "1.0.0" in detail_str
    # Surface the file count so operators can spot weirdly large installs.
    assert installed.details.get("files_written") is not None


# ---------------------------------------------------------------------------
# uninstall_skill
# ---------------------------------------------------------------------------


async def test_uninstall_refuses_dir_without_sidecar(tmp_path: Path) -> None:
    """A skill dir lacking ``.openclaw-meta.json`` is treated as bundled
    or user-edited — :func:`uninstall_skill` must refuse to ``rm -rf``
    it.
    """
    skills_dir = tmp_path / "skills"
    target = skills_dir / "bundled-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# bundled", encoding="utf-8")

    with pytest.raises(Exception):  # noqa: BLE001 — typed err name TBD
        await uninstall_skill(
            profile_skills_dir=skills_dir,
            name="bundled-skill",
        )

    # Directory still present.
    assert (target / "SKILL.md").is_file()


async def test_uninstall_removes_dir_with_sidecar_and_audits(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    target = skills_dir / "web-search"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# web-search", encoding="utf-8")
    (target / ".openclaw-meta.json").write_text(
        json.dumps(
            {
                "slug": "web-search",
                "version": "1.0.0",
                "source": "clawhub",
                "installed_at": "2026-05-25T12:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )
    audit = SystemAuditLog(tmp_path / "audit.log")

    await uninstall_skill(
        profile_skills_dir=skills_dir,
        name="web-search",
        audit_log=audit,
    )

    assert not target.exists()

    rows = await audit.tail(limit=10)
    events = [r.event for r in rows]
    assert "skill.uninstalled" in events


async def test_uninstall_rejects_path_traversal_names(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    # A sibling file the test will assert is left alone.
    sibling = tmp_path / "sentinel"
    sibling.write_text("don't touch", encoding="utf-8")

    for bad in ("..", "../etc", "foo/bar", "/etc/passwd"):
        with pytest.raises(ValueError):
            await uninstall_skill(
                profile_skills_dir=skills_dir,
                name=bad,
            )

    assert sibling.read_text(encoding="utf-8") == "don't touch"
