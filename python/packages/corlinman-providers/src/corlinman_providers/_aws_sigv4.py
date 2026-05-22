"""Minimal AWS Signature Version 4 request signer.

The repo's hard rule for the Bedrock adapter (parcel P8) is *no boto3 /
aioboto3* — we already depend on :mod:`httpx`, and pulling a multi-megabyte
AWS SDK in just to sign one request type is not worth it. SigV4 is a small,
well-specified algorithm; this module implements exactly the slice Bedrock
needs (a signed POST with a body, no STS / no session-token plumbing beyond
forwarding ``X-Amz-Security-Token`` when one is supplied).

Reference: AWS "Signature Version 4 signing process" docs. The four steps:

1. **Canonical request** — HTTP method, URI-encoded path, canonical query
   string, canonical (sorted, lowercased) headers, signed-header list, and
   the hex SHA-256 of the payload.
2. **String to sign** — ``AWS4-HMAC-SHA256`` + ISO8601 timestamp +
   credential scope (``<date>/<region>/<service>/aws4_request``) + the hex
   SHA-256 of the canonical request.
3. **Signing key** — a chained HMAC-SHA256: ``HMAC(HMAC(HMAC(HMAC("AWS4" +
   secret, date), region), service), "aws4_request")``.
4. **Signature + Authorization header** — ``HMAC(signing_key,
   string_to_sign)`` rendered hex, assembled into the ``Authorization``
   header value.

The signer is deliberately pure / synchronous: it takes the request
ingredients and returns the headers to send. The caller (the Bedrock
adapter) owns the actual httpx I/O.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import quote

__all__ = ["AwsCredentials", "sigv4_headers"]

_ALGORITHM = "AWS4-HMAC-SHA256"
_UNSIGNED = "UNSIGNED-PAYLOAD"


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    """An AWS credential triple. ``session_token`` is optional (STS / SSO)."""

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key via the four-stage HMAC chain."""
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _uri_encode(value: str, *, is_path: bool) -> str:
    """URI-encode per the SigV4 rules.

    ``~`` is unreserved and must stay literal; ``/`` is preserved for path
    segments but encoded inside query values. Everything else outside the
    unreserved set is percent-encoded with uppercase hex (``quote`` already
    uppercases).
    """
    safe = "/~" if is_path else "~"
    return quote(value, safe=safe)


def sigv4_headers(
    *,
    credentials: AwsCredentials,
    method: str,
    service: str,
    region: str,
    host: str,
    path: str,
    body: bytes,
    extra_headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    now: _dt.datetime | None = None,
    sign_payload: bool = True,
) -> dict[str, str]:
    """Return the full set of headers (including ``Authorization``) to send.

    Parameters
    ----------
    credentials:
        The AWS access key / secret (+ optional session token).
    method:
        HTTP verb, e.g. ``"POST"``.
    service:
        AWS service code — ``"bedrock"`` for ``bedrock-runtime``.
    region:
        AWS region, e.g. ``"us-east-1"``.
    host:
        The request host (also the value of the signed ``Host`` header).
    path:
        The absolute request path (already containing the model id).
    body:
        The raw request body bytes (hashed into the canonical request).
    extra_headers:
        Additional headers to include *and sign* (e.g. ``Content-Type``).
    query:
        Query parameters to fold into the canonical query string.
    now:
        Override the signing timestamp — only used by tests so a known
        AWS test vector reproduces a fixed signature.
    sign_payload:
        When ``False`` the literal ``UNSIGNED-PAYLOAD`` is used in place of
        the body hash (required for streaming uploads; Bedrock invoke
        bodies are small and fully buffered, so we sign them).

    Returns
    -------
    A dict of headers ready to hand to ``httpx`` — ``Host``, ``X-Amz-Date``,
    the body-hash header, any ``extra_headers``, an ``X-Amz-Security-Token``
    when a session token is present, and the assembled ``Authorization``.
    """
    now = now or _dt.datetime.now(_dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = _sha256_hex(body) if sign_payload else _UNSIGNED

    # ---- assemble the headers that get signed -----------------------------
    signed: dict[str, str] = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    for k, v in (extra_headers or {}).items():
        signed[k.lower()] = v
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token

    # ---- step 1: canonical request ---------------------------------------
    canonical_uri = _uri_encode(path, is_path=True)
    canonical_querystring = "&".join(
        f"{_uri_encode(k, is_path=False)}={_uri_encode(v, is_path=False)}"
        for k, v in sorted((query or {}).items())
    )
    sorted_header_keys = sorted(signed)
    canonical_headers = "".join(
        f"{k}:{signed[k].strip()}\n" for k in sorted_header_keys
    )
    signed_headers = ";".join(sorted_header_keys)
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    # ---- step 2: string to sign ------------------------------------------
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )

    # ---- step 3 + 4: signing key, signature, Authorization ---------------
    key = _signing_key(credentials.secret_access_key, date_stamp, region, service)
    signature = hmac.new(
        key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"{_ALGORITHM} "
        f"Credential={credentials.access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    # The returned dict keeps the canonical (lowercase) header names; httpx
    # transmits them case-insensitively and AWS accepts either case.
    out = dict(signed)
    out["authorization"] = authorization
    return out
