"""SQLite persistence: schema, meter instances, energy recording, retention."""

import functools
import logging
import sqlite3
import time

from . import config
from .config import (
    DATABASE_FILE,
    RETENTION_DAYS,
    DAILY_RETENTION_DAYS,
    ALLOCATION_BASELINE,
    ALLOCATION_MEASURED,
    ALLOCATION_ESTIMATED,
    ALLOCATION_RESET,
    ESTIMATED_THRESHOLD,
)
from .time_utils import now_local, _parse_timestamp
from .tariff import current_tariff, allocate_interval
from .modbus import float32

log = logging.getLogger("dtsu666.database")


# ============================================================
# Write-conflict resilience
# ============================================================

def _is_locked(exc):
    return (isinstance(exc, sqlite3.OperationalError)
            and "locked" in str(exc).lower())


def retry_on_locked(func):
    """Retry a database operation when SQLite reports SQLITE_BUSY.

    SQLite can return "database is locked" when another process (a backup,
    an audit, or an administrator with the sqlite3 shell) holds the write
    lock.  Losing a minute of energy data because of that is unacceptable,
    so the operation is retried with a short exponential backoff.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        attempts = max(1, config.db_lock_retries() + 1)
        delay = 0.05
        for attempt in range(1, attempts + 1):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_locked(exc) or attempt == attempts:
                    raise
                log.warning("Database busy (%s); retry %d/%d",
                            exc, attempt, attempts - 1)
                time.sleep(delay)
                delay = min(1.0, delay * 2)
        raise AssertionError("unreachable")  # pragma: no cover
    return wrapper


# ============================================================
# Database
# ============================================================

def open_database(path=DATABASE_FILE):
    """Open (or create) the SQLite database and return a connection.

    Configured for a long-running writer: WAL journalling, foreign keys
    enforced, a busy timeout so brief write contention retries instead of
    failing, and ``synchronous=NORMAL`` (durable across process crashes,
    which is what matters for a service; see the SQLite documentation).
    """
    busy_ms = config.db_busy_timeout_ms()
    db = sqlite3.connect(path, timeout=max(0.0, busy_ms / 1000.0))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")
    log.info("Database opened: %s (busy_timeout=%d ms, foreign_keys=ON)",
             path, busy_ms)

    db.execute("""
        CREATE TABLE IF NOT EXISTS meters (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            modbus_address     INTEGER NOT NULL,
            instance_name      TEXT    NOT NULL,
            valid_from         TEXT    NOT NULL,
            valid_to           TEXT,
            initial_imp_ep     REAL,
            replacement_reason TEXT,
            active             INTEGER NOT NULL DEFAULT 1
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id           INTEGER NOT NULL,
            timestamp          TEXT    NOT NULL,
            absolute_kwh       REAL    NOT NULL,
            delta_kwh          REAL    NOT NULL,
            tariff             TEXT    NOT NULL,
            vt_kwh             REAL    NOT NULL,
            nt_kwh             REAL    NOT NULL,
            allocation_method  TEXT    NOT NULL,
            FOREIGN KEY (meter_id) REFERENCES meters(id)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_meter_time
        ON readings(meter_id, timestamp)
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS energy_daily (
            meter_id           INTEGER NOT NULL,
            date               TEXT    NOT NULL,
            absolute_kwh       REAL    NOT NULL,
            total_kwh          REAL    NOT NULL,
            vt_kwh             REAL    NOT NULL,
            nt_kwh             REAL    NOT NULL,
            PRIMARY KEY (meter_id, date),
            FOREIGN KEY (meter_id) REFERENCES meters(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS corrections (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id      INTEGER NOT NULL,
            date_from     TEXT    NOT NULL,
            date_to       TEXT    NOT NULL,
            vt_kwh        REAL    NOT NULL,
            nt_kwh        REAL    NOT NULL,
            note          TEXT    DEFAULT '',
            created_at    TEXT    NOT NULL,
            FOREIGN KEY (meter_id) REFERENCES meters(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS reference_readings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id   INTEGER NOT NULL,
            timestamp  TEXT    NOT NULL,
            vt_kwh     REAL    NOT NULL,
            nt_kwh     REAL    NOT NULL,
            reason     TEXT    DEFAULT '',
            source     TEXT    DEFAULT 'photo',
            created_at TEXT    NOT NULL,
            UNIQUE(meter_id, timestamp),
            FOREIGN KEY (meter_id) REFERENCES meters(id)
        )
    """)
    db.commit()
    return db


# ---- Meter instance ------------------------------------------------

def get_active_meter(db, address):
    """Return the currently-active meter row for *address*, or None."""
    return db.execute(
        "SELECT * FROM meters WHERE modbus_address = ? AND active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (address,),
    ).fetchone()


def create_initial_meter(db, address, initial_imp_ep):
    """Create the very first meter instance. Returns the new row ``id``."""
    now = now_local().isoformat()
    count = db.execute(
        "SELECT COUNT(*) FROM meters WHERE modbus_address = ?", (address,)
    ).fetchone()[0]
    instance_name = f"DTSU666_{address}_{count + 1:03d}"

    db.execute(
        "UPDATE meters SET active = 0 WHERE modbus_address = ?", (address,)
    )
    cur = db.execute(
        "INSERT INTO meters (modbus_address, instance_name, valid_from, "
        "initial_imp_ep, replacement_reason, active) "
        "VALUES (?, ?, ?, ?, 'initial', 1)",
        (address, instance_name, now, initial_imp_ep),
    )
    db.commit()
    return cur.lastrowid


def perform_replacement(db, modbus, address):
    """Interactive meter replacement (called from ``--replace-meter``)."""
    old = get_active_meter(db, address)
    print()
    print("=" * 60)
    print(f"METER REPLACEMENT - MODBUS ADDRESS {address}")
    print("=" * 60)
    if old:
        print(f"Current instance : {old['instance_name']}")
        print(f"Valid from       : {old['valid_from']}")
        print(f"Initial ImpEp    : {old['initial_imp_ep']:.3f}")

    answer = input("\nConfirm physical meter replacement? [y/N]: ")
    if answer.lower() != "y":
        print("Cancelled.")
        return

    print("\nReading new meter …")
    registers = modbus.read_registers(address, 0x101E, 2)
    new_imp_ep = float32(registers, 0)
    print(f"New ImpEp: {new_imp_ep:.3f} kWh")

    answer = input("Create new meter instance? [Y/n]: ")
    if answer.lower() == "n":
        print("Cancelled.")
        return

    now = now_local().isoformat()
    if old:
        db.execute(
            "UPDATE meters SET valid_to = ?, active = 0 WHERE id = ?",
            (now, old["id"]),
        )

    count = db.execute(
        "SELECT COUNT(*) FROM meters WHERE modbus_address = ?", (address,)
    ).fetchone()[0]
    instance_name = f"DTSU666_{address}_{count + 1:03d}"
    db.execute(
        "INSERT INTO meters (modbus_address, instance_name, valid_from, "
        "initial_imp_ep, replacement_reason, active) "
        "VALUES (?, ?, ?, ?, 'meter_replacement', 1)",
        (address, instance_name, now, new_imp_ep),
    )


@retry_on_locked
def record_energy(db, meter, timestamp, absolute_kwh):
    """Store one energy sample; return accounting dict."""
    previous = db.execute(
        "SELECT absolute_kwh, timestamp FROM readings "
        "WHERE meter_id = ? ORDER BY id DESC LIMIT 1",
        (meter["id"],),
    ).fetchone()

    if previous is None:
        # ---- baseline ----
        delta = 0.0
        tariff = current_tariff(timestamp)
        vt = nt = 0.0
        method = ALLOCATION_BASELINE
    else:
        prev_abs = previous["absolute_kwh"]
        prev_ts = _parse_timestamp(previous["timestamp"])
        delta = absolute_kwh - prev_abs
        reset = delta < 0

        if reset:
            print(
                f"WARNING: ImpEp decreased on {meter['instance_name']}: "
                f"{prev_abs:.3f} -> {absolute_kwh:.3f} kWh"
            )
            delta = 0.0

        elapsed = (timestamp - prev_ts).total_seconds()
        same_tariff = (
            current_tariff(prev_ts) == current_tariff(timestamp)
        )
        tariff = current_tariff(timestamp)

        if reset:
            vt = nt = 0.0
            method = ALLOCATION_RESET
        elif delta <= 0:
            vt = nt = 0.0
            method = ALLOCATION_MEASURED if same_tariff else ALLOCATION_ESTIMATED
        elif elapsed <= ESTIMATED_THRESHOLD and same_tariff:
            method = ALLOCATION_MEASURED
            vt = delta if tariff == "VT" else 0.0
            nt = delta if tariff == "NT" else 0.0
        else:
            method = ALLOCATION_ESTIMATED
            vt, nt = allocate_interval(delta, prev_ts, timestamp)

    db.execute(
        "INSERT INTO readings (meter_id, timestamp, absolute_kwh, "
        "delta_kwh, tariff, vt_kwh, nt_kwh, allocation_method) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (meter["id"], timestamp.isoformat(), absolute_kwh,
         delta, tariff, vt, nt, method),
    )

    # ---- daily aggregate --------------------------------------------
    day_str = timestamp.date().isoformat()
    existing = db.execute(
        "SELECT * FROM energy_daily WHERE meter_id = ? AND date = ?",
        (meter["id"], day_str),
    ).fetchone()

    # The daily "latest counter" must never regress, even when the meter
    # counter itself decreased (a reset is recorded with a zero delta above,
    # but its raw counter is lower).  Clamp it to the highest value seen so
    # far so ``energy_daily.absolute_kwh`` stays monotonic.
    if existing is None:
        daily_abs = absolute_kwh
        if previous is not None:
            daily_abs = max(daily_abs, previous["absolute_kwh"])
        db.execute(
            "INSERT INTO energy_daily "
            "(meter_id, date, absolute_kwh, total_kwh, vt_kwh, nt_kwh) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (meter["id"], day_str, daily_abs, delta, vt, nt),
        )
    else:
        db.execute(
            "UPDATE energy_daily SET "
            "absolute_kwh = ?, total_kwh = total_kwh + ?, "
            "vt_kwh = vt_kwh + ?, nt_kwh = nt_kwh + ? "
            "WHERE meter_id = ? AND date = ?",
            (max(existing["absolute_kwh"], absolute_kwh),
             delta, vt, nt, meter["id"], day_str),
        )
    db.commit()

    return {
        "delta_kwh": delta,
        "tariff":    tariff,
        "vt_kwh":    vt,
        "nt_kwh":    nt,
        "method":    method,
    }


# ============================================================
# Retention
# ============================================================

@retry_on_locked
def cleanup_database(db):
    """Purge minute-level data older than the minute retention window and
    daily data older than the daily window (both tunable via dtsu666.conf)."""
    from datetime import timedelta
    now = now_local()
    minute_days = config.retention_minute_days()
    daily_days = config.retention_daily_days()
    cutoff_minute = (now - timedelta(days=minute_days)).isoformat()
    cutoff_daily  = (now - timedelta(days=daily_days)).date().isoformat()

    removed_minutes = db.execute(
        "DELETE FROM readings WHERE timestamp < ?", (cutoff_minute,)).rowcount
    removed_daily = db.execute(
        "DELETE FROM energy_daily WHERE date < ?", (cutoff_daily,)).rowcount
    db.commit()
    if removed_minutes or removed_daily:
        log.info("Retention cleanup: removed %d reading(s) and %d daily row(s) "
                 "(minute_days=%d, daily_days=%d)",
                 removed_minutes, removed_daily, minute_days, daily_days)
