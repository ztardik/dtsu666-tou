"""Tests for the production-hardening changes.

Covers: configurable runtime settings (dtsu666.conf), serial auto-recovery,
database busy-retry, sd_notify/watchdog, structured logging, runtime info
publishing, and the supervised acquisition loop.

Every test uses temporary files only - the production database and
configuration are never touched.
"""

import io
import logging
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

import pytest

from dtsu666_tou import (
    config, database, logging_utils, modbus, publishing, runtime, watchdog,
)
from dtsu666_tou.time_utils import now_local

LOCAL_TZ = config.LOCAL_TZ


# ======================================================================
# helpers
# ======================================================================

@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="hardening_")
    os.close(fd)
    db = database.open_database(path)
    mid = database.create_initial_meter(db, 1, 1000.0)
    meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
    try:
        yield db, meter, path
    finally:
        db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def write_conf(text):
    fd, path = tempfile.mkstemp(suffix=".conf", prefix="dtsu_conf_")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


@pytest.fixture(autouse=True)
def restore_runtime_config():
    """Never let one test leak runtime configuration into another."""
    yield
    config.reset_runtime_config()


# ======================================================================
# runtime configuration (dtsu666.conf)
# ======================================================================

class TestRuntimeConfig:
    def test_defaults_when_file_is_absent(self):
        config.reset_runtime_config()
        assert config.freshness_seconds() == 240.0
        assert config.availability_suffix() == "availability"
        assert config.payload_available() == "online"
        assert config.payload_not_available() == "offline"
        assert config.publish_runtime_info() is True
        assert config.watchdog_interval() == 0.0
        assert config.retention_minute_days() == 365
        assert config.retention_daily_days() == 3650
        assert config.db_busy_timeout_ms() == 5000
        assert config.serial_reopen_after_failures() == 3

    def test_file_overrides_defaults(self):
        path = write_conf(
            "[MONITORING]\nfreshness_seconds = 100\n"
            "availability_suffix = avail\npayload_available = up\n"
            "payload_not_available = down\npublish_runtime_info = false\n"
            "[WATCHDOG]\ninterval_seconds = 45\n"
            "[RETENTION]\nminute_days = 10\ndaily_days = 20\n"
            "[SERIAL]\nreopen_after_failures = 7\nreopen_delay_seconds = 1.5\n"
        )
        try:
            config.load_runtime_config(path)
            assert config.freshness_seconds() == 100.0
            assert config.availability_suffix() == "avail"
            assert config.payload_available() == "up"
            assert config.payload_not_available() == "down"
            assert config.publish_runtime_info() is False
            assert config.watchdog_interval() == 45.0
            assert config.retention_minute_days() == 10
            assert config.retention_daily_days() == 20
            assert config.serial_reopen_after_failures() == 7
            assert config.serial_reopen_delay() == 1.5
        finally:
            os.unlink(path)

    def test_malformed_values_fall_back_to_defaults(self):
        path = write_conf("[MONITORING]\nfreshness_seconds = not-a-number\n")
        try:
            config.load_runtime_config(path)
            assert config.freshness_seconds() == 240.0
        finally:
            os.unlink(path)

    def test_availability_topic(self):
        config.reset_runtime_config()
        assert config.availability_topic(1) == \
            "Electricity/dtsu666/1/availability"

    def test_freshness_window_is_honoured_by_commissioning(self, temp_db):
        from dtsu666_tou import commissioning
        db, meter, _path = temp_db
        now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=LOCAL_TZ)
        database.record_energy(db, meter, now - timedelta(seconds=120), 1000.0)

        config.reset_runtime_config()
        assert commissioning.connected(db, meter["id"], now) is True

        path = write_conf("[MONITORING]\nfreshness_seconds = 60\n")
        try:
            config.load_runtime_config(path)
            assert commissioning.connected(db, meter["id"], now) is False
        finally:
            os.unlink(path)


