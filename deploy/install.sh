#!/bin/bash
# install.sh - install the CHINT DTSU666 energy logger as a system service.
#
# Layout created under --prefix (default /opt/dtsu666):
#   app/    application code (read-only for the service)
#   venv/   virtualenv (paho-mqtt, pyserial)
#   etc/    secrets.ini, tariffs.ini, dtsu666.conf
#   var/    dtsu666_energy.db, audits/, backups/
#
# The service runs as the unprivileged user "dtsu666" (supplementary group
# "dialout" for the USB-RS485 adapter) and never as root.
#
# By default the service is installed but NOT enabled or started, so the
# rollout can be reviewed first.  Pass --enable to enable and start it.
#
# Usage:  sudo ./deploy/install.sh [options]
#         ./deploy/install.sh --dry-run          (no root needed)
#
#   --src DIR         application source directory (default: parent of this script)
#   --bundle FILE     install from a portable bundle (.tar.gz) instead of --src
#   --prefix DIR      install prefix (default: /opt/dtsu666)
#   --user NAME       service user (default: dtsu666)
#   --config-dir DIR  source of secrets.ini / tariffs.ini (default: --src)
#   --no-venv         skip creating the virtualenv
#   --no-seed-db      do not copy an existing database into var/
#   --no-service      do not install the systemd unit
#   --no-udev         do not install the udev rule
#   --no-wrappers     do not install /usr/local/bin wrappers
#   --enable          enable and start the service when finished
#   --restart         restart the service after installing (pick up new code)
#   --dry-run         show what would be done, change nothing
#   -h, --help        this help

set -Eeuo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

# Keep the original arguments: the option loop below shifts them away, but a
# sudo re-exec needs them again.
ORIG_ARGS=("$@")

SRC_DIR="$REPO_DIR"
BUNDLE=""
PREFIX="/opt/dtsu666"
SERVICE_USER="dtsu666"
SERVICE_NAME="dtsu666"
CONFIG_DIR=""
CONFIG_DIR_SET=0
DO_VENV=1
DO_SEED_DB=1
DO_SERVICE=1
DO_UDEV=1
DO_WRAPPERS=1
DO_ENABLE=0
DO_RESTART=0
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

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --src)          SRC_DIR="${2:?--src needs a directory}"; shift 2 ;;
        --bundle)       BUNDLE="${2:?--bundle needs a file}"; shift 2 ;;
        --prefix)       PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --user)         SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
        --config-dir)   CONFIG_DIR="${2:?--config-dir needs a directory}"; CONFIG_DIR_SET=1; shift 2 ;;
        --no-venv)      DO_VENV=0; shift ;;
        --no-seed-db)   DO_SEED_DB=0; shift ;;
        --no-service)   DO_SERVICE=0; shift ;;
        --no-udev)      DO_UDEV=0; shift ;;
        --no-wrappers)  DO_WRAPPERS=0; shift ;;
        --enable)       DO_ENABLE=1; shift ;;
        --restart)      DO_RESTART=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
done

[ -n "$CONFIG_DIR" ] || CONFIG_DIR="$SRC_DIR"

APP_DIR="$PREFIX/app"
VENV_DIR="$PREFIX/venv"
ETC_DIR="$PREFIX/etc"
VAR_DIR="$PREFIX/var"
AUDIT_DIR="$VAR_DIR/audits"
BACKUP_DIR="$VAR_DIR/backups"

step "DTSU666 logger installation"
log "source      : ${BUNDLE:-$SRC_DIR}"
log "prefix      : $PREFIX"
log "service user: $SERVICE_USER"
log "dry run     : $([ "$DRY_RUN" -eq 1 ] && echo yes || echo no)"

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        echo "  (re-running with sudo)"
        exec sudo -- "$0" "${ORIG_ARGS[@]}"
    fi
    die "must run as root - use sudo"
fi

# --------------------------------------------------------------------------
# 1. source of truth: --bundle or --src
# --------------------------------------------------------------------------

