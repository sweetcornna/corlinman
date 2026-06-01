"""json-pretty — a minimal corlinman example plugin (stdin JSON -> pretty)."""
from __future__ import annotations

import json
import sys


def main() -> int:
    raw = sys.stdin.read()
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        sys.stdout.write(f"invalid JSON: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