# ======================================================================
# serial transport recovery
# ======================================================================

class _FakeSerialPort:
    """Stand-in for pyserial's Serial, injected as the ``serial`` module."""

    EIGHTBITS = 8
    PARITY_NONE = "N"
    STOPBITS_ONE = 1
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_open = True
        self.writes = []
        self.response = b""
        _FakeSerialPort.instances.append(self)

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def read(self, count):
        return self.response


def _install_fake_serial(monkeypatch):
    _FakeSerialPort.instances = []
    fake = types.ModuleType("serial")
    fake.Serial = _FakeSerialPort
    fake.EIGHTBITS = 8
    fake.PARITY_NONE = "N"
    fake.STOPBITS_ONE = 1
    monkeypatch.setitem(sys.modules, "serial", fake)


def _frame(slave, payload):
    """Build a valid Modbus 0x03 response carrying *payload*."""
    body = bytes([slave, 0x03, len(payload)]) + payload
    return body + struct.pack("<H", modbus.crc16(body))


def _new_rtu(monkeypatch, **kwargs):
    _install_fake_serial(monkeypatch)
    kwargs.setdefault("reopen_after_failures", 3)
    kwargs.setdefault("reopen_delay", 0.0)
    kwargs.setdefault("reopen_max_delay", 0.0)
    return modbus.ModbusRTU("/dev/fake", 9600, 0.1, **kwargs)


class TestSerialRecovery:
    def test_reads_registers(self, monkeypatch):
        rtu = _new_rtu(monkeypatch)
        _FakeSerialPort.instances[-1].response = _frame(
            1, struct.pack(">HH", 0x1234, 0x5678))
        assert rtu.read_registers(1, 0x101E, 2) == [0x1234, 0x5678]
        assert rtu.consecutive_failures == 0
        # the request must still be a well-formed frame
        assert len(_FakeSerialPort.instances[-1].writes[0]) == 8

    def test_short_response_is_a_transport_failure(self, monkeypatch):
        rtu = _new_rtu(monkeypatch, reopen_after_failures=0)
        _FakeSerialPort.instances[-1].response = b"\x01\x03"
        with pytest.raises(modbus.ModbusTransportError):
            rtu.read_registers(1, 0x101E, 2)
        assert rtu.consecutive_failures == 1

    def test_crc_mismatch_is_a_transport_failure(self, monkeypatch):
        rtu = _new_rtu(monkeypatch, reopen_after_failures=0)
        bad = bytearray(_frame(1, struct.pack(">HH", 1, 2)))
        bad[-1] ^= 0xFF
        _FakeSerialPort.instances[-1].response = bytes(bad)
        with pytest.raises(modbus.ModbusTransportError, match="CRC"):
            rtu.read_registers(1, 0x101E, 2)

    def test_device_exception_is_a_protocol_error(self, monkeypatch):
        rtu = _new_rtu(monkeypatch)
        body = bytes([1, 0x83, 0x02])
        _FakeSerialPort.instances[-1].response = \
            body + struct.pack("<H", modbus.crc16(body))
        with pytest.raises(modbus.ModbusProtocolError):
            rtu.read_registers(1, 0x101E, 2)
        # a device that answers must not trigger a port re-open
        assert rtu.consecutive_failures == 0

    def test_port_is_reopened_after_the_failure_threshold(self, monkeypatch):
        rtu = _new_rtu(monkeypatch, reopen_after_failures=2)
        assert rtu.is_open() is True
        _FakeSerialPort.instances[-1].response = b""
        for _ in range(2):
            with pytest.raises(modbus.ModbusTransportError):
                rtu.read_registers(1, 0x101E, 2)
        assert rtu.reopen_count == 1
        assert len(_FakeSerialPort.instances) == 2, "a new port must be opened"
        assert rtu.is_open() is True
        assert rtu.consecutive_failures == 0

    def test_backoff_delays_the_reopen(self, monkeypatch):
        rtu = _new_rtu(monkeypatch, reopen_after_failures=1,
                       reopen_delay=3600.0, reopen_max_delay=3600.0)
        _FakeSerialPort.instances[-1].response = b""
        with pytest.raises(modbus.ModbusTransportError):
            rtu.read_registers(1, 0x101E, 2)
        # still backing off: no new port yet
        assert rtu.reopen_count == 0
        assert len(_FakeSerialPort.instances) == 1
        with pytest.raises(modbus.ModbusTransportError):
            rtu.read_registers(1, 0x101E, 2)

    def test_open_failure_is_reported_not_raised(self, monkeypatch):
        _install_fake_serial(monkeypatch)

        def boom(**kwargs):
            raise OSError("no such device")

        monkeypatch.setattr(_FakeSerialPort, "__init__", boom)
        rtu = modbus.ModbusRTU("/dev/gone", 9600, 0.1,
                               reopen_after_failures=1)
        assert rtu.is_open() is False
        with pytest.raises(modbus.ModbusTransportError):
            rtu.read_registers(1, 0x101E, 2)


