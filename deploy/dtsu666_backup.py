#!/usr/bin/env python3
"""dtsu666_backup.py - consistent, verifiable backup of the CHINT DTSU666
energy logger (database + configuration + application + reports).

Design goals
------------
* The database snapshot is taken through the SQLite online-backup API, so
  a *live* WAL database is captured consistently (never a raw file copy).
* Every file in the bundle is hashed (SHA-256) and recorded in a
  ``manifest.json`` so a restore can be verified later.
* Secrets are included so the bundle is self-contained, but the manifest
  and the printed summary never contain credential *values*.
* Standard library only - usable even when the application's virtualenv
  is broken.

Usage:
    dtsu666-backup [--db PATH] [--dest DIR] [--app-dir PATH]
                   [--keep N] [--no-secrets] [--json]

Exit code:
    0  backup created and verified
    1  verification of the produced bundle failed
    2  the backup could not be created
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import tarfile
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Zagreb")
TOOL_NAME = "dtsu666_backup"
TOOL_VERSION = "1.0"
BUNDLE_VERSION = 1

DEFAULT_DB = "dtsu666_energy.db"
DEFAULT_SECRETS = "secrets.ini"
DEFAULT_TARIFFS = "tariffs.ini"
DEFAULT_DEST = "backups"

# Files/dirs copied into every bundle (relative to the app dir).
APP_ITEMS = [
    "dtsu666_tou",
    "deploy",
    "README.md",
    "requirements.txt",
]
# Optional items: copied when present.
APP_ITEM_OPTIONAL = [
    "tests",
]


def _now():
    return datetime.now(LOCAL_TZ)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


# --------------------------------------------------------------------------
# database snapshot
# --------------------------------------------------------------------------

def snapshot_db(src, dest):
    """Create a transactionally-consistent copy of *src* at *dest* and
    return integrity information about the copy."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    src_conn = sqlite3.connect(f"file:{os.path.abspath(src)}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src_conn.backup(out)
        finally:
            out.close()
    finally:
        src_conn.close()

    chk = sqlite3.connect(dest)
    chk.row_factory = sqlite3.Row
    try:
        info = {
            "integrity_check": [r[0] for r in chk.execute("PRAGMA integrity_check")],
            "foreign_key_violations": [
                dict(r) for r in chk.execute("PRAGMA foreign_key_check")],
            "table_counts": {
                n: chk.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
                for (n,) in chk.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")},
            "bytes": os.path.getsize(dest),
            "sha256": _sha256(dest),
        }
    finally:
        chk.close()
    return info


# --------------------------------------------------------------------------
# staging / hashing
# --------------------------------------------------------------------------

IGNORE_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv", "venvs",
               ".mypy_cache", ".ruff_cache"}
IGNORE_SUFFIXES = (".pyc", ".pyo")


def _skip_dir(dirpath, name):
    """True for directories that must never enter a bundle."""
    if name in IGNORE_DIRS:
        return True
    return os.path.exists(os.path.join(dirpath, name, "pyvenv.cfg"))


def _ignore(dirpath, names):
    """copytree ignore callback: venvs, caches and nested secret files."""
    out = []
    for n in names:
        if (_skip_dir(dirpath, n) or n.endswith(IGNORE_SUFFIXES)
                or n == "secrets.ini"):
            out.append(n)
    return out


def copy_item(src_root, item, dst_root):
    """Copy one relative item from src_root into dst_root.  Returns True
    when something was copied."""
    src = os.path.join(src_root, item)
    dst = os.path.join(dst_root, item)
    if not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return True


def hash_tree(root):
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(dirpath, d))
        for fn in sorted(filenames):
            if fn.endswith(IGNORE_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            entries.append({"path": rel, "size": os.path.getsize(full),
                            "sha256": _sha256(full)})
    return sorted(entries, key=lambda e: e["path"])


def verify_tree(root, entries):
    """Return a list of problems comparing *entries* to the files under
    *root*."""
    problems = []
    seen = set()
    for e in entries:
        full = os.path.join(root, e["path"])
        if not os.path.isfile(full):
            problems.append(f"missing: {e['path']}")
            continue
        seen.add(e["path"])
        if os.path.getsize(full) != e["size"]:
            problems.append(f"size mismatch: {e['path']}")
        elif _sha256(full) != e["sha256"]:
            problems.append(f"checksum mismatch: {e['path']}")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skip_dir(dirpath, d)]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if rel not in seen:
                problems.append(f"unexpected file: {rel}")
    return problems


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------

def app_version(app_dir):
    path = os.path.join(app_dir, "dtsu666_tou", "config.py")
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "unknown"


def dep_info(app_dir):
    info = {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
    }
    req = os.path.join(app_dir, "requirements.txt")
    if os.path.exists(req):
        with open(req) as fh:
            info["requirements"] = [ln.strip() for ln in fh
                                    if ln.strip() and not ln.startswith("#")]
    return info


