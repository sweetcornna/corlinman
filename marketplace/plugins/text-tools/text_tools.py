"""text-tools — a minimal corlinman example plugin (stdin line -> transform)."""
from __future__ import annotations

import sys


def main() -> int:
    line = sys.stdin.readline().rstrip("\n")
    sys.stdout.write(line[::-1] + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
