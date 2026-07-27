"""Error type shared by every voice backend.

Backends raise :class:`SynthesisError` with a machine-readable ``code``;
the tool dispatcher folds it into the standard
``{"ok": false, "error": <code>, "message": ...}`` envelope and the admin
preview route turns it into an HTTP problem response. Keeping the code
separate from the message means the UI can special-case a few known
conditions (missing credentials, gateway attestation) without string
matching.
"""

from __future__ import annotations

__all__ = ["SynthesisError"]


class SynthesisError(RuntimeError):
    """A recoverable text-to-speech failure.

    Parameters
    ----------
    code
        Machine-readable slug, e.g. ``tts_unavailable``,
        ``tts_http_status``, ``live_attestation_unavailable``.
    message
        Human-readable detail, safe to surface in the UI.
    status_code
        Upstream HTTP status when the failure came from a provider call.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
