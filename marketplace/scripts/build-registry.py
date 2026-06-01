#!/usr/bin/env python3
"""build-registry.py — pack the corlinman marketplace registry deterministically.

Walks the source trees (``skills/``, ``plugins/``, ``mcp/``), packs each
skill/plugin into a reproducible gzip tarball under ``dist/``, computes the
sha256 of every tarball, and regenerates ``index.json`` so the declared
hashes match the dist bytes exactly.

Reproducibility rules (re-running yields byte-identical tarballs):

* members are walked in sorted order;
* every ``TarInfo`` is normalized — ``mtime=0``, ``uid=gid=0``,
  ``uname=gname=""``, modes pinned to 0644 (files) / 0755 (dirs);
* the gzip wrapper is written with ``mtime=0`` so the gzip header carries no
  timestamp.

The tarball wraps the source directory as ``<slug>/...`` so an extractor that
expects ``<slug>/SKILL.md`` or ``<slug>/manifest.json`` is satisfied.

The ``generated_at`` timestamp in ``index.json`` is intentionally *not* part
of the tarball bytes, so the tarballs (and their sha256s) stay stable across
runs even though the index carries a fresh wall-clock time. Pass
``--check`` to verify the tarballs reproduce + every declared sha256 matches
without rewriting wall-clock-sensitive fields.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_FILE_MODE = 0o644
_DIR_MODE = 0o755


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sorted_files(base: Path) -> list[Path]:
    """All files under ``base``, sorted by POSIX relative path."""
    return sorted(
        (p for p in base.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(base).as_posix(),
    )


def _build_tarball(src_dir: Path, slug: str) -> bytes:
    """Pack ``src_dir`` as ``<slug>/...`` into a reproducible gzip tarball."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in _sorted_files(src_dir):
            rel = path.relative_to(src_dir).as_posix()
            arcname = f"{slug}/{rel}"
            data = path.read_bytes()
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mtime = 0
            info.mode = _FILE_MODE
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    # gzip-wrap with mtime=0 so the gzip header carries no timestamp.
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0) as fh:
        fh.write(raw.getvalue())
    return gz.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _discover_skills() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    base = ROOT / "skills"
    for skill_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        slug = skill_dir.name
        name, description, emoji = _read_skill_frontmatter(skill_dir / "SKILL.md")
        version = "1.0.0"
        out.append(
            {
                "kind": "skill",
                "slug": slug,
                "name": name or slug,
                "description": description,
                "emoji": emoji,
                "latest_version": version,
                "versions": [version],
                "src": skill_dir,
                "tarball": f"dist/skills/{slug}-{version}.tar.gz",
            }
        )
    return out


def _discover_plugins() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    base = ROOT / "plugins"
    for plugin_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        slug = plugin_dir.name
        manifest = json.loads((plugin_dir / "manifest.json").read_text("utf-8"))
        version = str(manifest.get("version") or "0.1.0")
        out.append(
            {
                "kind": "plugin",
                "slug": slug,
                "name": str(manifest.get("name") or slug),
                "description": str(manifest.get("description") or ""),
                "latest_version": version,
                "versions": [version],
                "src": plugin_dir,
                "tarball": f"dist/plugins/{slug}-{version}.tar.gz",
            }
        )
    return out


def _discover_mcp() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    base = ROOT / "mcp"
    for mcp_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        slug = mcp_dir.name
        rel = f"mcp/{slug}/manifest.json"
        manifest = json.loads((mcp_dir / "manifest.json").read_text("utf-8"))
        requires = manifest.get("requires") or {}
        env = requires.get("env") if isinstance(requires, dict) else []
        out.append(
            {
                "kind": "mcp",
                "slug": slug,
                "name": str(manifest.get("name") or slug),
                "description": str(manifest.get("description") or ""),
                "transport": str(manifest.get("transport") or "stdio"),
                "latest_version": "1.0.0",
                "versions": ["1.0.0"],
                "manifest": rel,
                "requires": {"env": list(env or [])},
            }
        )
    return out


