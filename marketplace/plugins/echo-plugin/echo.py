"""echo-plugin — a minimal example plugin for the corlinman marketplace.

Reads one line from stdin and writes it back to stdout, prefixed with
``echo: ``. Exists only to give the plugin install path a tiny, known-good
payload to extract and run.
"""

from __future__ import annotations

import sys


def main() -> int:
    line = sys.stdin.readline().rstrip("\n")
    sys.stdout.write(f"echo: {line}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