# ======================================================================
# database resilience
# ======================================================================

class TestDatabaseHardening:
    def test_pragmas_are_applied(self, temp_db):
        db, _meter, _path = temp_db
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] > 0

    def test_foreign_keys_are_enforced(self, temp_db):
        db, _meter, _path = temp_db
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO readings (meter_id, timestamp, absolute_kwh, "
                "delta_kwh, tariff, vt_kwh, nt_kwh, allocation_method) "
                "VALUES (9999, ?, 1.0, 0.0, 'VT', 0.0, 0.0, 'measured')",
                (now_local().isoformat(),),
            )

    def test_busy_timeout_is_configurable(self):
        path = write_conf("[DATABASE]\nbusy_timeout_ms = 1234\n")
        fd, db_path = tempfile.mkstemp(suffix=".db", prefix="busy_")
        os.close(fd)
        try:
            config.load_runtime_config(path)
            db = database.open_database(db_path)
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
            db.close()
        finally:
            os.unlink(path)
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass

    def test_retry_on_locked_retries_then_succeeds(self):
        calls = {"n": 0}

        @database.retry_on_locked
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert flaky() == "ok"
        assert calls["n"] == 3

    def test_retry_on_locked_reraises_other_errors(self):
        calls = {"n": 0}

        @database.retry_on_locked
        def broken():
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: readings")

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            broken()
        assert calls["n"] == 1, "non-lock errors must not be retried"

    def test_retry_gives_up_after_the_configured_attempts(self):
        calls = {"n": 0}

        @database.retry_on_locked
        def always_locked():
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError):
            always_locked()
        assert calls["n"] == config.db_lock_retries() + 1

    def test_cleanup_uses_the_configured_retention(self, temp_db):
        db, meter, _path = temp_db
        old = now_local() - timedelta(days=10)
        database.record_energy(db, meter, old, 1000.0)
        assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1

        path = write_conf("[RETENTION]\nminute_days = 1\ndaily_days = 1\n")
        try:
            config.load_runtime_config(path)
            database.cleanup_database(db)
        finally:
            os.unlink(path)
        assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


# ======================================================================
# watchdog (sd_notify)
# ======================================================================

