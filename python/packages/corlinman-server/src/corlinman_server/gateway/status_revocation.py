"""Per-session **revocation epoch** store for agent status-card share tokens.

A status token (see :mod:`corlinman_server.gateway.status_token`) is a signed,
stateless capability — there is no DB row to delete, so once minted it stays
valid until it expires. Issue #34 adds a tiny escape hatch: an operator can
*revoke* every outstanding link for a single conversation by bumping that
session's **epoch**.

The epoch is folded into the signed token body at mint time and re-checked at
verify time: a token whose epoch is *behind* the session's current epoch is
rejected. Revoking is therefore just "increment the epoch" — every link minted
under the old epoch instantly stops verifying, while a freshly-minted link
carries the new epoch and keeps working.

Storage is deliberately minimal and dependency-free: a single JSON file
``<data_dir>/status_epochs.json`` mapping ``session_key -> int epoch``. It is
best-effort in the same spirit as
:func:`corlinman_server.gateway.status_token.resolve_signing_key` — any OS /
parse error is swallowed and treated as **epoch 0** (the backward-compatible
default), so a missing / unreadable / corrupt file never breaks verification,
it just means "nothing revoked yet". Absent epoch == 0 is exactly the value a
legacy (pre-#34) token carries, which keeps old links verifying until the
session is explicitly revoked.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

__all__ = ["current_epoch", "revoke_session"]

#: Filename of the per-session epoch map under the data dir.
_EPOCHS_FILENAME: str = "status_epochs.json"


def _epochs_path(data_dir: Path | None) -> Path | None:
    if data_dir is None:
        return None
    return Path(data_dir) / _EPOCHS_FILENAME


def _read_all(data_dir: Path | None) -> dict[str, int]:
    """Read the whole epoch map. Returns ``{}`` on any error / missing file.

    Never raises — a malformed or unreadable file is indistinguishable from
    "nothing revoked", so we degrade to the empty (all-zero) map.
    """
    path = _epochs_path(data_dir)
    if path is None:
        return {}
    try:
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        # Tolerate stray non-int values (corruption / hand-edits): coerce
        # what we can, skip the rest. Anything unparseable -> treated as 0
        # by virtue of being absent from the result.
        if not isinstance(key, str):
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def current_epoch(data_dir: Path | None, session_key: str) -> int:
    """Return the stored revocation epoch for ``session_key``, else ``0``.

    ``0`` is returned for every backward-compatible condition: ``data_dir``
    is ``None``, no epochs file exists, the session has no entry, or the file
    is unreadable / malformed. Never raises.
    """
    if not session_key:
        return 0
    epoch = _read_all(data_dir).get(session_key, 0)
    # Defensive clamp: a corrupt negative value would otherwise let an old
    # token sneak past the ``token_epoch < current_epoch`` gate.
    return epoch if epoch > 0 else 0


def revoke_session(data_dir: Path | None, session_key: str) -> int:
    """Increment ``session_key``'s epoch (invalidating its outstanding links).

    Returns the new epoch. Atomic write via ``tempfile`` + :func:`os.replace`
    so a concurrent reader never observes a half-written file. No-op returning
    ``0`` when ``data_dir`` is ``None`` (nowhere to persist) or ``session_key``
    is empty.
    """
    path = _epochs_path(data_dir)
    if path is None or not session_key:
        return 0

    epochs = _read_all(data_dir)
    new_epoch = epochs.get(session_key, 0)
    new_epoch = (new_epoch if new_epoch > 0 else 0) + 1
    epochs[session_key] = new_epoch

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same dir, then atomically replace so a
        # crash mid-write can't truncate the live map.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{_EPOCHS_FILENAME}.", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(epochs, fh, ensure_ascii=False, sort_keys=True)
            os.replace(tmp_name, path)
        except OSError:
            # Best-effort cleanup of the orphaned temp file.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        # Unwritable data dir — the revoke didn't persist. Return the epoch
        # we computed so an in-memory caller still sees the bump, but on the
        # next process the stored value stays 0 (best-effort, like the
        # signing-key resolver).
        return new_epoch
    return new_epoch