STAGE=""
BUNDLE_CONFIG=""
BUNDLE_DB=""
TMP_DIR=""

if [ -n "$BUNDLE" ]; then
    [ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
    TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/dtsu666-install-XXXXXX")
    trap '[ -n "${TMP_DIR:-}" ] && rm -rf "$TMP_DIR"' EXIT
    tar -xzf "$BUNDLE" -C "$TMP_DIR"
    top=$(ls "$TMP_DIR")
    STAGE="$TMP_DIR/$top/app"
    BUNDLE_CONFIG="$TMP_DIR/$top/config"
    BUNDLE_DB="$TMP_DIR/$top/db/dtsu666_energy.db"
    [ -d "$STAGE" ] || die "bundle does not contain app/"
    [ "$CONFIG_DIR_SET" -eq 1 ] || CONFIG_DIR="$BUNDLE_CONFIG"
else
    STAGE="$SRC_DIR"
    [ -d "$STAGE/dtsu666_tou" ] || die "$STAGE does not look like the DTSU666 source tree"
fi

step "Installing payload from $STAGE"

# --------------------------------------------------------------------------
# 2. service user and group
# --------------------------------------------------------------------------

if getent group "$SERVICE_USER" >/dev/null; then
    log "group $SERVICE_USER exists"
else
    run groupadd --system "$SERVICE_USER"
    log "created group $SERVICE_USER"
fi

if getent passwd "$SERVICE_USER" >/dev/null; then
    log "user $SERVICE_USER exists"
else
    run useradd --system --gid "$SERVICE_USER" \
        --home-dir "$PREFIX" --no-create-home \
        --shell /usr/sbin/nologin "$SERVICE_USER"
    log "created system user $SERVICE_USER"
fi

# The adapter is root:dialout 0660, so the service needs the dialout group.
if getent group dialout >/dev/null; then
    if id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx dialout; then
        log "$SERVICE_USER is already in dialout"
    else
        run usermod -aG dialout "$SERVICE_USER"
        log "added $SERVICE_USER to dialout"
    fi
else
    log "WARNING: group 'dialout' does not exist - check the udev rule group"
fi

# --------------------------------------------------------------------------
# 3. directory tree
# --------------------------------------------------------------------------

run install -d -m 0755 -o root -g root "$PREFIX"
run install -d -m 0755 -o root -g root "$APP_DIR"
run install -d -m 0755 -o root -g root "$ETC_DIR"
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$VAR_DIR"
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$AUDIT_DIR"
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$BACKUP_DIR"
log "created $PREFIX/{app,etc,var,var/audits,var/backups}"

# --------------------------------------------------------------------------
# 4. application payload
# --------------------------------------------------------------------------

payload=()
for item in dtsu666_tou deploy README.md requirements.txt pytest.ini; do
    [ -e "$STAGE/$item" ] && payload+=("$item")
done
shopt -s nullglob
for f in "$STAGE"/test_*.py; do
    payload+=("$(basename -- "$f")")
done
shopt -u nullglob
[ "${#payload[@]}" -gt 0 ] || die "nothing to install from $STAGE"

if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] would install %d item(s) into %s: %s\n' \
        "${#payload[@]}" "$APP_DIR" "${payload[*]}"
else
    # Remove stale copies of the installed directories first so that files
    # deleted upstream do not linger.
    rm -rf "${APP_DIR:?}/dtsu666_tou" "${APP_DIR:?}/deploy"
    rm -f "${APP_DIR:?}/README.md" "${APP_DIR:?}/requirements.txt" \
          "${APP_DIR:?}/pytest.ini" "${APP_DIR:?}"/test_*.py
    tar -C "$STAGE" \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
        --exclude='.pytest_cache' --exclude='.venv' --exclude='backups' \
        --exclude='audits' --exclude='secrets.ini' \
        -cf - "${payload[@]}" | tar -C "$APP_DIR" -xf -
    chown -R root:root "$APP_DIR"
    chmod -R go-w "$APP_DIR"
fi
log "installed ${#payload[@]} item(s) into $APP_DIR"