class TestWatchdog:
    def test_disabled_without_notify_socket(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        assert watchdog.enabled() is False
        assert watchdog.notify("READY=1") is False
        assert watchdog.ready() is False
        assert watchdog.watchdog_ping() is False
        assert watchdog.notify_stopping() is False

    def test_interval_from_watchdog_usec(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_USEC", "60000000")
        assert watchdog.watchdog_interval() == 60.0

    def test_interval_falls_back_to_config(self, monkeypatch):
        monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        path = write_conf("[WATCHDOG]\ninterval_seconds = 15\n")
        try:
            config.load_runtime_config(path)
            assert watchdog.watchdog_interval() == 15.0
        finally:
            os.unlink(path)

    def test_malformed_watchdog_usec_falls_back(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
        config.reset_runtime_config()
        assert watchdog.watchdog_interval() == 0.0

    def test_messages_are_delivered(self, monkeypatch, tmp_path):
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        server.settimeout(2.0)
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        try:
            assert watchdog.ready("starting up") is True
            assert server.recv(1024) == b"STATUS=starting up"
            assert server.recv(1024) == b"READY=1"
            assert watchdog.watchdog_ping() is True
            assert server.recv(1024) == b"WATCHDOG=1"
            assert watchdog.notify_stopping() is True
            assert server.recv(1024) == b"STOPPING=1"
        finally:
            server.close()

    def test_abstract_socket_name_is_translated(self, monkeypatch):
        seen = {}

        class FakeSock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def connect(self, address):
                seen["address"] = address

            def sendall(self, data):
                seen["data"] = data

        monkeypatch.setattr(watchdog.socket, "socket",
                            lambda *a, **k: FakeSock())
        monkeypatch.setenv("NOTIFY_SOCKET", "@dtsu666")
        assert watchdog.notify("WATCHDOG=1") is True
        assert seen["address"] == "\0dtsu666"
        assert seen["data"] == b"WATCHDOG=1"


# ======================================================================
# structured logging
# ======================================================================

class TestLogging:
    def test_setup_logging_uses_the_configured_level(self):
        path = write_conf("[LOGGING]\nlevel = WARNING\n")
        logger = logging.getLogger(logging_utils.LOGGER_NAME)
        logger.handlers.clear()
        try:
            config.load_runtime_config(path)
            assert logging_utils.setup_logging().level == logging.WARNING
        finally:
            os.unlink(path)
            logger.handlers.clear()

    def test_messages_carry_name_severity_and_format(self):
        logger = logging.getLogger(logging_utils.LOGGER_NAME)
        logger.handlers.clear()
        stream = io.StringIO()
        try:
            logging_utils.setup_logging(level="DEBUG", stream=stream)
            logging_utils.get_logger("scheduler").warning("hello %s", "world")
            out = stream.getvalue()
        finally:
            logger.handlers.clear()
        assert "dtsu666.scheduler" in out
        assert "WARNING" in out
        assert "hello world" in out

    def test_get_logger_children(self):
        assert logging_utils.get_logger().name == "dtsu666"
        assert logging_utils.get_logger("mqtt").name == "dtsu666.mqtt"


# ======================================================================
# runtime info publishing
# ======================================================================

class TestRuntimeInfo:
    def test_runtime_info_fields(self):
        info = publishing.runtime_info()
        assert info["version"] == config.__version__
        assert info["hostname"]
        assert info["process_id"] == os.getpid()
        assert info["uptime_seconds"] >= 0

    def test_publish_system_includes_runtime_info(self, temp_db, monkeypatch):
        from dtsu666_tou import mqtt
        db, meter, _path = temp_db
        config.reset_runtime_config()
        captured = []
        monkeypatch.setattr(mqtt, "mqtt_publish",
                            lambda t, p, retain=False: captured.append((t, p)))
        publishing.publish_system(meter, db, state="ready")
        payload = captured[0][1]
        assert payload["status"] == "ready"
        assert payload["version"] == config.__version__
        assert "uptime_seconds" in payload

    def test_publish_system_can_omit_runtime_info(self, temp_db, monkeypatch):
        from dtsu666_tou import mqtt
        db, meter, _path = temp_db
        path = write_conf("[MONITORING]\npublish_runtime_info = false\n")
        try:
            config.load_runtime_config(path)
            captured = []
            monkeypatch.setattr(mqtt, "mqtt_publish",
                                lambda t, p, retain=False: captured.append((t, p)))
            publishing.publish_system(meter, db, state="ready")
            assert "version" not in captured[0][1]
        finally:
            os.unlink(path)


# ======================================================================
# supervised acquisition loop
# ======================================================================

class _Args:
    replace_meter = False


class _FakeModbusRTU:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def is_open(self):
        return True

    def ensure_open(self, reason="x"):
        return True

    def close(self):
        self.closed = True


def _serial_cfg():
    return {"SERIAL": {"port": "/dev/fake", "baudrate": "9600",
                       "timeout": "1.0"}}


class TestSupervisedLoop:
    def test_retries_after_a_failure_instead_of_exiting(self, monkeypatch):
        from dtsu666_tou import app
        attempts = {"n": 0}

        def failing_init(db, modbus_, address):
            attempts["n"] += 1
            if attempts["n"] >= 2:
                runtime.running = False       # stop once the retry happened
            raise RuntimeError("meter offline at boot")

        monkeypatch.setattr(app, "ModbusRTU", _FakeModbusRTU)
        monkeypatch.setattr(app, "initialize_meter", failing_init)
        monkeypatch.setattr(app, "_interruptible_sleep", lambda s: None)
        runtime.running = True
        try:
            app.run_forever(None, _serial_cfg(), 1, _Args())
        finally:
            runtime.running = True
        assert attempts["n"] == 2, "the loop must be re-entered after a failure"

    def test_returns_when_the_scheduler_stops_cleanly(self, monkeypatch):
        from dtsu666_tou import app

        monkeypatch.setattr(app, "ModbusRTU", _FakeModbusRTU)
        monkeypatch.setattr(app, "initialize_meter",
                            lambda db, m, a: {"id": 1, "modbus_address": a})
        monkeypatch.setattr(app, "initial_read", lambda *a, **k: None)

        def scheduler(db, modbus_, meter):
            runtime.running = False

        monkeypatch.setattr(app, "run_scheduler", scheduler)
        runtime.running = True
        app.run_forever(None, _serial_cfg(), 1, _Args())
        assert runtime.running is False

    def test_unopenable_port_is_retried_not_fatal(self, monkeypatch):
        from dtsu666_tou import app
        opened = {"n": 0}

        class ClosedPort(_FakeModbusRTU):
            def is_open(self):
                return False

            def ensure_open(self, reason="x"):
                opened["n"] += 1
                if opened["n"] >= 2:
                    runtime.running = False
                return False

        monkeypatch.setattr(app, "ModbusRTU", ClosedPort)
        monkeypatch.setattr(app, "_interruptible_sleep", lambda s: None)
        runtime.running = True
        try:
            app.run_forever(None, _serial_cfg(), 1, _Args())
        finally:
            runtime.running = True
        assert opened["n"] == 2

    def test_interruptible_sleep_returns_immediately_when_stopped(self):
        from dtsu666_tou import app
        runtime.running = False
        try:
            started = time.monotonic()
            app._interruptible_sleep(30.0)
            assert time.monotonic() - started < 1.0
        finally:
            runtime.running = True

    def test_shutdown_publishes_disconnected_and_closes(self, temp_db,
                                                       monkeypatch):
        from dtsu666_tou import app
        db, meter, _path = temp_db
        published = []
        monkeypatch.setattr(app, "notify_mqtt_disconnected",
                            lambda m, d: published.append(m["id"]))
        monkeypatch.setattr(app.time, "sleep", lambda s: None)

        class FakeClient:
            stopped = False
            disconnected = False

            def loop_stop(self):
                self.stopped = True

            def disconnect(self):
                self.disconnected = True

        client = FakeClient()
        saved = (runtime.mqtt_client, runtime.meter)
        runtime.mqtt_client, runtime.meter = client, meter
        try:
            app.shutdown(db, 1)
        finally:
            runtime.mqtt_client, runtime.meter = saved
        assert published == [meter["id"]]
        assert client.stopped is True
        assert client.disconnected is True
