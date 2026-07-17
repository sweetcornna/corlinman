"""Repo-root pytest hooks.

Leaked-thread backstop: a test that opens an aiosqlite connection (or
any resource owning a non-daemon thread) and never closes it leaves a
thread parked on its work queue forever. Python then blocks in
``Py_Finalize → wait_for_thread_shutdown`` AFTER pytest has printed its
summary — the run looks finished but the process never exits. In CI
that wedged py-test until the 6h job cap ("intermittent hang"); locally
it wedged piped output invisibly.

``pytest_unconfigure`` runs after the terminal summary. If non-daemon
threads are still alive at that point, name them loudly and hard-exit
with the session's real status so the hang becomes an instant, visible
warning instead of a silent multi-hour stall. The leak itself should
still be fixed in the offending test — this is a backstop, not a
license.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

_EXIT_STATUS = 0


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    global _EXIT_STATUS
    _EXIT_STATUS = int(exitstatus)


def pytest_unconfigure(config: Any) -> None:
    leaked = [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread() and not t.daemon and t.is_alive()
    ]
    if not leaked:
        return
    names = ", ".join(sorted(t.name for t in leaked))
    sys.stderr.write(
        f"\n[conftest] {len(leaked)} leaked non-daemon thread(s) would block "
        f"interpreter exit: {names}\n"
        "[conftest] a test opened a connection/resource without closing it; "
        "forcing exit with the session's status.\n"
    )
    sys.stderr.flush()
    sys.stdout.flush()
    os._exit(_EXIT_STATUS)
