# corlinman-content-policy

Deterministic, dependency-leaf content policy shared by Corlinman's Tencent-facing QQ, QQ Official, and QZone transports.

The package performs local high-confidence text classification, fails closed when enabled, and denies unclassified media by default. It never records source text; callers should log only the sanitized audit metadata returned by `PolicyDecision.audit_fields()`.
