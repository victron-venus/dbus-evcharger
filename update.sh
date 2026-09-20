#!/bin/sh
#
# dbus-evcharger self-update script.
#
# Ships inside the release tarball and runs ON the Venus OS device to install
# the release into INSTALL_DIR (default /data/dbus-evcharger). It is invoked
# by SetupHelper or manually:
#
#     sh update.sh [INSTALL_DIR]
#
# This script owns all layout knowledge (runtime files, daemontools services,
# /service symlinks, device-local file preservation, restart order) so that
# callers never need to hardcode where files go. Adding a new
# module or a new daemontools service requires a change here only.
#
set -eu

# The unified package owns the same charger identity when enabled. Do not
# resurrect this legacy worker during an unattended PackageManager reinstall.
if [ -f /data/dbus-ev/local_config.py ]; then
    if python3 - <<'PYTHON'
from pathlib import Path
values = {}
p = Path("/data/dbus-ev/local_config.py")
exec(compile(p.read_text(), str(p), "exec"), values)
raise SystemExit(0 if values.get("CHARGER_ENABLED") else 1)
PYTHON
    then
        echo "dbus-ev already owns the charger; disable CHARGER_ENABLED before installing this package" >&2
        exit 1
    fi
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${1:-/data/dbus-evcharger}"

# Unit commands and boot hooks use this canonical persistent location.
if [ "$INSTALL_DIR" != "/data/dbus-evcharger" ]; then
    echo "Unsupported install directory: use /data/dbus-evcharger" >&2
    exit 2
fi

# Device-local files that must never be overwritten by an update.
LOCAL_ONLY="local_config.py"

# Runtime items shipped at the repo root and installed at INSTALL_DIR root.
RUNTIME_ITEMS="update.sh dbus_evcharger version setup gitHubInfo local_config.example.py"

# Flat-file leftovers that must never survive an update (we run `python3 -m
# dbus_evcharger`; a stale root main.py would shadow the package).
STALE_TOP_LEVEL="main.py inverter_control"

sep() { echo "=== dbus-evcharger update: $*"; }

