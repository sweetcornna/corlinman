#!/usr/bin/env python3
"""validate-index.py — independent integrity check of the registry.

Loads ``index.json`` and, for every item that declares a ``tarball`` +
``sha256``, re-reads the dist bytes and confirms the on-disk sha256 matches
the declared value. For ``mcp`` items it confirms the referenced manifest
exists and parses as JSON. Exits non-zero on the first failure.

Deliberately standalone (does not import ``build-registry``) so it is a true
second opinion on the bytes the gateway's ``GitHubSource`` would download.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    index = json.loads((ROOT / "index.json").read_text("utf-8"))
    if index.get("schema") != 1:
        print(f"unexpected schema: {index.get('schema')!r}", file=sys.stderr)
        return 1
    items = index.get("items") or []
    if not items:
        print("index has no items", file=sys.stderr)
        return 1

    ok = True
    for row in items:
        kind = row.get("kind")
        slug = row.get("slug")
        if kind in ("skill", "plugin"):
            tarball = str(row.get("tarball") or "")
            declared = str(row.get("sha256") or "").lower().removeprefix("sha256:")
            path = ROOT / tarball
            if not path.exists():
                print(f"FAIL {kind}:{slug} missing tarball {tarball}", file=sys.stderr)
                ok = False
                continue
            actual = _sha256(path.read_bytes())
            if actual != declared:
                print(
                    f"FAIL {kind}:{slug} sha256 mismatch "
                    f"declared={declared} actual={actual}",
                    file=sys.stderr,
                )
                ok = False
            else:
                print(f"OK {kind}:{slug} {tarball} sha256={actual}")
        elif kind == "mcp":
            manifest = str(row.get("manifest") or "")
            path = ROOT / manifest
            if not path.exists():
                print(f"FAIL mcp:{slug} missing manifest {manifest}", file=sys.stderr)
                ok = False
                continue
            try:
                json.loads(path.read_text("utf-8"))
            except ValueError as exc:
                print(f"FAIL mcp:{slug} manifest not JSON: {exc}", file=sys.stderr)
                ok = False
                continue
            print(f"OK mcp:{slug} {manifest} (valid JSON)")
        else:
            print(f"FAIL unknown kind: {kind!r}", file=sys.stderr)
            ok = False

    if ok:
        print("VALIDATOR PASSED: all sha256 + manifests verified")
        return 0
    print("VALIDATOR FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
