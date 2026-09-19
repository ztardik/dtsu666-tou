"""Tests for the on-demand read-only web status page."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import timedelta

from dtsu666_tou import config, database, web
from dtsu666_tou.time_utils import now_local


def _seed_db(path):
    """Create a database with one meter and two readings (today, midday)."""
    db = database.open_database(path)
    mid = database.create_initial_meter(db, 1, 50000.0)
    meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
    ts = now_local().replace(hour=12, minute=0, second=0, microsecond=0)
    database.record_energy(db, meter, ts, 50000.0)
    database.record_energy(db, meter, ts + timedelta(minutes=1), 50000.05)
    db.close()


def test_build_status_shape(tmp_path):
    db_path = tmp_path / "dtsu666_energy.db"
    _seed_db(db_path)
    db = web.open_readonly(str(db_path))
    try:
        st = web.build_status(db, str(tmp_path / "secrets.ini"),
                              str(tmp_path / "tariffs.ini"))
    finally:
        db.close()

    assert st["active_meter_id"] is not None
    assert st["operational"]["status"] in (
        "disconnected", "acquiring", "ready", "missing", "active")
    assert st["energy"]["absolute_kwh"] == 50000.05
    assert abs(st["energy"]["last_delta_kwh"] - 0.05) < 1e-9
    assert st["energy"]["last_tariff"] in ("VT", "NT")
    assert abs(st["periods"]["current_day"]["total_kwh"] - 0.05) < 1e-6
    assert st["cost"] is None                      # no tariffs.ini
    assert len(st["recent_readings"]) == 2
    assert len(st["meters"]) == 1
    assert st["config"]["freshness_seconds"] > 0


def test_build_status_with_tariffs(tmp_path):
    db_path = tmp_path / "dtsu666_energy.db"
    _seed_db(db_path)
    tariffs_path = tmp_path / "tariffs.ini"
    tariffs_path.write_text(
        "[COST]\n"
        "energija_vt = 0.1\n"
        "energija_nt = 0.05\n"
        "prijenos_vt = 0.02\n"
        "prijenos_nt = 0.01\n"
        "distribucija_vt = 0.03\n"
        "distribucija_nt = 0.02\n"
        "oie_kwh = 0.01\n"
        "opskrba_month = 0.982\n"
        "mjerno_mjesto_month = 1.983\n"
        "vat_percent = 13\n"
    )
    db = web.open_readonly(str(db_path))
    try:
        st = web.build_status(db, str(tmp_path / "secrets.ini"),
                              str(tariffs_path))
    finally:
        db.close()

    assert st["config"]["tariffs_loaded"] is True
    assert st["cost"] is not None
    assert set(st["cost"]) == {"current_day", "current_month"}
    assert st["cost"]["current_day"]["total"] >= 0
    assert st["cost"]["current_month"]["total_kwh"] == 0.05


def test_open_readonly_missing_db_raises(tmp_path):
    try:
        web.open_readonly(str(tmp_path / "missing.db"))
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("expected missing database to raise")


def test_open_readonly_wal_without_write_permission(tmp_path):
    # A cleanly closed WAL database has no -shm file, so a read-only open
    # from a directory the process cannot write to falls back to immutable.
    import os
    db_path = tmp_path / "dtsu666_energy.db"
    _seed_db(db_path)
    os.chmod(tmp_path, 0o555)
    try:
        db = web.open_readonly(str(db_path))
        try:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM readings").fetchone()
            assert row["n"] == 2
        finally:
            db.close()
    finally:
        os.chmod(tmp_path, 0o755)


def _start_server(tmp_path, db_path):
    server = web._Server(("127.0.0.1", 0), str(db_path),
                         str(tmp_path / "secrets.ini"),
                         str(tmp_path / "tariffs.ini"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_http_endpoints(tmp_path):
    db_path = tmp_path / "dtsu666_energy.db"
    _seed_db(db_path)
    server = _start_server(tmp_path, db_path)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["active_meter_id"] is not None
            assert data["operational"]["status"]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "CHINT DTSU666" in body
            assert "50000.050" in body
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("expected 404 for unknown path")
    finally:
        server.shutdown()
        server.server_close()


def test_http_missing_db_returns_error_json(tmp_path):
    server = _start_server(tmp_path, tmp_path / "missing.db")
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
            assert "error" in data
    finally:
        server.shutdown()
        server.server_close()