# --------------------------------------------------------------------------
# 5. virtualenv
# --------------------------------------------------------------------------

NEEDS_CONFIG=0
FAILED=0

if [ "$DO_VENV" -eq 1 ]; then
    step "Creating the virtualenv"
    if [ -x "$VENV_DIR/bin/python" ]; then
        log "reusing the existing $VENV_DIR"
    else
        run python3 -m venv "$VENV_DIR"
    fi
    run "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    run "$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
    if [ "$DRY_RUN" -eq 0 ]; then
        log "packages: $("$VENV_DIR/bin/pip" list --format=freeze \
            2>/dev/null | grep -iE '^(paho|pyserial)' | tr '\n' ' ')"
    fi
fi

# --------------------------------------------------------------------------
# 6. configuration (secrets.ini is the only file with credentials)
# --------------------------------------------------------------------------

step "Installing configuration into $ETC_DIR"

secrets_source=""
if [ -f "$CONFIG_DIR/secrets.ini" ]; then
    secrets_source="$CONFIG_DIR/secrets.ini"
elif [ -n "$BUNDLE_CONFIG" ] && [ -f "$BUNDLE_CONFIG/secrets.ini" ]; then
    secrets_source="$BUNDLE_CONFIG/secrets.ini"
fi

if [ -n "$secrets_source" ] && [ "$(readlink -f "$secrets_source")" != \
        "$(readlink -f "$ETC_DIR/secrets.ini" 2>/dev/null || echo /nonexistent)" ]; then
    if [ -f "$ETC_DIR/secrets.ini" ]; then
        stamp=$(date +%Y%m%d-%H%M%S)
        run cp -p "$ETC_DIR/secrets.ini" "$ETC_DIR/secrets.ini.bak.$stamp"
        log "backed up the previous secrets.ini to secrets.ini.bak.$stamp"
    fi
    run install -m 0640 -o root -g "$SERVICE_USER" \
        "$secrets_source" "$ETC_DIR/secrets.ini"
    log "installed secrets.ini (root:$SERVICE_USER 0640)"
elif [ -f "$ETC_DIR/secrets.ini" ]; then
    run chown root:"$SERVICE_USER" "$ETC_DIR/secrets.ini"
    run chmod 0640 "$ETC_DIR/secrets.ini"
    log "kept the existing secrets.ini"
else
    run install -m 0640 -o root -g "$SERVICE_USER" \
        "$SCRIPT_DIR/config/secrets.ini.example" "$ETC_DIR/secrets.ini"
    log "installed secrets.ini from the EXAMPLE - edit it before starting"
    NEEDS_CONFIG=1
fi

if [ -f "$CONFIG_DIR/tariffs.ini" ]; then
    run install -m 0644 -o root -g root "$CONFIG_DIR/tariffs.ini" "$ETC_DIR/tariffs.ini"
    log "installed tariffs.ini"
elif [ -f "$ETC_DIR/tariffs.ini" ]; then
    log "kept the existing tariffs.ini"
else
    run install -m 0644 -o root -g root \
        "$SCRIPT_DIR/config/tariffs.ini.example" "$ETC_DIR/tariffs.ini"
    log "installed tariffs.ini from the example"
fi

if [ -f "$CONFIG_DIR/dtsu666.conf" ]; then
    run install -m 0644 -o root -g root "$CONFIG_DIR/dtsu666.conf" "$ETC_DIR/dtsu666.conf"
    log "installed dtsu666.conf"
elif [ -f "$ETC_DIR/dtsu666.conf" ]; then
    log "kept the existing dtsu666.conf"
else
    run install -m 0644 -o root -g root \
        "$SCRIPT_DIR/config/dtsu666.conf.example" "$ETC_DIR/dtsu666.conf"
    log "installed dtsu666.conf (documented defaults)"
fi

# --------------------------------------------------------------------------
# 7. database
# --------------------------------------------------------------------------

step "Database"

