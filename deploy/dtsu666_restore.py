#!/usr/bin/env python3
"""dtsu666_restore.py - verify and restore a DTSU666 backup bundle.

Safety model
------------
* The bundle is ALWAYS verified (per-file SHA-256 + SQLite
  ``integrity_check``) before anything is written.  A verification
  failure aborts the restore.
* Writing requires ``--force`` (guard against accidental data loss).
* If the logger service is active the restore refuses to run unless
  ``--force`` is given, so nothing writes to the database mid-restore.
* The current database is preserved as a consistent point-in-time copy
  (``<db>.pre-restore-<timestamp>``) before being replaced.
* Because a replaced SQLite database must not inherit a stale WAL, the
  old ``-wal``/``-shm`` files are moved aside together with the old
  database.

Usage:
    dtsu666-restore BUNDLE [--verify-only] [--target PATH] [--force]
                           [--dry-run] [--restore-config] [--restore-app]

Exit code:
    0  success
    1  bundle verification failed
    2  aborted / could not restore
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Zagreb")
TOOL_NAME = "dtsu666_restore"
TOOL_VERSION = "1.0"

DEFAULT_APP_DIR = "."
DEFAULT_DB = "dtsu666_energy.db"
DEFAULT_SECRETS = "secrets.ini"
DEFAULT_TARIFFS = "tariffs.ini"
SERVICE_NAME = "dtsu666"

try:  # bundle tools are deployed side by side
    from dtsu666_backup import snapshot_db, verify_bundle
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dtsu666_backup import snapshot_db, verify_bundle


def _now():
    return datetime.now(LOCAL_TZ)


def _iso_now():
    return _now().isoformat(timespec="seconds")


def service_active(name=SERVICE_NAME):
    """Return True/False, or None when systemd cannot be queried."""
    if shutil.which("systemctl") is None:
        return None
    try:
        res = subprocess.run(["systemctl", "is-active", name],
                             capture_output=True, text=True, timeout=5,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() == "active"


def extract_bundle(bundle, dest):
    """Extract *bundle* into *dest*; return ``(root_dir, manifest)``."""
    with tarfile.open(bundle, "r:gz") as tf:
        names = tf.getnames()
        tf.extractall(dest, filter="data")
    tops = sorted({n.split("/")[0] for n in names})
    root = os.path.join(dest, tops[0])
    with open(os.path.join(root, "manifest.json")) as fh:
        manifest = json.load(fh)
    return root, manifest


def _move_aside(path):
    dest = f"{path}.pre-restore-{_now().strftime('%Y%m%d-%H%M%S')}"
    os.replace(path, dest)
    return dest


def _chown(path, owner):
    if shutil.which("chown") is None:
        return False
    return subprocess.run(["chown", owner, path], check=False).returncode == 0


def restore_db(snapshot, target, *, backup_current=True, owner=None,
               dry_run=False):
    """Replace *target* with the database at *snapshot*.

    A consistent point-in-time copy of the current database is kept when
    *backup_current* is set.  Returns a dict describing the action.
    """
    result = {"target": os.path.abspath(target), "safety_copy": None,
              "moved_aside": [], "dry_run": bool(dry_run)}
    if dry_run:
        return result

    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)

    if backup_current and os.path.exists(target):
        safe = f"{target}.pre-restore-{_now().strftime('%Y%m%d-%H%M%S')}"
        snapshot_db(target, safe)
        os.chmod(safe, 0o600)
        result["safety_copy"] = safe

    for suffix in ("", "-wal", "-shm", "-journal"):
        path = target + suffix
        if os.path.exists(path):
            result["moved_aside"].append(_move_aside(path))

    staging = os.path.join(os.path.dirname(os.path.abspath(target)),
                           f".{os.path.basename(target)}.restore-{os.getpid()}")
    shutil.copyfile(snapshot, staging)
    os.chmod(staging, 0o600)
    if owner:
        _chown(staging, owner)
    os.replace(staging, target)
    return result


def restore_config(root, app_dir, *, secrets_name, tariffs_name,
                   backup_current=True, dry_run=False):
    """Restore secrets.ini / tariffs.ini from the bundle."""
    actions = []
    for src_rel, dst_name in (("config/secrets.ini", secrets_name),
                              ("config/tariffs.ini", tariffs_name)):
        src = os.path.join(root, src_rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(app_dir, dst_name)
        action = {"source": src_rel, "target": dst}
        if not dry_run:
            if backup_current and os.path.exists(dst):
                action["safety_copy"] = _move_aside(dst)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
        actions.append(action)
    return actions


def restore_app(root, app_dir, *, dry_run=False):
    """Restore application files from the bundle (code only)."""
    src_app = os.path.join(root, "app")
    if not os.path.isdir(src_app) or dry_run:
        return []
    copied = []
    for name in sorted(os.listdir(src_app)):
        src = os.path.join(src_app, name)
        dst = os.path.join(app_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    return copied


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dtsu666-restore",
        description="Verify and restore a DTSU666 backup bundle.")
    ap.add_argument("bundle", nargs="?", help="backup bundle (.tar.gz)")
    ap.add_argument("--app-dir", default=None)
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="target database name or absolute path")
    ap.add_argument("--secrets", default=DEFAULT_SECRETS)
    ap.add_argument("--tariffs", default=DEFAULT_TARIFFS)
    ap.add_argument("--target", default=None,
                    help="restore the database to this path instead")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="allow replacing an existing database / active service")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore-config", action="store_true",
                    help="also restore secrets.ini and tariffs.ini")
    ap.add_argument("--restore-app", action="store_true",
                    help="also restore application files")
    ap.add_argument("--no-backup-current", action="store_true",
                    help="do not keep a copy of the current database")
    ap.add_argument("--owner", default=None,
                    help="chown the restored database to USER[:GROUP]")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not args.bundle:
        ap.error("a bundle path is required")

    problems, manifest = verify_bundle(args.bundle)
    if problems or manifest is None:
        print("BUNDLE VERIFICATION FAILED - nothing was changed:",
              file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1

    if args.verify_only:
        if args.json:
            print(json.dumps({"bundle": args.bundle, "ok": True,
                              "manifest": manifest}, indent=2, default=str))
        else:
            print(f"bundle : {args.bundle}")
            print(f"created: {manifest['created_at']}")
            print(f"app    : {manifest['app_version']} "
                  f"files={manifest['file_count']}")
            print(f"tables : {manifest['database']['table_counts']}")
            print("result : OK (checksums + database integrity verified)")
        return 0

    app_dir = os.path.abspath(args.app_dir or os.getcwd())
    if args.target:
        target = args.target
    elif os.path.isabs(args.db):
        target = args.db
    else:
        target = os.path.join(app_dir, args.db)

    active = service_active()
    if active and not args.force:
        print(f"ERROR: service '{SERVICE_NAME}' is active. Stop it first "
              f"(sudo systemctl stop {SERVICE_NAME}) or pass --force.",
              file=sys.stderr)
        return 2
    if os.path.exists(target) and not (args.force or args.dry_run):
        print(f"ERROR: target already exists: {target} "
              "(pass --force to replace it)", file=sys.stderr)
        return 2

    tmp = tempfile.mkdtemp(prefix="dtsu666-restore-")
    try:
        root, mf = extract_bundle(args.bundle, tmp)
        snapshot = os.path.join(root, mf["database"]["snapshot_path"])
        if not os.path.isfile(snapshot):
            print("ERROR: database snapshot missing from bundle",
                  file=sys.stderr)
            return 2

        result = {
            "bundle": os.path.abspath(args.bundle),
            "manifest_created_at": mf["created_at"],
            "app_version": mf["app_version"],
            "target": os.path.abspath(target),
            "dry_run": bool(args.dry_run),
            "service_active": active,
        }
        result["database"] = restore_db(
            snapshot, target, backup_current=not args.no_backup_current,
            owner=args.owner, dry_run=args.dry_run)
        if args.restore_config:
            result["config"] = restore_config(
                root, app_dir, secrets_name=args.secrets,
                tariffs_name=args.tariffs,
                backup_current=not args.no_backup_current,
                dry_run=args.dry_run)
        if args.restore_app:
            result["app"] = restore_app(root, app_dir, dry_run=args.dry_run)

        if not args.dry_run:
            conn = sqlite3.connect(target)
            ic = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            counts = {
                n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
                for (n,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")}
            conn.close()
            result["post_restore_integrity_check"] = ic
            result["post_restore_table_counts"] = counts
            result["ok"] = ic == ["ok"]

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"bundle : {result['bundle']}")
            print(f"created: {result['manifest_created_at']}")
            print(f"target : {result['target']}"
                  + ("  (dry run - nothing written)" if args.dry_run else ""))
            if result["database"]["safety_copy"]:
                print(f"kept   : {result['database']['safety_copy']}")
            for moved in result["database"]["moved_aside"]:
                print(f"aside  : {moved}")
            for action in result.get("config", []):
                print(f"config : {action['source']} -> {action['target']}")
            if "app" in result:
                print(f"app    : restored {len(result['app'])} item(s)")
            if not args.dry_run:
                print(f"tables : {result['post_restore_table_counts']}")
                print("result : " + ("OK" if result.get("ok") else "FAILED"))

        if args.dry_run:
            return 0
        if not result.get("ok", False):
            print("ERROR: restored database failed integrity_check",
                  file=sys.stderr)
            return 2
        print(f"\nNext: sudo systemctl start {SERVICE_NAME} && "
              f"journalctl -u {SERVICE_NAME} -n 50")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
