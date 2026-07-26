#!/bin/sh
set -e

role="${CORLINMAN_PROCESS_ROLE:-combined}"
: "${CORLINMAN_PY_CONFIG:=/data/py-config.json}"
export CORLINMAN_PY_CONFIG

# The gateway boot path owns the only py-config writer. It resolves secret
# references and publishes atomically with restrictive ownership before runtime
# reconciliation; the Agent only watches the completed drop.

# QQ-managed deployments split the privileged gateway from model-controlled
# agent execution. The agent container never receives the NapCat manager socket;
# the gateway is forced through gRPC and therefore never executes model tools.
case "${CORLINMAN_PROCESS_ROLE:-combined}" in
    agent)
        # gRPC creates the Unix socket using the process umask. Keep group write
        # for the gateway's narrow IPC group while denying all other users.
        umask 0007
        exec /opt/venv/bin/corlinman-python-server
        ;;
    gateway)
        exec /opt/venv/bin/corlinman-gateway --config /data/config.toml
        ;;
    combined)
        ;;
    *)
        echo "ERROR: invalid CORLINMAN_PROCESS_ROLE=${CORLINMAN_PROCESS_ROLE}" >&2
        exit 64
        ;;
esac

# Backward-compatible combined image mode for deployments without the QQ
# manager boundary. Boot the agent sidecar first, then wait for its listener.
/opt/venv/bin/corlinman-python-server &
echo "waiting for python sidecar gRPC ready on :50051..."
ready=""
i=1
while [ "$i" -le 60 ]; do
    if /opt/venv/bin/python3 -c "import socket; s=socket.socket(); s.settimeout(0.5); s.connect(('127.0.0.1',50051))" 2>/dev/null; then
        ready="1"
        break
    fi
    sleep 0.3
    i=$((i + 1))
done
if [ -z "$ready" ]; then
    echo "WARN: python sidecar did not become ready within 18s — starting gateway anyway"
fi

exec /opt/venv/bin/corlinman-gateway --config /data/config.toml
