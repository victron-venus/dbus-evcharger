#!/bin/bash
#
# Deploy dbus-evcharger to Venus OS
#
# Packs the local repository (minus VCS/CI/cache cruft), streams it to the
# device and runs the repo's own self-update script (update.sh) there, so all
# install logic lives in exactly one place - the same path the auto-deploy
# webhook uses for release tarballs.
#
# Prerequisites:
#   - SSH config with host 'Cerbo' pointing to Venus OS device
#   - SSH key authentication configured
#
# Usage: ./deploy.sh [SSH_HOST]
#

set -e

SSH_HOST="${1:-Cerbo}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="/data/.dbus-evcharger-deploy"
SEPARATOR="=============================================="

echo "$SEPARATOR"
echo "  Deploying dbus-evcharger to Venus OS"
echo "$SEPARATOR"
echo "SSH Host: $SSH_HOST"
echo ""

# Check local syntax before shipping (fail fast on the dev machine)
echo ">>> Checking Python syntax..."
python3 -m py_compile "$SCRIPT_DIR"/dbus_evcharger/*.py
echo "    Syntax OK"

# Package the repo and run update.sh on the device. `set -e` on the remote
# aborts the whole chain if update.sh fails, so the deploy is atomic-ish.
#
echo ">>> Streaming repository to $SSH_HOST and running update.sh..."
tar \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='.coverage' \
    --exclude='logs' \
    --exclude='*.egg-info' \
    --exclude='.venv' \
    --exclude='build' \
    --exclude='.mcp.json' \
    -czf - -C "$SCRIPT_DIR" . \
    | ssh "$SSH_HOST" "set -e; rm -rf $DEPLOY_DIR; mkdir -p $DEPLOY_DIR; \
        tar -xz -C $DEPLOY_DIR --strip-components=1; \
        rm -f /run/dbus-evcharger/heartbeat; \
        PUSH_LOCAL_CONFIG=1 sh $DEPLOY_DIR/update.sh; \
        waited=0; while [ \$waited -lt 15 ] && ! [ -f /run/dbus-evcharger/heartbeat ]; do sleep 1; waited=\$((waited + 1)); done; \
        rm -rf $DEPLOY_DIR"

# Wait for supervise to bring the service back up (svc -u is async)
echo ">>> Service status:"
STATUS=""
for i in $(seq 1 15); do
    sleep 1
    if STATUS="$(ssh "$SSH_HOST" "svstat /service/dbus-evcharger 2>&1")"; then
        printf '%s\n' "$STATUS"
        break
    fi
done
[ "$i" == "15" ] && echo "svstat failed: $STATUS" && exit 1

# The service dir must be a symlink into the install tree. A real directory
# here means stale code got resurrected (legacy /opt copy or boot-order race)
# and will keep running no matter what update.sh installs elsewhere.
if ! ssh "$SSH_HOST" "test -L /service/dbus-evcharger"; then
    echo "ERROR: /service/dbus-evcharger is not a symlink - split-brain install" >&2
    exit 1
fi

echo ""
echo "$SEPARATOR"
echo "  Deployment Complete!"
echo "$SEPARATOR"