def restore_instructions(bundle_name):
    return (
        "How to restore this bundle\n"
        "==========================\n"
        "1. Verify it first (no changes to the live system):\n"
        f"     dtsu666-verify-backup {bundle_name}\n"
        "2. Stop the service so nothing writes to the database:\n"
        "     sudo systemctl stop dtsu666\n"
        "3. Restore (a safety copy of the current database is kept):\n"
        f"     dtsu666-restore {bundle_name}\n"
        "4. Start the service and check the journal:\n"
        "     sudo systemctl start dtsu666\n"
        "     journalctl -u dtsu666 -n 50\n"
        "The database inside the bundle is at db/dtsu666_energy.db and has\n"
        "been integrity-checked at backup time.\n"
    )


def build_manifest(stage, *, app_dir, db_path, db_info, includes_secrets,
                   files, extra=None):
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": _iso_now(),
        "hostname": os.uname().nodename,
        "source_app_dir": os.path.abspath(app_dir),
        "source_db": os.path.abspath(db_path),
        "app_version": app_version(app_dir),
        "dependencies": dep_info(app_dir),
        "includes_secrets": bool(includes_secrets),
        "database": dict(db_info, snapshot_path="db/dtsu666_energy.db"),
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
        "restore_instructions": restore_instructions(
            "dtsu666-backup-<timestamp>.tar.gz"),
        "credentials_note": (
            "Credential VALUES are never written to this manifest; only the "
            "presence of secrets.ini is recorded."),
    }
    if extra:
        manifest.update(extra)
    return manifest


def _iso_now():
    return _now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# create + verify
# --------------------------------------------------------------------------

def create_backup(*, app_dir, db_path, dest, secrets_path, tariffs_path,
                  include_secrets=True, audit_dir=None):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    os.makedirs(dest, exist_ok=True)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    base = f"dtsu666-backup-{stamp}"
    tmp = tempfile.mkdtemp(prefix="dtsu666-backup-")
    try:
        stage = os.path.join(tmp, base)
        os.makedirs(stage)

        db_info = snapshot_db(db_path,
                              os.path.join(stage, "db", "dtsu666_energy.db"))

        includes_secrets = False
        cfg_dst = os.path.join(stage, "config")
        if include_secrets and os.path.exists(secrets_path):
            os.makedirs(cfg_dst, exist_ok=True)
            target = os.path.join(cfg_dst, "secrets.ini")
            shutil.copy2(secrets_path, target)
            os.chmod(target, 0o600)
            includes_secrets = True
        if os.path.exists(tariffs_path):
            os.makedirs(cfg_dst, exist_ok=True)
            shutil.copy2(tariffs_path, os.path.join(cfg_dst, "tariffs.ini"))

        for item in APP_ITEMS:
            copy_item(app_dir, item, os.path.join(stage, "app"))
        for item in APP_ITEM_OPTIONAL:
            copy_item(app_dir, item, os.path.join(stage, "app"))

        if audit_dir and os.path.isdir(audit_dir):
            rdst = os.path.join(stage, "reports")
            os.makedirs(rdst, exist_ok=True)
            for fn in sorted(f for f in os.listdir(audit_dir)
                             if f.startswith("audit-"))[-10:]:
                shutil.copy2(os.path.join(audit_dir, fn),
                             os.path.join(rdst, fn))

        with open(os.path.join(stage, "VERSION"), "w") as fh:
            fh.write(f"app={app_version(app_dir)}\n")
            fh.write(f"bundle_version={BUNDLE_VERSION}\n")
            fh.write(f"created_at={_iso_now()}\n")

        files = hash_tree(stage)
        manifest = build_manifest(stage, app_dir=app_dir, db_path=db_path,
                                  db_info=db_info,
                                  includes_secrets=includes_secrets,
                                  files=files)
        with open(os.path.join(stage, "manifest.json"), "w") as fh:
            json.dump(manifest, fh, indent=2)

        bundle = os.path.join(dest, base + ".tar.gz")
        # Never overwrite a bundle: two backups inside the same wall-clock
        # second would otherwise silently collide.
        suffix = 1
        while os.path.exists(bundle):
            suffix += 1
            bundle = os.path.join(dest, f"{base}-{suffix}.tar.gz")
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(stage, arcname=base)
        os.chmod(bundle, 0o600)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    problems, _ = verify_bundle(bundle)
    return bundle, manifest, problems


