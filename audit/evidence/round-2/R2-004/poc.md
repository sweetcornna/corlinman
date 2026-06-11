# R2-004 — WeChat Official XML Parser Hardening

**Severity**: High (CVSS 7.5 — network-reachable DoS on signature-validated
endpoint; XXE / SSRF potential)
**Component**: `python/packages/corlinman-channels/src/corlinman_channels/wechat_official.py`
**Function**: `parse_wechat_xml(body: bytes) -> dict[str, str]` (lines 204–217 pre-fix)
**Class**: CWE-776 (Improper Restriction of XML External Entity Reference) /
CWE-409 (Improper Handling of Highly Compressed Data — billion-laughs)

## Root cause

The WeChat Official webhook handler called the stdlib parser:

```python
from xml.etree import ElementTree as ET
...
root = ET.fromstring(body)
```

CPython's `xml.etree.ElementTree` is documented vulnerable to entity-expansion
DoS in the official security docs
(<https://docs.python.org/3/library/xml.html#xml-vulnerabilities>). Even though
WeChat's webhook is guarded by a SHA-1 signature, the signing token is a
low-entropy shared secret operators paste into the developer console — once it
leaks (logged stack trace, screenshot, partner outage), an attacker can mint a
valid `signature` for any timestamp+nonce they choose and submit an XML bomb
that the receiver expands into multi-GB string concat — single-process DoS,
trivial to script.

## Proof-of-concept payload

Signature verification passes for any attacker who knows or guesses the
shared `token`; the dangerous part is the body. The minimal exponential
billion-laughs payload (~1 kB on the wire, expands to ~10⁹ characters):

```xml
<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<xml><Content>&lol4;</Content></xml>
```

Real attacks scale this to 6–9 nesting levels for trillions of expansions.
A bonus XXE variant turns the webhook into an SSRF probe:

```xml
<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<xml><Content>&xxe;</Content></xml>
```

## Before / after behaviour

| Payload | `xml.etree` (before) | `defusedxml` (after) |
|---------|----------------------|----------------------|
| Billion-laughs | Silently expands → OOM / runaway CPU. `pytest.raises(ValueError)` fails with `DID NOT RAISE` (see `before.log`). | Raises `defusedxml.EntitiesForbidden` → caught and re-raised as `ValueError` → webhook returns empty 200 (existing `except ValueError` path). `after.log` shows GREEN. |
| XXE / SYSTEM entity | stdlib happens to drop external entities, so this passed already, but only by accident — no policy enforcement. | Explicit `DTDForbidden` / `EntitiesForbidden` raises immediately. |
| Plain malformed `<xml><Content>oops` | Raises `ParseError` (existing behaviour preserved). | Raises `ParseError` (still preserved — re-raised as `ValueError`). |
| Real WeChat envelope (text / image / voice / event) | Parses correctly. | Parses correctly — `defusedxml` is a behavioural drop-in for well-formed flat documents. |

## Fix

Swapped only the parse call site:

```python
from defusedxml.ElementTree import fromstring as _xml_fromstring
...
root = _xml_fromstring(body)
```

`ET.tostring` (serialisation, attacker-uncontrolled) and `ET.ParseError`
(exception type) remain on the stdlib `xml.etree` import — no need to pull
those through defusedxml. The `except` clause widens to
`(ET.ParseError, ValueError)` because defusedxml's hardening exceptions
inherit from `ValueError` (`EntitiesForbidden`, `DTDForbidden`,
`ExternalReferenceForbidden`). The webhook caller already handles
`ValueError` as "return empty 200, drop the request", so end-to-end the
attacker just sees an empty ack instead of a wedged process.

## Dependency justification (per spec §"deps allowed when severity ≥ high")

- **`defusedxml>=0.7.1`**: Author Christian Heimes (also a CPython core dev);
  the library is the recommendation in the CPython security docs itself.
  Pure-Python, zero transitive deps, BSD-licensed, last release stable for
  years (active maintenance, no abandonment). Added to the
  `corlinman-channels` package's `pyproject.toml` only — not the root
  workspace.

## Hard rules honoured

- Did NOT touch `verify_signature` or any webhook auth path.
- Did NOT widen the parser's accepted inputs — only narrowed them.
- Dep declared in the channel package's pyproject (not the workspace root).
- Single, minimal call-site swap; no behavioural change for well-formed
  WeChat traffic.

## Evidence files

- `before.log` — failing `pytest.raises` for the bomb payload on stdlib ET.
- `after.log` — full `TestParseXml` class passes (10/10) after the swap.
- `regression.log` — full `corlinman-channels` package suite (`-m "not
  live_llm and not live_transport"`): 661 passed, 0 failed.
- `poc.md` — this file.
