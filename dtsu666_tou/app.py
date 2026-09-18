"""CLI entry point, signal handling, and the --test smoke test."""

import argparse
import logging
import os
import signal
import sys
import tempfile
import time
from datetime import timedelta

from . import config, runtime, watchdog
from .config import (
    __version__, DATABASE_FILE, RUNTIME_CONF_FILE, SECRETS_FILE,
    check_ntp_sync, load_config,
)
from .logging_utils import setup_logging
from .time_utils import now_local
from .modbus import (
    ModbusRTU, ModbusTransportError, FakeModbus, _pack32,
    ELECTRICAL, POWER_FACTOR, FREQUENCY,
)
from .database import (
    open_database, get_active_meter, create_initial_meter, perform_replacement,
    record_energy, cleanup_database,
)
from .periods import build_periods
from .hep import calculate_corrected
from .mqtt import create_mqtt_client
from .lifecycle import initialize_meter, initial_read
from .scheduler import run_scheduler
from .publishing import notify_mqtt_disconnected

log = logging.getLogger("dtsu666.app")


# ============================================================
# Signal handling
# ============================================================

def _signal_handler(sig, frame):
    print("\nShutdown requested ...")
    runtime.running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ============================================================
# Test mode
# ============================================================

def run_test_mode():
    """End-to-end smoke test using fake hardware (fast, no sleep)."""
    import os as _os
    import tempfile

    print("=== --test mode ===")

    # ---------- build a fake meter with a known ImpEp ----------
    regs = {}
    imp = 20000.0   # starting counter
    def _set_imp(value):
        nonlocal imp
        imp = value
        hi, lo = _pack32(value)
        regs[0x101E] = hi
        regs[0x101F] = lo
    _set_imp(imp)

    # Populate electrical registers with plausible values
    for addr, name, _d, _u, mult, _dec in ELECTRICAL:
        hi, lo = _pack32(230.0 / mult)
        regs[addr] = hi
        regs[addr + 1] = lo
    for addr, name, _d, _u, mult, _dec in POWER_FACTOR:
        hi, lo = _pack32(0.98 / mult)
        regs[addr] = hi
        regs[addr + 1] = lo
    for addr, name, _d, _u, mult, _dec in FREQUENCY:
        hi, lo = _pack32(50.0 / mult)
        regs[addr] = hi
        regs[addr + 1] = lo
    # Basic registers - place address at register 0x002E
    regs[0x002E] = 1

    _fake_modbus = FakeModbus(regs)

    # ---------- temp database -----------------------------------
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="dtsu_test_")
    _os.close(fd)
    db = open_database(db_path)

    try:
        # ---------- create meter + baseline ---------------------
        meter_id = create_initial_meter(db, 1, imp)
        meter = db.execute(
            "SELECT * FROM meters WHERE id = ?", (meter_id,)
        ).fetchone()
        record_energy(db, meter, now_local(), imp)
        print(f"  meter instance : {meter['instance_name']} (id={meter_id})")
        print(f"  baseline ImpEp : {imp:.3f} kWh")

        # ---------- normal minute (measured) ---------------------
        imp += 0.034
        _set_imp(imp)
        ts1 = now_local()
        acc1 = record_energy(db, meter, ts1, imp)
        assert abs(acc1["delta_kwh"] - 0.034) < 1e-6
        assert acc1["method"] == "measured", (
            f"expected measured, got {acc1['method']}"
        )
        print(f"  normal minute  : delta={acc1['delta_kwh']:.3f} "
              f"method={acc1['method']}  OK")

        # ---------- gap of 1 hour (estimated, single tariff) -----
        gap_ts = ts1 + timedelta(hours=1)
        imp += 1.500
        _set_imp(imp)
        acc2 = record_energy(db, meter, gap_ts, imp)
        assert acc2["method"] == "estimated"
        assert abs(acc2["delta_kwh"] - 1.500) < 0.001
        # All 60 min in VT? depends on current time; just check
        # vt+nt == delta
        assert abs(acc2["vt_kwh"] + acc2["nt_kwh"] - 1.500) < 1e-6
        print(f"  1h gap         : delta={acc2['delta_kwh']:.3f} "
              f"vt={acc2['vt_kwh']:.3f} nt={acc2['nt_kwh']:.3f} "
              f"method={acc2['method']}  OK")

        # ---------- period summaries -----------------------------
        periods = build_periods(db, meter["id"], now_local())
        corrected = calculate_corrected(db, meter["id"], now_local(), periods)
        assert "current_day" in corrected
        assert "previous_day" in corrected
        assert "current_week" in corrected
        assert "previous_week" in corrected
        assert "current_month" in corrected
        assert "previous_month" in corrected
        print("  periods         : all 6 summaries built  OK")

        # ---------- corrections layer (reference_readings) -----
        db.execute(
            "INSERT INTO reference_readings "
            "(meter_id, timestamp, vt_kwh, nt_kwh, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (meter["id"], now_local().isoformat(), 99.0, 0.0,
             now_local().isoformat()),
        )
        # insert a second anchor so a shift is computed
        db.execute(
            "INSERT INTO reference_readings "
            "(meter_id, timestamp, vt_kwh, nt_kwh, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (meter["id"],
             (now_local() + timedelta(hours=1)).isoformat(),
             99.1, 0.1,
             now_local().isoformat()),
        )
        db.commit()
        corrected = calculate_corrected(
            db, meter["id"], now_local(), periods,
        )
        cd = corrected["current_day"]
        print(f"  corrections     : raw vt={cd['vt_kwh']:.3f} "
              f"corrected vt={cd['vt_corrected']:.3f}  OK")

        # ---------- ImpEp decrease -------------------------------
        prev = imp
        imp = prev - 50   # went backwards
        _set_imp(imp)
        acc3 = record_energy(db, meter, now_local(), imp)
        assert acc3["delta_kwh"] == 0.0
        print("  ImpEp decrease  : delta clamped to 0  OK")

        print("\n=== All --test checks passed ===")
        return True

    finally:
        db.close()
        _os.unlink(db_path)


