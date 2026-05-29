"""Subprocess-based ``claude auth login`` driver.

Spawns the locally-installed ``claude`` CLI's interactive login flow,
extracts the first ``https://...`` it prints to stdout, and parks the
subprocess waiting on stdin so the operator can paste back the code
they retrieve from the browser.

Why a separate module from :mod:`claude_code_import`:

* Import = read-only adapter for a pre-existing ``~/.claude/.credentials.json``.
  Useful when the operator already signed in on this host.
* Login = drive the CLI from zero — VPS where the credentials file does
  not exist yet, operator wants to bootstrap from the admin UI without
  shelling into the box first.

Lifecycle:

1. ``launch_claude_login()`` spawns ``claude auth login``, reads stdout
   up to 6s or until a URL surfaces, returns ``LaunchedSession``.
2. The CLI now blocks reading stdin ("Paste code here if prompted > ").
3. Operator pastes code in the UI; backend ``submit_code()`` writes the
   code + newline to the subprocess stdin and waits for exit (≤60s).
4. On clean exit the CLI has written ``~/.claude/.credentials.json``;
   :mod:`claude_code_import` then surfaces it as a normal credential.

Sessions are tracked in a process-local dict. The gateway runs as one
process per host (systemd unit), so this is fine; if we ever move to
multi-worker we'll need a shared store.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


# `claude auth login` prints the OAuth URL on the line after
# "If the browser didn't open, visit:" — we accept any https:// URL on
# claude.com / anthropic.com so future CLI variants keep working.
_URL_RE = re.compile(
    r"https://[^\s]+(?:claude\.com|anthropic\.com)[^\s]*",
    re.IGNORECASE,
)

# Hard ceilings — both keep a misbehaving CLI from leaking a process.
_LAUNCH_READ_TIMEOUT_S: float = 8.0
_SUBMIT_WAIT_TIMEOUT_S: float = 60.0
# Sessions abandoned for longer than this are reaped on next access.
_SESSION_MAX_IDLE_S: float = 300.0


@dataclass
class LaunchedSession:
    """A live ``claude auth login`` subprocess plus its parsed URL."""

    session_id: str
    url: str
    proc: asyncio.subprocess.Process
    output_buffer: str
    started_at: float = field(default_factory=time.time)


@dataclass
class LaunchResult:
    """Shape returned by :func:`launch_claude_login`."""

    session_id: str
    url: str


_sessions: dict[str, LaunchedSession] = {}


def _reap_expired() -> None:
    """Drop sessions older than ``_SESSION_MAX_IDLE_S``.

    Best-effort cleanup. The subprocess itself may still be running — we
    kill it so it doesn't sit forever holding stdin open.
    """
    now = time.time()
    expired: list[str] = []
    for sid, sess in _sessions.items():
        if now - sess.started_at > _SESSION_MAX_IDLE_S:
            expired.append(sid)
    for sid in expired:
        sess = _sessions.pop(sid, None)
        if sess is None:
            continue
        if sess.proc.returncode is None:
            try:
                sess.proc.kill()
            except ProcessLookupError:
                pass
        logger.info("claude_login_session_expired", session_id=sid)


async def launch_claude_login() -> LaunchResult:
    """Spawn ``claude auth login`` and return its OAuth URL.

    Raises :class:`ClaudeLoginError` if the binary isn't on PATH or no
    URL appears within :data:`_LAUNCH_READ_TIMEOUT_S` seconds.
    """
    _reap_expired()

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "auth",
            "login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise ClaudeLoginError(
            "claude_cli_not_installed",
            "The `claude` CLI is not installed on this host.",
        ) from exc

    assert proc.stdout is not None
    deadline = time.time() + _LAUNCH_READ_TIMEOUT_S
    chunks: list[str] = []
    url: str | None = None

    while time.time() < deadline:
        remaining = max(0.05, deadline - time.time())
        try:
            line_bytes = await asyncio.wait_for(
                proc.stdout.readline(), timeout=remaining
            )
        except TimeoutError:
            break
        if not line_bytes:
            # EOF — CLI exited before printing a URL. Could be already
            # logged in.
            break
        line = line_bytes.decode("utf-8", errors="replace")
        chunks.append(line)
        m = _URL_RE.search(line)
        if m:
            url = m.group(0)
            break

    if url is None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        logger.warning(
            "claude_login_no_url",
            output_preview="".join(chunks)[:400],
        )
        raise ClaudeLoginError(
            "no_url_emitted",
            "The `claude` CLI did not print an OAuth URL within "
            f"{_LAUNCH_READ_TIMEOUT_S:.0f}s. It may already be logged in "
            "or the CLI version changed its output format.",
        )

    session_id = secrets.token_urlsafe(16)
    _sessions[session_id] = LaunchedSession(
        session_id=session_id,
        url=url,
        proc=proc,
        output_buffer="".join(chunks),
    )
    logger.info(
        "claude_login_session_started",
        session_id=session_id,
        url_host=_url_host(url),
    )
    return LaunchResult(session_id=session_id, url=url)


async def submit_code(session_id: str, code: str) -> None:
    """Push the OAuth code back into the parked subprocess's stdin.

    Returns once the subprocess exits cleanly (or raises
    :class:`ClaudeLoginError` on timeout / nonzero exit).
    """
    sess = _sessions.get(session_id)
    if sess is None:
        raise ClaudeLoginError(
            "unknown_session",
            "No claude login session for that id. It may have expired — "
            "click 登录 again to start over.",
        )

    proc = sess.proc
    if proc.returncode is not None:
        _sessions.pop(session_id, None)
        raise ClaudeLoginError(
            "subprocess_exited",
            "The `claude auth login` subprocess has already exited "
            f"(code {proc.returncode}). Re-launch to retry.",
        )

    assert proc.stdin is not None
    code = code.strip()
    if not code:
        raise ClaudeLoginError("empty_code", "Paste the code from the browser.")

    try:
        proc.stdin.write((code + "\n").encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError) as exc:
        _sessions.pop(session_id, None)
        raise ClaudeLoginError(
            "write_failed",
            "Could not write the code back to the CLI (it may have "
            "already exited). Re-launch to retry.",
        ) from exc

    try:
        await asyncio.wait_for(proc.wait(), timeout=_SUBMIT_WAIT_TIMEOUT_S)
    except TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        _sessions.pop(session_id, None)
        raise ClaudeLoginError(
            "submit_timeout",
            "The CLI did not finish within "
            f"{_SUBMIT_WAIT_TIMEOUT_S:.0f}s of receiving the code.",
        ) from exc

    # Drain any remaining stdout so we log full transcript on failure.
    tail = b""
    if proc.stdout is not None:
        try:
            tail = await asyncio.wait_for(proc.stdout.read(), timeout=2.0)
        except TimeoutError:
            tail = b""

    rc = proc.returncode
    _sessions.pop(session_id, None)
    if rc != 0:
        logger.warning(
            "claude_login_subprocess_failed",
            returncode=rc,
            tail=tail.decode("utf-8", errors="replace")[-400:],
        )
        raise ClaudeLoginError(
            "subprocess_nonzero",
            "The `claude auth login` subprocess exited with code "
            f"{rc}. Double-check the pasted code and try again.",
        )

    logger.info("claude_login_completed", session_id=session_id)


def cancel(session_id: str) -> bool:
    """Kill a parked subprocess. Returns True if it was alive."""
    sess = _sessions.pop(session_id, None)
    if sess is None:
        return False
    if sess.proc.returncode is None:
        try:
            sess.proc.kill()
        except ProcessLookupError:
            return False
        return True
    return False


def _url_host(url: str) -> str:
    """Best-effort host extraction for logging (avoids leaking the full URL)."""
    try:
        # cheap split — no urllib import needed
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return "?"


class ClaudeLoginError(Exception):
    """Raised for any predictable failure in the login flow.

    `code` is a stable machine-readable tag, `message` is what the UI
    surfaces to the operator.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
