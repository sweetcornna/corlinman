# R3-003 — Abandoned-namespace supply-chain hijack (PoC)

**Class:** CWE-829 (Inclusion of Functionality from Untrusted Control Sphere) / CWE-494 (Download of Code Without Integrity Check)
**Severity:** Critical (CVSS 8.1 — AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:H/A:H)
**Affected paths (pre-fix):** `deploy/install.sh:{6,9,13,17,92,548}`, `deploy/corlinman-upgrader.sh:9`, `.github/workflows/release-image.yml:45`, `docker/compose/docker-compose.yml:16`, `deploy/AI_DEPLOY.md`, `docs/quickstart.md`, `README.md`, `docs/system-updates.md`, `docs/config.example.toml`, `docs/PLAN_*.md`, `docs/release-notes-v0.1.0.md`, `docs/multi-agent-release-plan.md`, `docs/design/*.md`, `docs/roadmap.md` — 64 total references, all enumerated in `before.log`.

## Attack flow

1. The corlinman repo was transferred `github.com/ymylive/corlinman` → `github.com/sweetcornna/corlinman` (commit `67fc06e`). GitHub serves a 301 redirect from old paths to the new owner *until* the `ymylive` namespace is re-registered by someone else. The `ghcr.io/ymylive/corlinman` image namespace is similarly orphaned and claim-on-first-push.
2. **Attacker registers `ymylive` on github.com** (free; no review). They immediately gain control of `https://raw.githubusercontent.com/ymylive/corlinman/main/*` and `https://github.com/ymylive/corlinman/releases/*`. They can also push to `ghcr.io/ymylive/corlinman:latest` from a workflow under their account because the namespace is unclaimed on GHCR.
3. **Documented install one-liner runs the attacker's payload as root.** Every quickstart / README / AI_DEPLOY entrypoint says `curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh | bash`. After step 2 that 301-redirects no longer fire — the raw URL serves the attacker's malicious tarball. install.sh in `--mode native` runs as root, registers a systemd unit, sets up `corlinman-upgrader.{path,service}` (root-equivalent), and clones from the attacker-controlled repo. In `--mode docker`, `docker pull ghcr.io/ymylive/corlinman:latest` lands the attacker image which then runs as a container with mounted `~/.corlinman` (full data exfil) and, if `--enable-one-click-upgrade` was set, the host docker socket (root-equivalent host RCE).
4. **Blast radius:** every fresh install + every documented upgrade path on every operator host — single-shot, no user interaction beyond the standard one-liner. The corlinman-upgrader.service (root, watches `.upgrade-request`) re-invokes the poisoned install.sh on every UI-triggered upgrade, perpetuating the compromise across versions.

## Why minimal change is sufficient

Rewriting every reference to `sweetcornna/corlinman` removes the dependency on GitHub's redirect being intact and on `ghcr.io/ymylive` staying unclaimed. Combined with the namespace-reservation operator action tracked in `SQUAT_RESERVE.md` + `audit/ARCH_DEBT.md`, the attack surface collapses to zero.