def _read_skill_frontmatter(skill_md: Path) -> tuple[str, str, str | None]:
    """Parse ``name`` / ``description`` / ``emoji`` from YAML frontmatter.

    Deliberately dependency-free (no PyYAML): the frontmatter here is flat
    ``key: value`` pairs with optionally quoted values.
    """
    text = skill_md.read_text("utf-8")
    if not text.startswith("---"):
        return "", "", None
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else ""
    name = description = ""
    emoji: str | None = None
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        key = key.strip()
        if key == "name":
            name = value
        elif key == "description":
            description = value
        elif key == "emoji":
            emoji = value or None
    return name, description, emoji


def _write_tarballs(
    items: Iterable[dict[str, object]], *, write: bool
) -> dict[str, str]:
    """Pack every skill/plugin item; return ``{tarball_rel: sha256}``.

    When ``write`` is False the bytes are computed (to verify reproducibility
    and hashes) but not flushed to disk.
    """
    hashes: dict[str, str] = {}
    for item in items:
        if item["kind"] not in ("skill", "plugin"):
            continue
        src = item["src"]  # type: ignore[assignment]
        rel = str(item["tarball"])
        data = _build_tarball(src, str(item["slug"]))  # type: ignore[arg-type]
        hashes[rel] = _sha256(data)
        dest = ROOT / rel
        if write:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    return hashes


def _build_index(
    items: list[dict[str, object]], hashes: dict[str, str], *, generated_at: str
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    updated_at = generated_at
    for item in sorted(items, key=lambda i: (str(i["kind"]), str(i["slug"]))):
        kind = item["kind"]
        if kind == "skill":
            row: dict[str, object] = {
                "kind": "skill",
                "slug": item["slug"],
                "name": item["name"],
                "description": item["description"],
                "latest_version": item["latest_version"],
                "versions": item["versions"],
                "tarball": item["tarball"],
                "sha256": hashes[str(item["tarball"])],
                "updated_at": updated_at,
            }
            if item.get("emoji"):
                row["emoji"] = item["emoji"]
            rows.append(row)
        elif kind == "plugin":
            rows.append(
                {
                    "kind": "plugin",
                    "slug": item["slug"],
                    "name": item["name"],
                    "description": item["description"],
                    "latest_version": item["latest_version"],
                    "versions": item["versions"],
                    "tarball": item["tarball"],
                    "sha256": hashes[str(item["tarball"])],
                    "updated_at": updated_at,
                }
            )
        elif kind == "mcp":
            rows.append(
                {
                    "kind": "mcp",
                    "slug": item["slug"],
                    "name": item["name"],
                    "description": item["description"],
                    "transport": item["transport"],
                    "latest_version": item["latest_version"],
                    "versions": item["versions"],
                    "manifest": item["manifest"],
                    "requires": item["requires"],
                    "updated_at": updated_at,
                }
            )
    return {"schema": 1, "generated_at": generated_at, "items": rows}


def _collect() -> list[dict[str, object]]:
    return _discover_skills() + _discover_plugins() + _discover_mcp()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify tarballs reproduce + declared sha256s match; do not write.",
    )
    args = parser.parse_args(argv)

    items = _collect()

    if args.check:
        hashes = _write_tarballs(items, write=False)
        index_path = ROOT / "index.json"
        if not index_path.exists():
            print("index.json missing; run without --check first", file=sys.stderr)
            return 1
        declared = json.loads(index_path.read_text("utf-8"))
        ok = True
        for row in declared.get("items", []):
            tarball = row.get("tarball")
            if not tarball:
                continue
            want = str(row.get("sha256") or "")
            got = hashes.get(tarball)
            on_disk = (ROOT / tarball).read_bytes()
            disk_sha = _sha256(on_disk)
            if got != want or disk_sha != want:
                ok = False
                print(
                    f"MISMATCH {tarball}: declared={want} rebuilt={got} disk={disk_sha}",
                    file=sys.stderr,
                )
            else:
                print(f"OK {tarball} sha256={want}")
        return 0 if ok else 1

    hashes = _write_tarballs(items, write=True)
    index = _build_index(items, hashes, generated_at=_now_iso())
    (ROOT / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for tarball, digest in sorted(hashes.items()):
        print(f"packed {tarball} sha256={digest}")
    print(f"wrote {ROOT / 'index.json'} ({len(index['items'])} items)")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
