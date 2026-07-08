"""Repo-root pytest hooks — test hermeticity (gap E5) + leaked-thread backstop.

Hermeticity: the suite must produce the same result on a laptop that
exports ``ANTHROPIC_BASE_URL`` / ``HTTP(S)_PROXY`` for daily work as it
does in a clean CI shell. Before this file existed, a leaked
``ANTHROPIC_BASE_URL`` rerouted the real ``anthropic`` SDK away from the
respx mocks in the provider error-mapping tests (Connection error
instead of the mocked status), and a leaked proxy variable pointed httpx
transports at a proxy that doesn't exist inside the sandbox — dozens of
false failures that had to be dodged with ``env -u`` wrappers.

Scope: the scrub is skipped for tests explicitly marked ``live_llm`` /
``live_transport`` — those hit real endpoints on purpose and may need the
operator's routing environment. Tests that want one of these variables set
still work unchanged: their own ``monkeypatch.setenv`` runs after this
autouse fixture.

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

import pytest

#: Environment that must never leak from the invoking shell into a
#: hermetic (non-live) test: provider base-URL overrides and proxy
#: routing. Both spellings of the proxy vars — httpx honours lowercase.
_HERMETIC_SCRUB_VARS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_LIVE_MARKERS: tuple[str, ...] = ("live_llm", "live_transport")


@pytest.fixture(autouse=True)
def _hermetic_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete shell-leaked routing env for every non-live test."""
    for marker in _LIVE_MARKERS:
        if request.node.get_closest_marker(marker) is not None:
            return
    for var in _HERMETIC_SCRUB_VARS:
        monkeypatch.delenv(var, raising=False)


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