DB_PATH="$VAR_DIR/dtsu666_energy.db"
seed_source=""
if [ -n "$BUNDLE_DB" ] && [ -f "$BUNDLE_DB" ]; then
    seed_source="$BUNDLE_DB"
elif [ -f "$SRC_DIR/dtsu666_energy.db" ]; then
    seed_source="$SRC_DIR/dtsu666_energy.db"
fi

if [ -f "$DB_PATH" ]; then
    log "keeping the existing $DB_PATH"
elif [ "$DO_SEED_DB" -eq 1 ] && [ -n "$seed_source" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] would seed %s from %s (consistent snapshot)\n' \
            "$DB_PATH" "$seed_source"
    else
        python3 - "$seed_source" "$DB_PATH" <<'PYEOF'
import os
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{os.path.abspath(src_path)}?mode=ro", uri=True)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
dst.close()
src.close()
print("  seeded a consistent snapshot")
PYEOF
        run chown "$SERVICE_USER:$SERVICE_USER" "$DB_PATH"
        run chmod 0640 "$DB_PATH"
    fi
else
    log "no database to seed - it will be created on first start"
fi

# --------------------------------------------------------------------------
# 8. command wrappers
# --------------------------------------------------------------------------

step "Command wrappers"
WRAPPERS=(dtsu666-audit dtsu666-backup dtsu666-verify-backup dtsu666-restore dtsu666-web)
if [ "$DO_WRAPPERS" -eq 1 ]; then
    for w in "${WRAPPERS[@]}"; do
        run install -m 0755 -o root -g root \
            "$APP_DIR/deploy/bin/$w" "/usr/local/bin/$w"
    done
    log "installed ${WRAPPERS[*]} into /usr/local/bin"
else
    log "skipped (--no-wrappers)"
fi

# --------------------------------------------------------------------------
# 9. systemd unit and udev rule
# --------------------------------------------------------------------------

if [ "$DO_SERVICE" -eq 1 ]; then
    step "systemd"
    run install -m 0644 -o root -g root \
        "$APP_DIR/deploy/systemd/$SERVICE_NAME.service" \
        "/etc/systemd/system/$SERVICE_NAME.service"
    run systemctl daemon-reload
    log "installed /etc/systemd/system/$SERVICE_NAME.service"
    if [ "$DRY_RUN" -eq 0 ]; then
        if systemd-analyze verify "/etc/systemd/system/$SERVICE_NAME.service" \
                >/dev/null 2>&1; then
            log "unit syntax verified by systemd-analyze"
        else
            log "WARNING: systemd-analyze reported problems in the unit"
        fi
    fi
else
    log "skipped systemd unit installation (--no-service)"
fi

if [ "$DO_UDEV" -eq 1 ]; then
    step "udev"
    run install -m 0644 -o root -g root \
        "$APP_DIR/deploy/udev/99-dtsu666.rules" "/etc/udev/rules.d/99-dtsu666.rules"
    if command -v udevadm >/dev/null 2>&1; then
        run udevadm control --reload-rules
        run udevadm trigger --subsystem-match=tty
    fi
    log "installed /etc/udev/rules.d/99-dtsu666.rules"
else
    log "skipped udev rule installation (--no-udev)"
fi

# --------------------------------------------------------------------------
# 10. final permissions
# --------------------------------------------------------------------------

if [ "$DRY_RUN" -eq 0 ]; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$VAR_DIR"
    chmod 0750 "$VAR_DIR" "$AUDIT_DIR" "$BACKUP_DIR" 2>/dev/null || true
    chmod 0640 "$ETC_DIR/secrets.ini" 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# 11. verification
# --------------------------------------------------------------------------

as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$SERVICE_USER" -- "$@"
    else
        su -s /bin/sh -c "$(printf '%q ' "$@")" "$SERVICE_USER"
    fi
}

