# SEC-204 PoC — unauth RCE via misconfigured co-hosted Agent gRPC bind

## Vulnerable condition

Operator co-hosts the agent in-process **and** points the bind at a
non-loopback address:

```bash
export CORLINMAN_GRPC_AGENT_INPROC=1
export CORLINMAN_PY_ADDR=0.0.0.0:50051        # or any public NIC
```

`corlinman_server.gateway.grpc.agent_server.resolve_agent_bind`
historically returned that value verbatim. `serve_agent` then ran
`server.add_insecure_port(bind)` — no TLS, no auth.

Any attacker on the network could dial the Agent gRPC, drive
`ReasoningLoop`, and trigger the auto-bound `run_shell` / `write_file` /
`apply_patch` tools → unauthenticated remote code execution on the
host process's identity.

## Reproduction — BEFORE the fix

```python
import os
from corlinman_server.gateway.grpc import agent_server

os.environ["CORLINMAN_PY_ADDR"] = "0.0.0.0:50051"
bind = agent_server.resolve_agent_bind(None)
print(repr(bind))
# '0.0.0.0:50051'                              # silently returned
# server.add_insecure_port(bind) → unauth-RCE socket on every NIC
```

No warning. No error. The dangerous bind is silently accepted.

## Reproduction — AFTER the fix

```python
import os
from corlinman_server.gateway.grpc import agent_server

os.environ["CORLINMAN_PY_ADDR"] = "0.0.0.0:50051"
try:
    agent_server.resolve_agent_bind(None)
except agent_server.GrpcAgentBindError as exc:
    print(exc)
# refusing to bind co-hosted Agent gRPC to non-loopback host '0.0.0.0'
# (resolved bind='0.0.0.0:50051') — the service uses add_insecure_port
# (no TLS, no auth) and exposing it to the network is an
# unauthenticated-RCE risk. Bind to 127.0.0.1, ::1, localhost, or a
# unix:// UDS path; or, if the deployment fronts the socket with an
# mTLS / firewalled proxy, opt in explicitly with
# CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1.
```

The gateway lifespan now refuses to boot the co-hosted agent rather
than silently opening an unauthenticated network surface. The
operator must (a) revert to a loopback bind, (b) switch to a `unix://`
UDS, or (c) opt in **explicitly** by setting
`CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1` — in which case a
`grpc.agent.public_bind` warning is emitted on every boot so the
choice is visible in audit logs.

## Reproduction — AFTER, with explicit opt-in

```python
import os
from corlinman_server.gateway.grpc import agent_server

os.environ["CORLINMAN_PY_ADDR"] = "0.0.0.0:50051"
os.environ["CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC"] = "1"
bind = agent_server.resolve_agent_bind(None)
print(repr(bind))
# '0.0.0.0:50051'                              # opt-in honoured
# (and a warning is logged: "grpc.agent.public_bind ...")
```

Strict comparison against `"1"` — values like `"true"` or `"yes"` do
**not** open the door (defence-in-depth against typos silently
exposing the surface).

## What the fix does not do

This change is the defence-in-depth layer that closes the operator-
footgun path. It does **not** add auth (TLS / mTLS / token) to the
Agent gRPC itself; that is a separate, larger work item. Until that
ships, deployments that genuinely need a non-loopback bind must:

1. Set `CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1` (explicit acknowledgement).
2. Front the socket with an mTLS proxy or firewall it to known
   peers only.

The `grpc.agent.public_bind` warning makes that posture observable.
