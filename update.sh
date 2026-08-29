#!/bin/sh
#
# dbus-evcharger self-update script.
#
# Ships inside the release tarball and runs ON the Venus OS device to install
# the release into INSTALL_DIR (default /data/dbus-evcharger). It is invoked
# by the auto-deploy webhook (../inverter-monitoring) or manually:
#
#     sh update.sh [INSTALL_DIR]
#
# This script owns all layout knowledge (runtime files, daemontools services,
# /service symlinks, device-local file preservation, restart order) so that
# callers like the webhook never need to hardcode where files go. Adding a new
# module or a new daemontools service requires a change here only.
#
set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${1:-/data/dbus-evcharger}"

# Device-local files that must never be overwritten by an update.
LOCAL_ONLY="local_config.py"

# Runtime items shipped at the repo root and installed at INSTALL_DIR root.
RUNTIME_ITEMS="dbus_evcharger version"

# Flat-file leftovers that must never survive an update (we run `python3 -m
# dbus_evcharger`; a stale root main.py would shadow the package).
STALE_TOP_LEVEL="main.py inverter_control"

sep() { echo "=== dbus-evcharger update: $*"; }

# 1. Stop the services BEFORE touching files so a half-written tree is never
#    executed and the multilog log dir is not disturbed under a running logger.
for svc in /service/dbus-evcharger/log /service/dbus-evcharger; do
    [ -e "$svc" ] && svc -dk "$svc" 2>/dev/null || true
done
sleep 1

# 1c. Reap stale daemontools supervise processes left behind by earlier
#     updates. Every time a service dir under $INSTALL_DIR/service is replaced
#     the inode changes, so svscan spawns a NEW supervise and the old one is
#     never killed - they linger forever with "(deleted)" cwd. Several
#     supervisors on one service corrupt runit state (broken log pipes that
#     crash print() with EPIPE, and down services that svc -u cannot bring up).
#     The same inode churn also orphans the run processes themselves
#     (cwd == $INSTALL_DIR): when a supervise dies, svc -dk can no longer
#     reach its child, so it keeps running the old code and hammering D-Bus
#     next to the new instance. Drop the /service symlinks first so svscan
#     does not respawn supervisors while we replace the dirs below, then kill
#     anything whose cwd lives under our install tree. Fresh
#     supervisors are spawned in step 6.
#     rm -rf (not rm -f): a pre-existing legacy install may have left a real
#     directory here; ln -sf onto a directory would nest the link inside it.
rm -rf /service/dbus-evcharger
sleep 2
for pid in /proc/[0-9]*; do
    cwd=$(readlink "$pid/cwd" 2>/dev/null) || continue
    case "$cwd" in
        "$INSTALL_DIR/service/"*)
            kill -9 "${pid##*/}" 2>/dev/null || true
            ;;
        "$INSTALL_DIR")
            kill -9 "${pid##*/}" 2>/dev/null || true
            ;;
        *)
            # Ignore processes outside our install tree
            ;;
    esac
done
sleep 1

# 1d. Remove the legacy single-script install. Venus OS boot machinery links
#     everything under /opt/victronenergy into /service at startup, so a left-
#     over copy there resurrects a REAL /service/dbus-evcharger directory after
#     every reboot - and the later `ln -sf` in step 6/rc.local then silently
#     fails onto that directory, running stale code forever (seen 2026-08-25).
LEGACY_OPT="/opt/victronenergy/dbus-evcharger"
if [ -e "$LEGACY_OPT" ]; then
    rm -rf "$LEGACY_OPT"
    sep "removed legacy $LEGACY_OPT"
fi

mkdir -p "$INSTALL_DIR"
sep "installing from $SRC_DIR into $INSTALL_DIR"

# 2. Back up device-local files so the wholesale copy below can restore them.
TMP_BACKUP="/tmp/dbus-evcharger-update-$$"
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

# 4. Install daemontools services: every dir under service/ and services/
#    maps to INSTALL_DIR/service/. New services are picked up automatically.
#    A manual `svc -d` on the device leaves a `down` file behind, which would
#    keep the service "normally down" across reboots even after `svc -u` -
#    a deploy means "run the new version", so drop them.
mkdir -p "$INSTALL_DIR/service"
for svc in "$SRC_DIR/service"/* "$SRC_DIR/services"/*; do
    [ -d "$svc" ] || continue
    name="$(basename "$svc")"
    rm -rf "$INSTALL_DIR/service/$name"
    cp -a "$svc" "$INSTALL_DIR/service/$name"
    find "$INSTALL_DIR/service/$name" -type f -name run -exec chmod +x {} \; 2>/dev/null || true
    find "$INSTALL_DIR/service/$name" -name down -exec rm -f {} \; 2>/dev/null || true
done

# 5. Restore device-local files and drop stale flat-file leftovers.
for f in $LOCAL_ONLY; do
    [ -f "$TMP_BACKUP/$f" ] && cp -p "$TMP_BACKUP/$f" "$INSTALL_DIR/$f"
done
rm -rf "$TMP_BACKUP"
for f in $STALE_TOP_LEVEL; do
    rm -f "$INSTALL_DIR/$f"
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

# 6. Refresh /service symlinks.
ln -sf "$INSTALL_DIR/service/dbus-evcharger" /service/

# 6a. Ensure boot persistence: /service is tmpfs, so rc.local recreates the
#     symlink on every boot. The block is rewritten on every update so fixes
#     reach devices that already carry an older block (marker-delimited).
#     `rm -rf` before `ln -sf`: if anything recreated a real directory at
#     /service/dbus-evcharger, ln -sf would fail silently onto it and stale code
#     would keep running.
RC_LOCAL="/data/rc.local"
if [ ! -f "$RC_LOCAL" ]; then
    printf '#!/bin/sh\n' > "$RC_LOCAL"
    chmod +x "$RC_LOCAL"
fi
sed -i '/# === dbus-evcharger service persistence ===/,/# === end dbus-evcharger ===/d' "$RC_LOCAL" 2>/dev/null || true
cat >> "$RC_LOCAL" << 'RCEOF'

# === dbus-evcharger service persistence ===
# Recreate /service symlink on boot (lost since /service is tmpfs).
rm -rf /service/dbus-evcharger
ln -sf /data/dbus-evcharger/service/dbus-evcharger /service/dbus-evcharger
sleep 2
svc -u /service/dbus-evcharger/log 2>/dev/null || true
svc -u /service/dbus-evcharger 2>/dev/null || true
# === end dbus-evcharger ===
RCEOF
sep "refreshed rc.local boot persistence block"

# 6b. Give svscan a moment to spawn fresh supervisors for the new symlinks
#     before we try to bring the services up, so svc -u lands on a live one.
sleep 3

# 7. Let PackageManager rediscover the package (version changed).
svc -t /service/PackageManager 2>/dev/null || true

# 8. Bring everything back up (svc -d only marks down; svc -u starts).
for svc in /service/dbus-evcharger/log /service/dbus-evcharger; do
    [ -e "$svc" ] && svc -u "$svc" 2>/dev/null || true
done

sep "installed version $(cat "$INSTALL_DIR/version" 2>/dev/null || echo unknown)"