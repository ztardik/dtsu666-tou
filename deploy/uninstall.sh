#!/bin/bash
# uninstall.sh - remove the CHINT DTSU666 energy logger service.
#
# By default the database, audits, backups and configuration are KEPT so the
# system can be re-installed or recovered by hand.  Pass --purge to delete
# everything under the prefix; a final backup is taken first unless
# --no-backup is given.
#
# Usage:  sudo ./deploy/uninstall.sh [options]
#         ./deploy/uninstall.sh --dry-run           (no root needed)
#
#   --prefix DIR   install prefix (default: /opt/dtsu666)
#   --user NAME    service user (default: dtsu666)
#   --purge        also remove the install prefix (data + configuration)
#   --no-backup    skip the last backup before --purge
#   --keep-user    do not delete the system user/group
#   --dry-run      show what would be done, change nothing
#   -h, --help     this help

set -Eeuo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ORIG_ARGS=("$@")

PREFIX="/opt/dtsu666"
SERVICE_USER="dtsu666"
SERVICE_NAME="dtsu666"
DO_PURGE=0
DO_BACKUP=1
KEEP_USER=0
DRY_RUN=0

log()  { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)     PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --user)       SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
        --purge)      DO_PURGE=1; shift ;;
        --no-backup)  DO_BACKUP=0; shift ;;
        --keep-user)  KEEP_USER=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
done

VAR_DIR="$PREFIX/var"
APP_DIR="$PREFIX/app"
ETC_DIR="$PREFIX/etc"

step "DTSU666 logger removal"
log "prefix  : $PREFIX"
log "purge   : $([ "$DO_PURGE" -eq 1 ] && echo yes || echo no)"
log "dry run : $([ "$DRY_RUN" -eq 1 ] && echo yes || echo no)"

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -- "$0" "${ORIG_ARGS[@]}"
    fi
    die "must run as root - use sudo"
fi

# ---- stop and disable the service ---------------------------------------
if command -v systemctl >/dev/null 2>&1; then
    step "systemd"
    if systemctl list-unit-files "$SERVICE_NAME.service" >/dev/null 2>&1; then
        run systemctl disable --now "$SERVICE_NAME" || true
    fi
    if [ -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
        run rm -f "/etc/systemd/system/$SERVICE_NAME.service"
        run systemctl daemon-reload
        run systemctl reset-failed "$SERVICE_NAME" || true
        log "removed the systemd unit"
    else
        log "no systemd unit installed"
    fi
fi

# ---- udev rule -----------------------------------------------------------
if [ -f /etc/udev/rules.d/99-dtsu666.rules ]; then
    step "udev"
    run rm -f /etc/udev/rules.d/99-dtsu666.rules
    if command -v udevadm >/dev/null 2>&1; then
        run udevadm control --reload-rules
    fi
    log "removed the udev rule"
fi

# ---- command wrappers ----------------------------------------------------
step "Command wrappers"
for w in dtsu666-audit dtsu666-backup dtsu666-verify-backup dtsu666-restore; do
    if [ -e "/usr/local/bin/$w" ]; then
        run rm -f "/usr/local/bin/$w"
        log "removed /usr/local/bin/$w"
    fi
done

# ---- last backup + purge -------------------------------------------------
if [ "$DO_PURGE" -eq 1 ]; then
    step "Removing $PREFIX"
    if [ "$DO_BACKUP" -eq 1 ] && [ -f "$VAR_DIR/dtsu666_energy.db" ] \
            && [ "$DRY_RUN" -eq 0 ]; then
        out="/root/dtsu666-final-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
        if "$APP_DIR/deploy/bin/dtsu666-backup" \
                --dest "$(dirname "$out")" >/dev/null 2>&1; then
            log "final backup written under $(dirname "$out")"
        else
            log "WARNING: final backup failed - purge anyway? (Ctrl-C to abort)"
            sleep 5
        fi
    fi
    run rm -rf "${PREFIX:?}"
    log "removed $PREFIX"
else
    step "Keeping data and configuration"
    log "kept $VAR_DIR"
    log "kept $ETC_DIR"
    log "kept $APP_DIR"
    echo
    echo "  To remove everything as well, re-run with --purge."
fi

# ---- user / group --------------------------------------------------------
if [ "$KEEP_USER" -eq 0 ]; then
    step "Service user"
    if getent passwd "$SERVICE_USER" >/dev/null; then
        run userdel "$SERVICE_USER" || log "WARNING: could not delete the user"
        log "deleted user $SERVICE_USER"
    fi
    if getent group "$SERVICE_USER" >/dev/null; then
        run groupdel "$SERVICE_USER" || log "WARNING: could not delete the group"
        log "deleted group $SERVICE_USER"
    fi
else
    log "kept the user $SERVICE_USER (--keep-user)"
fi

step "Done"
