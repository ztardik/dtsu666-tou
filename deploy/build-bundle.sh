#!/bin/bash
# build-bundle.sh - build a portable, installable bundle of the DTSU666 logger.
#
# The bundle has the same layout as a database backup, so install.sh can
# consume either:
#
#   dtsu666-portable-<timestamp>.tar.gz
#   ├── app/                 application payload (dtsu666_tou, deploy, tests ...)
#   ├── config/              example configuration (real config only with
#   │                        --with-config; credentials only with --with-secrets)
#   ├── db/                  optional consistent database snapshot (--include-db)
#   ├── VERSION
#   ├── SHA256SUMS
#   └── INSTALL.md
#
# Credentials are excluded by default: distribute a bundle without
# --with-secrets and copy secrets.ini to the target host separately.
#
# Usage: ./deploy/build-bundle.sh [options]
#   --out DIR         output directory (default: ./dist)
#   --name PREFIX     archive name prefix (default: dtsu666-portable)
#   --with-config     include the real tariffs.ini / dtsu666.conf when present
#   --with-secrets    include the real secrets.ini (WARNING: credentials!)
#   --include-db      include a consistent snapshot of the database
#   --src DIR         source tree (default: parent of this script)
#   --verify          verify the produced bundle's internal checksums
#   -h, --help        this help

set -Eeuo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

SRC_DIR="$REPO_DIR"
OUT_DIR="$REPO_DIR/dist"
NAME="dtsu666-portable"
WITH_CONFIG=0
WITH_SECRETS=0
INCLUDE_DB=0
DO_VERIFY=0

log()  { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --out)           OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
        --name)          NAME="${2:?--name needs a value}"; shift 2 ;;
        --src)           SRC_DIR="${2:?--src needs a directory}"; shift 2 ;;
        --with-config)   WITH_CONFIG=1; shift ;;
        --with-secrets)  WITH_SECRETS=1; shift ;;
        --include-db)    INCLUDE_DB=1; shift ;;
        --verify)        DO_VERIFY=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "unknown option: $1 (try --help)" ;;
    esac
done

[ -d "$SRC_DIR/dtsu666_tou" ] || die "$SRC_DIR is not a DTSU666 source tree"

STAMP=$(date +%Y%m%d-%H%M%S)
BASE="$NAME-$STAMP"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/dtsu666-bundle-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
STAGE="$TMP/$BASE"
mkdir -p "$STAGE/app" "$STAGE/config"

step "Building $BASE"
log "source: $SRC_DIR"

# ---- application payload (single source of truth for the file list) ------
PAYLOAD_ITEMS=(dtsu666_tou deploy README.md requirements.txt pytest.ini)
payload=()
for item in "${PAYLOAD_ITEMS[@]}"; do
    [ -e "$SRC_DIR/$item" ] && payload+=("$item")
done
shopt -s nullglob
for f in "$SRC_DIR"/test_*.py; do
    payload+=("$(basename -- "$f")")
done
shopt -u nullglob
[ "${#payload[@]}" -gt 0 ] || die "nothing to package from $SRC_DIR"

tar -C "$SRC_DIR" \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
    --exclude='.pytest_cache' --exclude='.venv' --exclude='venvs' \
    --exclude='backups' --exclude='audits' --exclude='dist' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    -cf - "${payload[@]}" | tar -C "$STAGE/app" -xf -
log "packaged ${#payload[@]} application item(s)"

# ---- configuration -------------------------------------------------------
for f in dtsu666.conf.example secrets.ini.example tariffs.ini.example; do
    [ -f "$SCRIPT_DIR/config/$f" ] && cp -p "$SCRIPT_DIR/config/$f" "$STAGE/config/"
done
if [ "$WITH_CONFIG" -eq 1 ]; then
    for f in dtsu666.conf tariffs.ini; do
        if [ -f "$SRC_DIR/$f" ]; then
            cp -p "$SRC_DIR/$f" "$STAGE/config/$f"
            log "included real $f"
        fi
    done
fi
if [ "$WITH_SECRETS" -eq 1 ]; then
    if [ -f "$SRC_DIR/secrets.ini" ]; then
        cp -p "$SRC_DIR/secrets.ini" "$STAGE/config/secrets.ini"
        chmod 0600 "$STAGE/config/secrets.ini"
        log "included secrets.ini - KEEP THIS BUNDLE PRIVATE (mode 0600)"
    else
        log "WARNING: --with-secrets given but no secrets.ini found"
    fi
fi

# ---- optional database snapshot ------------------------------------------
if [ "$INCLUDE_DB" -eq 1 ]; then
    if [ -f "$SRC_DIR/dtsu666_energy.db" ]; then
        mkdir -p "$STAGE/db"
        python3 - "$SRC_DIR/dtsu666_energy.db" "$STAGE/db/dtsu666_energy.db" <<'PYEOF'
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
PYEOF
        log "included a consistent database snapshot"
    else
        log "WARNING: --include-db given but no database found"
    fi
fi

# ---- metadata ------------------------------------------------------------
APP_VERSION=$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' \
    "$SRC_DIR/dtsu666_tou/config.py" | head -1)
cat > "$STAGE/VERSION" <<EOF
name=$BASE
app_version=${APP_VERSION:-unknown}
built_at=$(date --iso-8601=seconds)
built_on=$(hostname)
source=$SRC_DIR
includes_secrets=$WITH_SECRETS
includes_database=$INCLUDE_DB
EOF

cat > "$STAGE/INSTALL.md" <<EOF
# DTSU666 portable bundle

Built $(date --iso-8601=seconds) from $SRC_DIR.

Install on the target host:

    tar -xzf $BASE.tar.gz
    cd $BASE
    sudo ./app/deploy/install.sh --src "\$PWD/app" --config-dir "\$PWD/config"

Then, if the bundle does not contain secrets.ini:

    sudoedit /opt/dtsu666/etc/secrets.ini

Check the bundle contents before trusting it:

    sha256sum -c SHA256SUMS
EOF

( cd "$STAGE" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )

# ---- archive -------------------------------------------------------------
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/$BASE.tar.gz"
tar -czf "$ARCHIVE" -C "$TMP" "$BASE"
chmod 0644 "$ARCHIVE"

ARCHIVE_SHA=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
printf '%s  %s\n' "$ARCHIVE_SHA" "$(basename -- "$ARCHIVE")" \
    > "$OUT_DIR/$BASE.tar.gz.sha256"

step "Built"
log "archive : $ARCHIVE"
log "size    : $(du -h "$ARCHIVE" | cut -f1)"
log "sha256  : $ARCHIVE_SHA"
log "app     : ${APP_VERSION:-unknown}"
log "secrets : $([ "$WITH_SECRETS" -eq 1 ] && echo INCLUDED || echo excluded)"
log "database: $([ "$INCLUDE_DB" -eq 1 ] && echo INCLUDED || echo excluded)"

if [ "$DO_VERIFY" -eq 1 ]; then
    step "Verifying"
    if ( cd "$STAGE" && sha256sum -c SHA256SUMS --quiet ); then
        log "internal SHA256SUMS verified"
    else
        die "internal checksum verification failed"
    fi
fi