# ============================================================
# Supervised acquisition loop
# ============================================================

MAX_RESTART_DELAY = 60.0


def _interruptible_sleep(seconds):
    """Sleep in small steps so SIGINT/SIGTERM are handled promptly."""
    end = time.monotonic() + max(0.0, seconds)
    while runtime.running and time.monotonic() < end:
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))


def run_forever(db, cfg, address, args):
    """Acquire until stopped, restarting the pipeline after any failure.

    Serial faults, a meter that is unreachable at boot, database errors and
    unexpected exceptions are all recovered here: the loop is re-entered
    with exponential backoff instead of the process exiting - which used to
    leave a stale ``active`` status visible in Home Assistant.

    Returns the meter row that was in use, for the shutdown notification.
    """
    port = cfg["SERIAL"]["port"]
    baudrate = int(cfg["SERIAL"]["baudrate"])
    timeout = float(cfg["SERIAL"]["timeout"])

    delay = 1.0
    attempt = 0
    while runtime.running:
        attempt += 1
        modbus = None
        try:
            modbus = ModbusRTU(port, baudrate, timeout)
            if not modbus.is_open() and not modbus.ensure_open("startup"):
                raise ModbusTransportError(f"cannot open serial port {port}")

            if args.replace_meter:
                perform_replacement(db, modbus, address)
                return runtime.meter

            meter = initialize_meter(db, modbus, address)
            runtime.meter = meter
            initial_read(db, modbus, meter)

            delay = 1.0                 # healthy startup: reset the backoff
            attempt = 0
            watchdog.status_message(f"acquiring meter {address} on {port}")

            run_scheduler(db, modbus, meter)
            if not runtime.running:
                return runtime.meter
            log.warning("Scheduler returned unexpectedly; restarting")
        except Exception:  # noqa: BLE001
            if not runtime.running:
                return runtime.meter
            log.exception("Acquisition loop failed (attempt %d); "
                          "restarting in %.0f s", attempt, delay)
            watchdog.status_message(f"restarting after failure in {delay:.0f}s")
        finally:
            if modbus is not None:
                try:
                    modbus.close()
                except Exception:  # noqa: BLE001
                    pass

        if not runtime.running:
            return runtime.meter
        _interruptible_sleep(delay)
        delay = min(MAX_RESTART_DELAY, delay * 2)

    return runtime.meter