# Fail before stopping the existing service if the firmware lacks dependencies.
# Provision packages separately; never modify the system Python during an update.
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PYTHON'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
import requests, dbus, paho.mqtt.client
sys.path.insert(0, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
from gi.repository import GLib
from vedbus import VeDbusService
PYTHON

command -v svc >/dev/null
command -v svstat >/dev/null

# Stage the release before stopping anything. SetupHelper runs this script from
# the installed package tree, which must not be deleted while it is our source.
# /tmp is volatile on Venus OS and avoids writing an extra release to flash.
STAGING_DIR=$(mktemp -d /tmp/dbus-evcharger-update.XXXXXX)
trap 'rm -rf "$STAGING_DIR"' EXIT
trap 'exit 1' HUP INT TERM
mkdir -p "$STAGING_DIR/source" "$STAGING_DIR/backup"
for item in $RUNTIME_ITEMS $LOCAL_ONLY service services; do
    [ ! -e "$SRC_DIR/$item" ] || cp -a "$SRC_DIR/$item" "$STAGING_DIR/source/$item"
done
# Supervise state belongs to the old process, not the release payload.
find "$STAGING_DIR/source" -type d -name supervise -prune -exec rm -rf {} \;
SRC_DIR="$STAGING_DIR/source"
cd "$STAGING_DIR"

# Validate the owned service layout before stopping a healthy worker. Ordinary
# updates preserve the service/log directories and their live supervisors.
SERVICE_TARGET="$INSTALL_DIR/service/dbus-evcharger"
SERVICE_LINK="/service/dbus-evcharger"
UNIT_SOURCE="$SRC_DIR/service/dbus-evcharger"
[ -d "$UNIT_SOURCE" ] || UNIT_SOURCE="$SRC_DIR/services/dbus-evcharger"
for run_file in run log/run; do
    if [ ! -f "$UNIT_SOURCE/$run_file" ]; then
        echo "Missing service definition: $UNIT_SOURCE/$run_file" >&2
        exit 1
    fi
done
if [ -L "$SERVICE_LINK" ]; then
    if [ "$(readlink "$SERVICE_LINK")" != "$SERVICE_TARGET" ]; then
        echo "Refusing to replace an unexpected service symlink: $SERVICE_LINK" >&2
        exit 1
    fi
elif [ -e "$SERVICE_LINK" ]; then
    echo "Legacy service directory requires migration before updating: $SERVICE_LINK" >&2
    exit 1
fi
for directory in "$INSTALL_DIR/service" "$SERVICE_TARGET" "$SERVICE_TARGET/log"; do
    if [ -L "$directory" ] || { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then
        echo "Unexpected service directory layout: $directory" >&2
        exit 1
    fi
done
LEGACY_OPT="/opt/victronenergy/dbus-evcharger"
if [ -e "$LEGACY_OPT" ] || [ -L "$LEGACY_OPT" ]; then
    echo "Legacy firmware installation requires migration before updating: $LEGACY_OPT" >&2
    exit 1
fi

# Stop only the application; its existing logger and supervisors stay alive.
# Verify termination before replacing Python files. A stuck worker gets one
# supervisor-scoped kill after twenty seconds; unrelated processes are untouched.
stop_worker() {
    [ -e "$SERVICE_LINK" ] || return 0
    svc -d "$SERVICE_LINK" || return 1
    waited=0
    while [ "$waited" -lt 25 ]; do
        status=$(svstat "$SERVICE_LINK" 2>/dev/null || true)
        case "$status" in *": down "*) return 0 ;; esac
        if [ "$waited" -eq 20 ]; then
            svc -k "$SERVICE_LINK" || return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "Service did not stop; runtime files were not changed: $SERVICE_LINK" >&2
    return 1
}
stop_worker

mkdir -p "$INSTALL_DIR"
sep "installing from $SRC_DIR into $INSTALL_DIR"

# 2. Back up device-local files so the wholesale copy below can restore them.
TMP_BACKUP="$STAGING_DIR/backup"
mkdir -p "$TMP_BACKUP"
for f in $LOCAL_ONLY; do
    [ -f "$INSTALL_DIR/$f" ] && cp -p "$INSTALL_DIR/$f" "$TMP_BACKUP/"
done

# 3. Install runtime items (replace wholesale to also drop stale files).
for item in $RUNTIME_ITEMS; do
    [ -n "$item" ] || continue
    if [ -e "$SRC_DIR/$item" ]; then
        rm -rf "${INSTALL_DIR:?}/$item"
        cp -a "$SRC_DIR/$item" "$INSTALL_DIR/$item"
    fi
done

# Replace only the two owned run scripts, using rename in each destination
# directory. Open files, log pipes, directory ownership and supervise state
# survive the update. A running multilog uses the new script on its next start.
mkdir -p "$SERVICE_TARGET/log"
for run_file in run log/run; do
    destination="$SERVICE_TARGET/$run_file"
    temporary=$(mktemp "$(dirname "$destination")/.run.XXXXXX")
    if [ -f "$destination" ]; then
        cp -p "$destination" "$temporary"
    fi
    cat "$UNIT_SOURCE/$run_file" > "$temporary"
    chmod 755 "$temporary"
    mv -f "$temporary" "$destination"
done
rm -f "$SERVICE_TARGET/down" "$SERVICE_TARGET/log/down"

# 5. Restore device-local files and drop stale flat-file leftovers.
for f in $LOCAL_ONLY; do
    [ -f "$TMP_BACKUP/$f" ] && cp -p "$TMP_BACKUP/$f" "$INSTALL_DIR/$f"
done
rm -rf "$TMP_BACKUP"
for f in $STALE_TOP_LEVEL; do
    rm -rf "${INSTALL_DIR:?}/$f"
done

# 5b. Optional: push the developer's local_config.py instead of keeping the
#     device copy (used by deploy.sh, where the dev machine is authoritative).
if [ "${PUSH_LOCAL_CONFIG:-0}" = "1" ] && [ -f "$SRC_DIR/local_config.py" ]; then
    SETUP_OPTIONS_DIR="/data/setupOptions/dbus-evcharger"
    mkdir -p "$SETUP_OPTIONS_DIR"
    cp -p "$SRC_DIR/local_config.py" "$INSTALL_DIR/local_config.py"
    cp -p "$SRC_DIR/local_config.py" "$SETUP_OPTIONS_DIR/local_config.py"
    sep "pushed local_config.py (PUSH_LOCAL_CONFIG=1)"
fi

# A valid existing symlink must retain its inode too. Never replace a live
# service directory or move another service out of the way.
if [ ! -L "$SERVICE_LINK" ]; then
    ln -s "$SERVICE_TARGET" "$SERVICE_LINK"
fi

# Refresh the boot hook before an existing exit statement. On boot it creates
# a missing canonical link, without deleting a directory or unexpected link.
RC_LOCAL="/data/rc.local"
if [ ! -f "$RC_LOCAL" ]; then
    printf '#!/bin/sh\n' > "$RC_LOCAL"
    chmod +x "$RC_LOCAL"
fi
sed -i '/# === dbus-evcharger service persistence ===/,/# === end dbus-evcharger ===/d' "$RC_LOCAL" 2>/dev/null || true
RC_BLOCK="$STAGING_DIR/rc.block"
cat > "$RC_BLOCK" << 'RCEOF'

# === dbus-evcharger service persistence ===
if [ ! -e /service/dbus-evcharger ] && [ ! -L /service/dbus-evcharger ]; then
    ln -s /data/dbus-evcharger/service/dbus-evcharger /service/dbus-evcharger
fi
if [ -L /service/dbus-evcharger ] && [ "$(readlink /service/dbus-evcharger)" = /data/dbus-evcharger/service/dbus-evcharger ]; then
    svc -u /service/dbus-evcharger/log 2>/dev/null || true
    svc -u /service/dbus-evcharger 2>/dev/null || true
fi
# === end dbus-evcharger ===
RCEOF
# An existing rc.local may end in "exit 0"; boot hooks appended after it never run.
awk -v block="$RC_BLOCK" '
    !inserted && /^exit[ \t]+0[ \t]*$/ {
        while ((getline line < block) > 0) print line
        close(block)
        inserted = 1
    }
    { print }
    END { if (!inserted) while ((getline line < block) > 0) print line }
' "$RC_LOCAL" > "$STAGING_DIR/rc.local"
cat "$STAGING_DIR/rc.local" > "$RC_LOCAL"
chmod +x "$RC_LOCAL"
sep "refreshed rc.local boot persistence block"

# Existing supervisors remain attached. Only a first installation needs
# svscan to notice the new link. PackageManager is not restarted by an update.
sleep 3
for service_path in "$SERVICE_LINK/log" "$SERVICE_LINK"; do
    svc -u "$service_path"
done

sep "installed version $(cat "$INSTALL_DIR/version" 2>/dev/null || echo unknown)"
