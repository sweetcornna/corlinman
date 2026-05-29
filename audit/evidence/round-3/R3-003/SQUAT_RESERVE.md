# R3-003 — Namespace reservation (operator action required)

The code fix in this commit redirects every install / upgrade / image reference
from the abandoned `ymylive` namespace to the canonical `sweetcornna` namespace.
It does **not** prevent a third party from registering `ymylive` and serving
malicious content from the old documented URLs that are still scattered across
external blog posts, archived branches, container registry pulls cached by
proxies, and operator shell history.

To close the residual attack surface, the human operator (not this tool) must:

## 1. Reserve `ymylive` on github.com

1. Sign in to GitHub with an account you control (e.g. `sweetcornna` or a
   dedicated org owner).
2. Create an organization (or user account placeholder) named exactly
   `ymylive`. GitHub usernames/org names are case-insensitive and globally
   unique on a first-claim basis.
3. Inside the `ymylive` org, create a public empty repository named
   `corlinman` containing only a README that says:
   > This namespace is reserved. The canonical repository moved to
   > <https://github.com/sweetcornna/corlinman>. Do not run install scripts
   > from this URL.
4. Optionally, mark the repo archived so PRs/issues are disabled.

This is the standard squat-prevention pattern recommended in
[OpenSSF SLSA §Source.Reproducible](https://slsa.dev/spec/v1.0/requirements)
and aligns with PyPI / npm advice on abandoned package names.

## 2. Reserve `ymylive/corlinman` on ghcr.io

GHCR namespaces are claimed by the first `docker push` from an authenticated
account that owns the matching GitHub user/org. Once step 1 is done, push a
single dummy image:

```bash
# From the ymylive-owning account
echo "FROM scratch" > /tmp/Dockerfile.deprecated
docker buildx build --platform linux/amd64 -t ghcr.io/ymylive/corlinman:deprecated \
    --push - < /tmp/Dockerfile.deprecated
# Make the package public so `docker pull` does not 401 (which a smart
# attacker would interpret as a still-unclaimed namespace).
gh api -X PATCH /user/packages/container/corlinman/visibility -f visibility=public
```

A scratch image is ~0 bytes on disk and prevents anyone else from pushing under
`ghcr.io/ymylive/corlinman:*`.

## 3. Audit external surface

Search and update (or arrange for redirect/deletion of) any third-party
documentation that still references the `ymylive` URLs — blog posts, gist
mirrors, Docker Hub tutorials, video descriptions. The code fix covers
in-tree docs; out-of-tree content is operator territory.

## 4. Validation

After steps 1-3:

- `curl -fsSL -o /dev/null -w "%{http_code}" https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh`
  should return `200` and the body must be the squat-warning README **or**
  return `404` (repo intentionally has no `deploy/install.sh`).
- `docker manifest inspect ghcr.io/ymylive/corlinman:deprecated` must succeed
  (proves the namespace is claimed).
- A search for `ymylive/corlinman` on `github.com/search` should show only the
  reservation repo, not a clone with a populated `deploy/` directory.

## Why this is out of scope for the perpetual audit loop

The audit tool can only modify files inside this repository. Registering an
external GitHub org and pushing to GHCR requires interactive auth flows
(2FA, account creation) that a non-interactive agent must not perform on
behalf of the operator. Captured here, tracked in `audit/ARCH_DEBT.md`, and
flagged in the commit body so it surfaces in the operator's review queue.