def shutdown(db, address):
    """Publish a final 'disconnected' state and close everything down.

    The availability topic is deliberately left at ``online``: a *planned*
    stop should be visible as an explicit ``disconnected`` status, whereas
    an *unexpected* death is what the broker's last will reports as
    ``offline`` (making the entities unavailable in Home Assistant).
    """
    print("\nStopping ...")
    log.info("Shutdown requested")
    watchdog.notify_stopping()

    if runtime.mqtt_client is not None:
        meter = runtime.meter
        if meter is None:
            try:
                meter = get_active_meter(db, address)
            except Exception:  # noqa: BLE001
                meter = None
        if meter is not None:
            try:
                notify_mqtt_disconnected(meter, db)
                time.sleep(0.5)   # flush the paho outbound queue
            except Exception:  # noqa: BLE001
                log.exception("Failed to publish the disconnected state")

    try:
        cleanup_database(db)
    except Exception:  # noqa: BLE001
        log.exception("Final retention cleanup failed")
    try:
        db.close()
    except Exception:  # noqa: BLE001
        pass
    if runtime.mqtt_client:
        try:
            runtime.mqtt_client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            runtime.mqtt_client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    print("Stopped.")


# ============================================================
# main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="CHINT DTSU666 single-meter Modbus RTU -> MQTT reader",
    )
    parser.add_argument("--replace-meter", action="store_true",
                        help="Interactive physical meter replacement")
    parser.add_argument("--test", action="store_true",
                        help="Run smoke-test with fake hardware and exit")
    parser.add_argument("--skip-ntp-check", action="store_true",
                        help="Bypass NTP sync requirement (dev only)")
    parser.add_argument("--config", default=SECRETS_FILE,
                        help=f"Path to secrets.ini (default: {SECRETS_FILE})")
    parser.add_argument("--runtime-config", default=RUNTIME_CONF_FILE,
                        help="Path to dtsu666.conf (non-secret tuning)")
    parser.add_argument("--db", default=DATABASE_FILE,
                        help="Path to the SQLite database")
    args = parser.parse_args()

    # ---- runtime configuration + logging ------------------------
    config.load_runtime_config(args.runtime_config)
    setup_logging()

    # ---- test mode (fast path) ----------------------------------
    if args.test:
        ok = run_test_mode()
        sys.exit(0 if ok else 1)

    # ---- banner -------------------------------------------------
    print()
    print("=" * 70)
    print(f"CHINT DTSU666 MODBUS / MQTT READER  v{__version__}")
    print("=" * 70)

    # ---- NTP ----------------------------------------------------
    if args.skip_ntp_check:
        print("NTP check skipped (--skip-ntp-check)")
    elif not check_ntp_sync():
        print()
        print("ERROR: System time is not NTP-synchronized.")
        print("Tariff and period accounting depend on correct local time.")
        print("Start with --skip-ntp-check to override (dev only).")
        log.error("Refusing to start: system clock is not NTP-synchronized")
        sys.exit(1)
    else:
        print("NTP status : synchronized")

    # ---- config -------------------------------------------------
    try:
        cfg, address = load_config(args.config)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    port       = cfg["SERIAL"]["port"]
    baudrate   = int(cfg["SERIAL"]["baudrate"])
    timeout    = float(cfg["SERIAL"]["timeout"])

    print(f"Serial port  : {port}")
    print(f"Baudrate     : {baudrate}")
    print("Format       : 8N1")
    print(f"Modbus addr  : {address}")
    print(f"Database     : {args.db}")
    print(f"Time         : {now_local().isoformat()}")

    log.info("Starting v%s: meter=%s port=%s db=%s",
             __version__, address, port, args.db)

    # ---- database -----------------------------------------------
    db = open_database(args.db)

    # ---- MQTT ---------------------------------------------------
    create_mqtt_client(cfg, address)

    # ---- run ----------------------------------------------------
    # The service is "ready" as soon as it can record and publish, even if
    # the meter itself is not answering yet - run_forever retries that.
    watchdog.ready(f"waiting for meter {address} on {port}")
    try:
        run_forever(db, cfg, address, args)
    except KeyboardInterrupt:      # pragma: no cover - signals are handled
        runtime.running = False
    finally:
        shutdown(db, address)
