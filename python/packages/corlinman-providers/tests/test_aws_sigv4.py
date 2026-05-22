"""AWS SigV4 signer correctness tests.

Two layers of verification:

1. **Known AWS test vector** — the signing-key derivation is checked against
   the exact byte sequence AWS publishes in the "Deriving the signing key"
   worked example (secret ``wJalrX…``, date ``20120215``, region
   ``us-east-1``, service ``iam``). This pins the four-stage HMAC chain.

2. **End-to-end reconstruction** — :func:`sigv4_headers` is run with a
   frozen timestamp and the canonical request / string-to-sign / signature
   are independently recomputed inside the test from the SigV4 spec; the
   header the signer emits must match. This catches canonicalisation bugs
   (header sort, payload hash, scope assembly) without depending on a live
   AWS endpoint.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac

from corlinman_providers._aws_sigv4 import AwsCredentials, sigv4_headers

# AWS-published worked example for the signing-key derivation.
_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_AWS_SIGNING_KEY_HEX = (
    "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"
)


def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _h(("AWS4" + secret).encode(), date)
    k_region = _h(k_date, region)
    k_service = _h(k_region, service)
    return _h(k_service, "aws4_request")


def test_signing_key_matches_aws_published_vector() -> None:
    """The HMAC chain reproduces AWS's documented signing key bytes."""
    key = _signing_key(_AWS_SECRET, "20120215", "us-east-1", "iam")
    assert key.hex() == _AWS_SIGNING_KEY_HEX


def test_sigv4_headers_reproduces_canonical_signature() -> None:
    """Independently recompute the SigV4 signature and match the signer."""
    creds = AwsCredentials(
        access_key_id="AKIDEXAMPLE",
        secret_access_key=_AWS_SECRET,
    )
    now = dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.UTC)
    body = b'{"hello":"world"}'
    host = "bedrock-runtime.us-east-1.amazonaws.com"
    path = "/model/anthropic.claude-3-haiku-20240307-v1:0/invoke-with-response-stream"

    headers = sigv4_headers(
        credentials=creds,
        method="POST",
        service="bedrock",
        region="us-east-1",
        host=host,
        path=path,
        body=body,
        extra_headers={"content-type": "application/json"},
        now=now,
    )

    # ---- independently rebuild the expected signature -------------------
    amz_date = "20150830T123600Z"
    date_stamp = "20150830"
    payload_hash = hashlib.sha256(body).hexdigest()

    signed_headers_map = {
        "content-type": "application/json",
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    sorted_keys = sorted(signed_headers_map)
    canonical_headers = "".join(f"{k}:{signed_headers_map[k]}\n" for k in sorted_keys)
    signed_headers = ";".join(sorted_keys)
    # The path contains ``:`` which SigV4 percent-encodes (%3A).
    canonical_uri = path.replace(":", "%3A")
    canonical_request = "\n".join(
        ["POST", canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/us-east-1/bedrock/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = _signing_key(_AWS_SECRET, date_stamp, "us-east-1", "bedrock")
    expected_sig = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    assert headers["x-amz-date"] == amz_date
    assert headers["x-amz-content-sha256"] == payload_hash
    assert f"Signature={expected_sig}" in headers["authorization"]
    assert f"Credential=AKIDEXAMPLE/{scope}" in headers["authorization"]
    assert f"SignedHeaders={signed_headers}" in headers["authorization"]


def test_sigv4_includes_session_token_when_present() -> None:
    """An STS session token is added to the signed headers and emitted."""
    creds = AwsCredentials(
        access_key_id="AKIDEXAMPLE",
        secret_access_key=_AWS_SECRET,
        session_token="FwoGZXIvYXdzEXAMPLETOKEN==",
    )
    headers = sigv4_headers(
        credentials=creds,
        method="POST",
        service="bedrock",
        region="us-east-1",
        host="bedrock-runtime.us-east-1.amazonaws.com",
        path="/model/m/invoke-with-response-stream",
        body=b"{}",
        extra_headers={"content-type": "application/json"},
        now=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
    )
    assert headers["x-amz-security-token"] == "FwoGZXIvYXdzEXAMPLETOKEN=="
    # The token must be part of the SignedHeaders list — otherwise AWS 403s.
    assert "x-amz-security-token" in headers["authorization"]


def test_sigv4_is_deterministic_for_fixed_timestamp() -> None:
    """Same inputs + same timestamp → identical signature (no nonce)."""
    creds = AwsCredentials("AKID", _AWS_SECRET)
    kw = {
        "credentials": creds,
        "method": "POST",
        "service": "bedrock",
        "region": "eu-west-1",
        "host": "h",
        "path": "/p",
        "body": b"abc",
        "now": dt.datetime(2024, 5, 1, 9, 0, 0, tzinfo=dt.UTC),
    }
    assert sigv4_headers(**kw)["authorization"] == sigv4_headers(**kw)["authorization"]