step "Verification"
if [ "$DRY_RUN" -eq 0 ]; then
    if as_user test -r "$ETC_DIR/secrets.ini"; then
        log "$SERVICE_USER can read secrets.ini"
    else
        log "WARNING: $SERVICE_USER cannot read $ETC_DIR/secrets.ini"
    fi

    if [ -e /dev/ttyUSB0 ]; then
        if as_user test -w /dev/ttyUSB0; then
            log "$SERVICE_USER can write /dev/ttyUSB0"
        else
            log "WARNING: $SERVICE_USER cannot access /dev/ttyUSB0"
            log "         re-plug the adapter after the udev rule is loaded"
        fi
    fi

    if ( cd "$APP_DIR" && as_user "$VENV_DIR/bin/python" -m dtsu666_tou --test ) \
            >/dev/null 2>&1; then
        log "smoke test (python -m dtsu666_tou --test) passed"
    else
        log "WARNING: smoke test failed - run it manually to see why"
    fi

    # Reproduce exactly what the unit's ExecStart does: run from the config
    # directory with PYTHONPATH set, then read the configuration.  This is
    # what catches an import-path or permission problem before first start.
    svc_out=$(as_user env -C "$ETC_DIR" PYTHONPATH="$APP_DIR" \
        "$VENV_DIR/bin/python" -c \
        "import dtsu666_tou.app, dtsu666_tou.config as c; cfg, addr = c.load_config(); print('config OK for address %d' % addr)" \
        2>&1) || true
    case "$svc_out" in
        "config OK for address "*)
            log "service invocation check passed ($svc_out)" ;;
        *)
            log "ERROR: the service could not start as configured:"
            printf '       %s\n' "$svc_out"
            FAILED=1 ;;
    esac

    if as_user /usr/local/bin/dtsu666-audit >/dev/null 2>&1; then
        log "read-only audit ran and wrote a report into $AUDIT_DIR"
    else
        log "NOTE: audit reported problems (run dtsu666-audit to see them)"
    fi
fi

# --------------------------------------------------------------------------
# 12. summary
# --------------------------------------------------------------------------

step "Installed"
cat <<EOF
  app       : $APP_DIR          (root:root, read-only for the service)
  venv      : $VENV_DIR
  config    : $ETC_DIR/{secrets.ini,tariffs.ini,dtsu666.conf}
  data      : $VAR_DIR          ($SERVICE_USER:$SERVICE_USER 0750)
  commands  : /usr/local/bin/dtsu666-{audit,backup,verify-backup,restore,web}
  service   : /etc/systemd/system/$SERVICE_NAME.service
  udev rule : /etc/udev/rules.d/99-dtsu666.rules
  user      : $SERVICE_USER (supplementary group dialout)
EOF

if [ "$NEEDS_CONFIG" -eq 1 ]; then
    echo
    echo "ACTION REQUIRED: edit $ETC_DIR/secrets.ini (it is still the example)."
fi

echo
if [ "$DO_ENABLE" -eq 1 ]; then
    run systemctl enable "$SERVICE_NAME"
    log "service enabled"
fi
if [ "$DO_RESTART" -eq 1 ]; then
    run systemctl restart "$SERVICE_NAME"
    log "service restarted"
    echo
    echo "Check it with:  systemctl status $SERVICE_NAME"
    echo "                journalctl -u $SERVICE_NAME -f"
elif [ "$DO_ENABLE" -eq 1 ]; then
    run systemctl start "$SERVICE_NAME"
    log "service started"
    echo
    echo "Check it with:  systemctl status $SERVICE_NAME"
    echo "                journalctl -u $SERVICE_NAME -f"
else
    cat <<EOF
The service is installed but NOT enabled and NOT started, so the rollout can
be reviewed first.  When you are ready:

  sudo dtsu666-audit                    # read-only audit of the database
  dtsu666-verify-backup <bundle>        # verify a backup bundle
  sudo systemctl start $SERVICE_NAME            # start now (this boot only)
  journalctl -u $SERVICE_NAME -f                # watch it run
  sudo systemctl enable $SERVICE_NAME           # also start at every boot
EOF
fi

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "One or more verification steps failed - fix them before starting"
    echo "the service (see the ERROR/WARNING lines above)."
fi
exit "$FAILED"