def verify_bundle(bundle):
    """Verify checksums and database integrity of *bundle*.

    Returns ``(problems, manifest)``.  Never modifies the live system.
    """
    if not os.path.exists(bundle):
        return [f"bundle not found: {bundle}"], None
    problems = []
    tmp = tempfile.mkdtemp(prefix="dtsu666-verify-")
    try:
        with tarfile.open(bundle, "r:gz") as tf:
            names = tf.getnames()
            if not names:
                return ["empty archive"], None
            tops = sorted({n.split("/")[0] for n in names})
            if len(tops) != 1:
                return [f"expected one top-level directory, found {tops}"], None
            top = tops[0]
            manifest_name = f"{top}/manifest.json"
            if manifest_name not in names:
                return ["manifest.json missing from bundle"], None
            manifest = json.load(tf.extractfile(manifest_name))
            tf.extractall(tmp, filter="data")

        root = os.path.join(tmp, top)
        problems = [p for p in verify_tree(root, manifest["files"])
                    if p != "unexpected file: manifest.json"]

        db_rel = manifest["database"]["snapshot_path"]
        db_file = os.path.join(root, db_rel)
        if not os.path.isfile(db_file):
            problems.append(f"database snapshot missing: {db_rel}")
        else:
            try:
                c = sqlite3.connect(db_file)
                ic = [r[0] for r in c.execute("PRAGMA integrity_check")]
                c.close()
                if ic != ["ok"]:
                    problems.append(f"database integrity_check: {ic}")
            except sqlite3.Error as exc:
                problems.append(f"database not readable: {exc}")
        return problems, manifest
    except (tarfile.TarError, OSError, ValueError, KeyError) as exc:
        return [f"cannot read bundle: {exc}"], None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def prune_bundles(dest, keep):
    """Delete the oldest bundles in *dest*, keeping the newest *keep*."""
    if keep is None or keep < 0:
        return []
    bundles = sorted(
        (os.path.join(dest, f) for f in os.listdir(dest)
         if f.startswith("dtsu666-backup-") and f.endswith(".tar.gz")),
        key=lambda p: os.path.getmtime(p))
    removed = []
    while len(bundles) > keep:
        old = bundles.pop(0)
        os.remove(old)
        removed.append(old)
    return removed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _resolve(app_dir, path):
    return path if os.path.isabs(path) else os.path.join(app_dir, path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dtsu666-backup",
        description="Create or verify a consistent backup bundle of the "
                    "DTSU666 energy logger.")
    ap.add_argument("--app-dir", default=None,
                    help="application directory (default: current directory)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--secrets", default=DEFAULT_SECRETS)
    ap.add_argument("--tariffs", default=DEFAULT_TARIFFS)
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--audit-dir", default="audits")
    ap.add_argument("--keep", type=int, default=10,
                    help="keep only the newest N bundles (-1 disables)")
    ap.add_argument("--no-secrets", action="store_true",
                    help="do not include secrets.ini in the bundle")
    ap.add_argument("--verify-only", metavar="BUNDLE",
                    help="verify an existing bundle and exit")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.verify_only:
        problems, manifest = verify_bundle(args.verify_only)
        ok = not problems
        if args.json:
            print(json.dumps({"bundle": args.verify_only, "ok": ok,
                              "problems": problems, "manifest": manifest},
                             indent=2, default=str))
        else:
            print(f"bundle : {args.verify_only}")
            if manifest:
                print(f"created: {manifest['created_at']}")
                print(f"app    : {manifest['app_version']} "
                      f"files={manifest['file_count']}")
                print(f"tables : {manifest['database']['table_counts']}")
            print("result : " + ("OK" if ok else "FAILED"))
            for p in problems:
                print("  - " + p)
        return 0 if ok else 1

    app_dir = os.path.abspath(args.app_dir or os.getcwd())
    db_path = _resolve(app_dir, args.db)
    secrets_path = _resolve(app_dir, args.secrets)
    tariffs_path = _resolve(app_dir, args.tariffs)
    audit_dir = _resolve(app_dir, args.audit_dir)
    dest = _resolve(app_dir, args.dest)

    try:
        bundle, manifest, problems = create_backup(
            app_dir=app_dir, db_path=db_path, dest=dest,
            secrets_path=secrets_path, tariffs_path=tariffs_path,
            include_secrets=not args.no_secrets, audit_dir=audit_dir)
    except (FileNotFoundError, sqlite3.Error, OSError, tarfile.TarError) as exc:
        print(f"ERROR: backup failed: {exc}", file=sys.stderr)
        return 2

    removed = []
    if not problems:
        removed = prune_bundles(dest, args.keep)

    summary = {
        "bundle": bundle,
        "created_at": manifest["created_at"],
        "app_version": manifest["app_version"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "bundle_bytes": os.path.getsize(bundle),
        "database": manifest["database"]["table_counts"],
        "db_sha256": manifest["database"]["sha256"],
        "includes_secrets": manifest["includes_secrets"],
        "verified": not problems,
        "problems": problems,
        "pruned": removed,
    }
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"bundle : {bundle}")
        print(f"size   : {_human(os.path.getsize(bundle))} "
              f"({manifest['file_count']} files)")
        print(f"app    : {manifest['app_version']}")
        print(f"secrets: {'included' if manifest['includes_secrets'] else 'excluded'}")
        print(f"tables : {manifest['database']['table_counts']}")
        print(f"db sha : {manifest['database']['sha256']}")
        print("verify : " + ("OK" if not problems else "FAILED"))
        for p in problems:
            print("  - " + p)
        for r in removed:
            print(f"pruned : {os.path.basename(r)}")

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
