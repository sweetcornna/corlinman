"""OAuth credential storage + PKCE flow for subscription-based providers.

This package is the corlinman-side port of the most useful bits of
hermes-agent's ``hermes_cli/web_server.py`` OAuth surface (Anthropic PKCE
+ Claude Code CLI auto-import). It lets an operator who already pays for
a Claude Pro / Max / Code subscription consume that quota from corlinman
without minting an API key.

Public surface:

* :class:`OAuthCredential` — frozen dataclass with redaction-aware
  ``__repr__`` so tokens never leak into logs / tracebacks.
* :func:`load_credential` / :func:`save_credential` / :func:`delete_credential`
  — JSON-on-disk persistence under ``<data_dir>/.oauth/<provider>.json``
  with file mode ``0o600``. The ``data_dir`` is always passed in by the
  caller; this module never assumes ``~/.corlinman`` so multi-tenant /
  sandboxed callers can scope per request.

The per-provider PKCE drivers (:mod:`anthropic_pkce`) and the read-only
Claude Code CLI import (:mod:`claude_code_import`) sit alongside this
module. The router that exposes them over HTTP lives at
:mod:`corlinman_server.gateway.routes_admin_b.oauth`.

Logging policy: nothing in this package ever logs ``access_token`` or
``refresh_token`` — even at DEBUG. Operators who need to inspect a token
must read the JSON file directly.
"""

from __future__ import annotations

from corlinman_server.gateway.oauth.storage import (
    OAuthCredential,
    delete_credential,
    load_credential,
    save_credential,
)

__all__ = [
    "OAuthCredential",
    "delete_credential",
    "load_credential",
    "save_credential",
]
