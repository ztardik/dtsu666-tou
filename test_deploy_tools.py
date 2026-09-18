"""Tests for the operational tools: dtsu666-audit, -backup, -restore.

Everything runs against temporary files.  The production database, the
configuration and the installed service are never touched.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta

import pytest

from dtsu666_tou import database
from dtsu666_tou.time_utils import now_local

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.join(HERE, "deploy")
LOCAL_TZ = database.config.LOCAL_TZ


def _load(module_name, filename):
    """Load a deploy/ tool by path (they are not importable packages)."""
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(DEPLOY, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod          # dtsu666_restore imports backup
    spec.loader.exec_module(mod)
    return mod


audit_tool = _load("dtsu666_audit", "dtsu666_audit.py")
backup_tool = _load("dtsu666_backup", "dtsu666_backup.py")
restore_tool = _load("dtsu666_restore", "dtsu666_restore.py")


# ======================================================================
# helpers
# ======================================================================

def _make_db(path, *, minutes=20):
    """Create a small, realistic database and return (db, meter)."""
    db = database.open_database(path)
    mid = database.create_initial_meter(db, 1, 1000.0)
    meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
    start = datetime(2026, 9, 1, 0, 0, tzinfo=LOCAL_TZ)
    kwh = 1000.0
    for _ in range(minutes):
        start = start + timedelta(minutes=1)
        kwh += 0.01
        database.record_energy(db, meter, start, kwh)
    db.commit()
    return db, meter


def _write_ini(path, password="s3cr3t-value"):
    with open(path, "w") as fh:
        fh.write(
            "[SERIAL]\nport = /dev/ttyUSB0\nbaudrate = 9600\n"
            "timeout = 1.0\naddress = 1\n\n"
            f"[MQTT]\nhost = 10.0.0.1\nport = 1883\nusername = user\n"
            f"password = {password}\n\n[HEP Correction]\n"
            "1 = 2026-09-01 00:10, 1000.0, 0.0, test\n"
        )
    return path


def _write_tariffs(path):
    with open(path, "w") as fh:
        fh.write("[COST]\nenergija_vt = 0.09\nvat_percent = 13\n")
    return path


# ======================================================================
# audit tool
# ======================================================================

class TestAuditTool:
    def test_clean_database_has_no_errors(self, tmp_path):
        db_path = str(tmp_path / "clean.db")
        db, _meter = _make_db(db_path)
        db.close()
        rc = audit_tool.main([
            "--db", db_path, "--secrets", str(tmp_path / "none.ini"),
            "--tariffs", str(tmp_path / "none.ini"),
            "--out-dir", str(tmp_path / "audits"), "--json"])
        assert rc == 0

    def test_report_files_are_written(self, tmp_path):
        db_path = str(tmp_path / "clean.db")
        db, _meter = _make_db(db_path)
        db.close()
        out_dir = tmp_path / "audits"
        rc = audit_tool.main([
            "--db", db_path, "--secrets", str(tmp_path / "none.ini"),
            "--tariffs", str(tmp_path / "none.ini"),
            "--out-dir", str(out_dir)])
        assert rc == 0
        names = sorted(p.name for p in out_dir.iterdir())
        assert any(n.startswith("audit-") and n.endswith(".json") for n in names)
        assert any(n.endswith("-summary.md") for n in names)
        md = next(p for p in out_dir.iterdir() if p.suffix == ".md")
        assert "## Findings" in md.read_text()

    def test_detects_duplicate_and_non_monotonic_timestamps(self, tmp_path):
        db_path = str(tmp_path / "dup.db")
        db, meter = _make_db(db_path)
        ts = "2026-09-01T00:00:30+02:00"
        for _ in range(2):
            db.execute(
                "INSERT INTO readings (meter_id, timestamp, absolute_kwh, "
                "delta_kwh, tariff, vt_kwh, nt_kwh, allocation_method) "
                "VALUES (?, ?, 1000.5, 0.0, 'VT', 0.0, 0.0, 'measured')",
                (meter["id"], ts))
        db.commit()
        res = audit_tool.reading_integrity(db, meter["id"], 180.0)
        db.close()
        assert res["duplicate_timestamps"], "duplicates must be reported"
        assert res["non_monotonic"], "time going backwards must be reported"

    def test_detects_counter_decrease(self, tmp_path):
        db_path = str(tmp_path / "dec.db")
        db, meter = _make_db(db_path)
        db.execute(
            "INSERT INTO readings (meter_id, timestamp, absolute_kwh, "
            "delta_kwh, tariff, vt_kwh, nt_kwh, allocation_method) "
            "VALUES (?, '2026-09-01T23:00:00+02:00', 5.0, 0.0, 'VT', "
            "0.0, 0.0, 'measured')", (meter["id"],))
        db.commit()
        res = audit_tool.reading_integrity(db, meter["id"], 180.0)
        db.close()
        assert res["counter_decreases"]

    def test_detects_aggregate_mismatch(self, tmp_path):
        db_path = str(tmp_path / "agg.db")
        db, meter = _make_db(db_path)
        db.execute(
            "UPDATE energy_daily SET total_kwh = total_kwh + 5 WHERE meter_id=?",
            (meter["id"],))
        db.commit()
        agg = audit_tool.aggregate_consistency(db, meter["id"])
        db.close()
        assert agg["mismatch_count"] >= 1
        assert any(m["issue"] == "aggregate mismatch" for m in agg["mismatches"])

    def test_aggregate_consistency_is_clean_for_a_normal_database(self,
                                                                 tmp_path):
        db_path = str(tmp_path / "ok.db")
        db, meter = _make_db(db_path)
        agg = audit_tool.aggregate_consistency(db, meter["id"])
        db.close()
        assert agg["mismatch_count"] == 0
        assert agg["days_with_readings"] >= 1

    def test_credentials_never_appear_in_the_report(self, tmp_path, capsys):
        db_path = str(tmp_path / "cred.db")
        db, _meter = _make_db(db_path)
        db.close()
        secrets = _write_ini(tmp_path / "secrets.ini")
        tariffs = _write_tariffs(tmp_path / "tariffs.ini")
        rc = audit_tool.main([
            "--db", db_path, "--secrets", str(secrets),
            "--tariffs", str(tariffs), "--out-dir", str(tmp_path / "audits"),
            "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "s3cr3t-value" not in out
        assert '"password_set": true' in out

    def test_readonly_never_modifies_the_database(self, tmp_path):
        db_path = str(tmp_path / "ro.db")
        db, _meter = _make_db(db_path)
        db.close()
        before = backup_tool._sha256(db_path)
        audit_tool.main(["--db", db_path, "--no-write",
                         "--secrets", str(tmp_path / "none.ini"),
                         "--tariffs", str(tmp_path / "none.ini")])
        assert backup_tool._sha256(db_path) == before

    def test_missing_database_exits_with_2(self, tmp_path):
        rc = audit_tool.main(["--db", str(tmp_path / "absent.db"),
                              "--no-write"])
        assert rc == 2


# ======================================================================
# shared fixtures for the backup / restore tools
# ======================================================================

@pytest.fixture
def app_dir(tmp_path):
    """A minimal application tree holding the files the tools copy."""
    d = tmp_path / "app"
    (d / "dtsu666_tou").mkdir(parents=True)
    (d / "dtsu666_tou" / "config.py").write_text('__version__ = "0.9.0"\n')
    (d / "dtsu666_tou" / "app.py").write_text("# payload\n")
    (d / "deploy").mkdir()
    (d / "deploy" / "dtsu666_audit.py").write_text("# tool\n")
    (d / "README.md").write_text("# readme\n")
    (d / "requirements.txt").write_text("paho-mqtt>=2.0.0\n")
    return d


def _backup(app_dir, work, **kwargs):
    db_path = str(work / "dtsu666_energy.db")
    db, _meter = _make_db(db_path)
    db.close()
    secrets = _write_ini(work / "secrets.ini")
    tariffs = _write_tariffs(work / "tariffs.ini")
    bundle, manifest, problems = backup_tool.create_backup(
        app_dir=str(app_dir), db_path=db_path, dest=str(work / "backups"),
        secrets_path=secrets, tariffs_path=tariffs, **kwargs)
    return bundle, manifest, problems


# ======================================================================
# backup tool
# ======================================================================

class TestBackupTool:
    def test_bundle_is_created_and_verifies(self, tmp_path, app_dir):
        bundle, manifest, problems = _backup(app_dir, tmp_path)
        assert problems == []
        assert os.path.isfile(bundle)
        assert os.stat(bundle).st_mode & 0o777 == 0o600

        problems, verified = backup_tool.verify_bundle(bundle)
        assert problems == []
        assert verified["file_count"] == len(verified["files"])

    def test_manifest_records_every_file(self, tmp_path, app_dir):
        bundle, manifest, _problems = _backup(app_dir, tmp_path)
        with tarfile.open(bundle) as tf:
            names = {m.name.split("/", 1)[1] for m in tf.getmembers()
                     if m.isfile() and "/" in m.name}
        listed = {f["path"] for f in manifest["files"]}
        assert listed | {"manifest.json"} == names

    def test_credentials_are_stored_but_never_recorded(self, tmp_path, app_dir):
        bundle, manifest, _problems = _backup(app_dir, tmp_path)
        assert manifest["includes_secrets"] is True
        assert "s3cr3t-value" not in json.dumps(manifest)
        with tarfile.open(bundle) as tf:
            top = tf.getnames()[0].split("/")[0]
            member = tf.getmember(f"{top}/config/secrets.ini")
            assert member.mode & 0o777 == 0o600
            assert b"s3cr3t-value" in tf.extractfile(member).read()

    def test_no_secrets_option(self, tmp_path, app_dir):
        bundle, manifest, problems = _backup(app_dir, tmp_path,
                                            include_secrets=False)
        assert problems == []
        assert manifest["includes_secrets"] is False
        with tarfile.open(bundle) as tf:
            assert not any(n.endswith("config/secrets.ini")
                           for n in tf.getnames())

    def test_database_snapshot_is_consistent(self, tmp_path, app_dir):
        bundle, manifest, _problems = _backup(app_dir, tmp_path)
        out = tmp_path / "extract"
        out.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(out, filter="data")
        top = os.listdir(out)[0]
        snapshot = out / top / "db" / "dtsu666_energy.db"
        conn = sqlite3.connect(str(snapshot))
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 20
        conn.close()
        assert manifest["database"]["integrity_check"] == ["ok"]
        assert manifest["database"]["snapshot_path"] == "db/dtsu666_energy.db"

    def test_verify_detects_a_modified_file(self, tmp_path, app_dir):
        bundle, _manifest, _problems = _backup(app_dir, tmp_path)
        extract = tmp_path / "tampered"
        extract.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(extract, filter="data")
        top = os.listdir(extract)[0]
        with open(extract / top / "db" / "dtsu666_energy.db", "ab") as fh:
            fh.write(b"tampered")
        broken = str(tmp_path / "tampered.tar.gz")
        with tarfile.open(broken, "w:gz") as tf:
            tf.add(extract / top, arcname=top)
        problems, _mf = backup_tool.verify_bundle(broken)
        assert any(("size mismatch" in p) or ("checksum mismatch" in p)
                   for p in problems)

    def test_verify_detects_a_missing_bundle(self, tmp_path):
        problems, manifest = backup_tool.verify_bundle(
            str(tmp_path / "absent.tar.gz"))
        assert manifest is None
        assert problems

    def test_prune_keeps_the_newest_bundles(self, tmp_path, app_dir):
        dest = tmp_path / "backups"
        db_path = tmp_path / "dtsu666_energy.db"
        db, _meter = _make_db(str(db_path))
        db.close()
        secrets = _write_ini(tmp_path / "secrets.ini")
        tariffs = _write_tariffs(tmp_path / "tariffs.ini")
        for i in range(3):
            bundle, _mf, _p = backup_tool.create_backup(
                app_dir=str(app_dir), db_path=str(db_path), dest=str(dest),
                secrets_path=secrets, tariffs_path=tariffs,
                include_secrets=False)
            os.utime(bundle, (1000 + i, 1000 + i))
        removed = backup_tool.prune_bundles(str(dest), 2)
        assert len(removed) == 1
        assert len(list(dest.glob("*.tar.gz"))) == 2

    def test_venvs_and_nested_secrets_are_excluded(self, tmp_path, app_dir):
        venv = app_dir / "dtsu666_tou" / "stray-venv"
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (venv / "big.so").write_text("x" * 100)
        (app_dir / "dtsu666_tou" / "secrets.ini").write_text("password = leak\n")
        bundle, _manifest, problems = _backup(app_dir, tmp_path)
        assert problems == []
        with tarfile.open(bundle) as tf:
            names = tf.getnames()
        assert not any("pyvenv.cfg" in n for n in names)
        assert not any("big.so" in n for n in names)
        assert not any(n.endswith("dtsu666_tou/secrets.ini") for n in names)

    def test_cli_reports_ok_and_returns_zero(self, tmp_path, app_dir, capsys):
        db, _meter = _make_db(str(app_dir / "dtsu666_energy.db"))
        db.close()
        _write_ini(app_dir / "secrets.ini")
        _write_tariffs(app_dir / "tariffs.ini")
        rc = backup_tool.main([
            "--app-dir", str(app_dir), "--db", "dtsu666_energy.db",
            "--secrets", "secrets.ini", "--tariffs", "tariffs.ini",
            "--dest", "backups", "--audit-dir", "audits", "--keep", "3"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "verify : OK" in out
        assert "secrets: included" in out


# ======================================================================
# restore tool
# ======================================================================

@pytest.fixture
def no_service(monkeypatch):
    """Make the service-state probe deterministic."""
    monkeypatch.setattr(restore_tool, "service_active", lambda name=None: False)


class TestRestoreTool:
    def test_verify_only_does_not_write(self, tmp_path, app_dir, no_service):
        bundle, _mf, _p = _backup(app_dir, tmp_path)
        target = tmp_path / "restored.db"
        rc = restore_tool.main([bundle, "--verify-only",
                                "--target", str(target)])
        assert rc == 0
        assert not target.exists()

    def test_restore_produces_the_exact_snapshot(self, tmp_path, app_dir,
                                                 no_service):
        bundle, manifest, _p = _backup(app_dir, tmp_path)
        target = tmp_path / "out" / "dtsu666_energy.db"
        rc = restore_tool.main([bundle, "--target", str(target)])
        assert rc == 0
        assert backup_tool._sha256(str(target)) == \
            manifest["database"]["sha256"]
        assert os.stat(target).st_mode & 0o777 == 0o600

    def test_existing_target_requires_force(self, tmp_path, app_dir,
                                           no_service, capsys):
        bundle, _mf, _p = _backup(app_dir, tmp_path)
        target = tmp_path / "restored.db"
        target.write_bytes(b"existing")
        rc = restore_tool.main([bundle, "--target", str(target)])
        assert rc == 2
        assert "already exists" in capsys.readouterr().err
        assert target.read_bytes() == b"existing"

    def test_force_keeps_a_safety_copy(self, tmp_path, app_dir, no_service):
        bundle, manifest, _p = _backup(app_dir, tmp_path)
        target = tmp_path / "restored.db"
        db, _meter = _make_db(str(target))
        db.close()
        original_sha = backup_tool._sha256(str(target))
        rc = restore_tool.main([bundle, "--target", str(target), "--force"])
        assert rc == 0
        copies = list(tmp_path.glob("restored.db.pre-restore-*"))
        assert len(copies) == 1
        assert backup_tool._sha256(str(copies[0])) == original_sha
        assert backup_tool._sha256(str(target)) == \
            manifest["database"]["sha256"]

    def test_stale_wal_files_are_moved_aside(self, tmp_path, app_dir,
                                            no_service):
        bundle, _mf, _p = _backup(app_dir, tmp_path)
        target = tmp_path / "restored.db"
        target.write_bytes(b"stale")
        (tmp_path / "restored.db-wal").write_bytes(b"stale wal")
        (tmp_path / "restored.db-shm").write_bytes(b"stale shm")
        rc = restore_tool.main([bundle, "--target", str(target), "--force",
                                "--no-backup-current"])
        assert rc == 0
        assert not (tmp_path / "restored.db-wal").exists()
        assert not (tmp_path / "restored.db-shm").exists()
        assert list(tmp_path.glob("restored.db-wal.pre-restore-*"))
        assert list(tmp_path.glob("restored.db-shm.pre-restore-*"))

    def test_tampered_bundle_is_refused(self, tmp_path, app_dir, no_service,
                                        capsys):
        bundle, _mf, _p = _backup(app_dir, tmp_path)
        extract = tmp_path / "tamper"
        extract.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(extract, filter="data")
        top = os.listdir(extract)[0]
        with open(extract / top / "config" / "tariffs.ini", "a") as fh:
            fh.write("oops = 1\n")
        broken = str(tmp_path / "bad.tar.gz")
        with tarfile.open(broken, "w:gz") as tf:
            tf.add(extract / top, arcname=top)
        target = tmp_path / "restored.db"
        rc = restore_tool.main([broken, "--target", str(target)])
        assert rc == 1
        assert not target.exists()
        assert "VERIFICATION FAILED" in capsys.readouterr().err

    def test_dry_run_writes_nothing(self, tmp_path, app_dir, no_service):
        bundle, _mf, _p = _backup(app_dir, tmp_path)
        target = tmp_path / "restored.db"
        rc = restore_tool.main([bundle, "--target", str(target), "--dry-run"])
        assert rc == 0
        assert not target.exists()

    def test_restore_config_writes_secrets(self, tmp_path, app_dir,
                                           no_service):
        bundle, _mf, _p = _backup(app_dir, tmp_path)
        dest = tmp_path / "app2"
        dest.mkdir()
        target = dest / "dtsu666_energy.db"
        rc = restore_tool.main([
            bundle, "--target", str(target), "--app-dir", str(dest),
            "--secrets", "secrets.ini", "--tariffs", "tariffs.ini",
            "--restore-config"])
        assert rc == 0
        restored = dest / "secrets.ini"
        assert restored.read_text().count("s3cr3t-value") == 1
        assert os.stat(restored).st_mode & 0o777 == 0o600

    def test_round_trip_preserves_the_rows(self, tmp_path, app_dir,
                                          no_service):
        bundle, manifest, _p = _backup(app_dir, tmp_path)
        target = tmp_path / "roundtrip.db"
        assert restore_tool.main([bundle, "--target", str(target)]) == 0
        conn = sqlite3.connect(str(target))
        rows = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        days = conn.execute("SELECT COUNT(*) FROM energy_daily").fetchone()[0]
        conn.close()
        assert rows == manifest["database"]["table_counts"]["readings"]
        assert days == manifest["database"]["table_counts"]["energy_daily"]
