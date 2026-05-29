# corlinman · ARCH_DEBT.md

> Issues that are architectural / cross-service / require product decision.
> Not fixed inside the perpetual loop. Surfaced here for user review.

- **R3-003 follow-up (operator action):** reserve the abandoned `ymylive` namespace on github.com and ghcr.io to prevent a third party from re-registering it and serving malicious install scripts / container images from URLs still referenced in external docs. Procedure: `audit/evidence/round-3/R3-003/SQUAT_RESERVE.md`.
