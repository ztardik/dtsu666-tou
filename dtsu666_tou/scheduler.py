"""Wall-clock-aligned polling scheduler.

Electrical every wall-clock second, energy every wall-clock minute,
HEP-correction and tariff hot-reload watchers, and a once-a-day
retention cleanup.
"""

import logging
import os
import time

from . import commissioning, runtime, watchdog
from .config import SECRETS_FILE
from .cost import publish_cost
from .database import cleanup_database, record_energy
from .hep import process_hep_corrections
from .modbus import (
    read_basic,
    read_electrical,
    read_energy,
    read_frequency,
    read_power_factor,
    zero_electrical,
)
from .publishing import (
    energy_flat,
    publish_electrical,
    publish_energy,
    publish_energy_disconnected,
    publish_meter,
    publish_system,
)
from .tariff import current_tariff
from .time_utils import (
    local_to_epoch,
    next_minute_boundary,
    next_second_boundary,
    now_local,
)

# ---- secrets.ini mtime watcher for HEP corrections ------------

log = logging.getLogger("dtsu666.scheduler")

_HEP_LAST_MTIME = 0.0
_last_tariff = None          # track tariff changes for publish_system
_WATCHDOG_LAST_PING = 0.0


def _safe(label, func, *args, **kwargs):
    """Run *func*, logging (never propagating) any failure.

    The acquisition loop must survive anything that is not a signal - a
    failed secrets.ini hot-reload or a publish error must never stop the
    logger from recording energy.
    """
    try:
        return func(*args, **kwargs)
    except Exception:  # noqa: BLE001
        log.exception("%s failed", label)
        return None


def _publish_system_state(meter, db, now):
    publish_system(meter, db,
                   commissioning.operational_state(db, meter["id"], now))


def _ping_watchdog():
    """Reset the systemd watchdog timer at most twice per interval."""
    global _WATCHDOG_LAST_PING
    interval = watchdog.watchdog_interval()
    if interval <= 0 or not watchdog.enabled():
        return
    now_m = time.monotonic()
    if now_m - _WATCHDOG_LAST_PING >= interval / 2.0:
        watchdog.watchdog_ping()
        _WATCHDOG_LAST_PING = now_m


def _maybe_process_hep_corrections(db, meter):
    """Check secrets.ini mtime; if changed, process [HEP Correction]."""
    global _HEP_LAST_MTIME
    try:
        mtime = os.path.getmtime(SECRETS_FILE)
    except OSError:
        return
    if mtime <= _HEP_LAST_MTIME:
        return
    _HEP_LAST_MTIME = mtime
    process_hep_corrections(db, meter)


def run_scheduler(db, modbus, meter, *,
                  now_fn=None, sleep_fn=None, max_seconds=None):
    """Wall-clock aligned polling loop.

    Parameters
    ----------
    now_fn     : zero-arg callable -> aware local datetime (default: now_local)
    sleep_fn   : one-arg callable -> sleeps *seconds* (default: time.sleep)
    max_seconds: if set, stop after this many **real** seconds (for --test)
    """
    global _last_tariff

    now_fn   = now_fn   or now_local
    sleep_fn = sleep_fn or time.sleep

    addr = meter["modbus_address"]

    print()
    print("=" * 70)
    print("POLLING STARTED")
    print("=" * 70)
    print("  Electrical  : every 1 second  (wall-clock aligned)")
    print("  Energy      : every 60 seconds (HH:MM:00)")
    print("  Time zone   : Europe/Zagreb")
    print("  Tariff      : HEP Bijeli VT / NT (DST-aware)")
    print()

    # ---- wall-clock align -----------------------------------------
    target = next_second_boundary(now_fn())
    while runtime.running:
        remaining = local_to_epoch(target) - time.time()
        if remaining <= 0:
            break
        sleep_fn(min(remaining, 0.2))

    # ---- state ------------------------------------------------
    was_connected = False          # unknown until first successful read
    last_energy_minute = None

    _t0 = time.monotonic()

    # Pre-compute next boundaries
    now = now_fn()
    next_second = next_second_boundary(now)
    next_energy = next_minute_boundary(now)

    while runtime.running:
        now = now_fn()
        _ping_watchdog()

        # ---- secrets.ini mtime check (HEP corrections) -----------
        _safe("HEP correction reload", _maybe_process_hep_corrections,
              db, meter)
        # ---- tariff-change detection (publish system on change) ----
        try:
            tariff = current_tariff(now)
        except Exception:  # noqa: BLE001
            log.exception("Tariff calculation failed")
            tariff = _last_tariff
        if tariff != _last_tariff:
            _last_tariff = tariff
            _safe("tariff-change publish", _publish_system_state,
                  meter, db, now)
            _safe("cost publish on tariff change", publish_cost, meter, db)

        # =====================================================
        # ELECTRICAL  - every wall-clock second
        # =====================================================
        if now >= next_second:
            try:
                elec = read_electrical(modbus, addr)
                pf   = read_power_factor(modbus, addr)
                freq = read_frequency(modbus, addr)
                publish_electrical(meter, elec, True, pf, freq)

                if not was_connected:
                    was_connected = True
                    publish_meter(meter, read_basic(modbus, addr), True)

            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] Electrical fail: %s", addr, exc)
                _safe("electrical disconnected publish",
                      publish_electrical, meter, zero_electrical(), False)

                if was_connected:
                    was_connected = False
                    _safe("meter disconnected publish",
                          publish_meter, meter, {}, False)
                    _safe("energy disconnected publish",
                          publish_energy_disconnected, meter, db)

            # Advance to NEXT boundary - never catch up
            next_second = next_second_boundary(now)

        # =====================================================
        # ENERGY  - exactly at HH:MM:00
        # =====================================================
        if now >= next_energy:
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            # Guard: at most once per wall-clock minute
            if last_energy_minute != minute_key:
                last_energy_minute = minute_key
                if not runtime.running:
                    break   # SIGINT/SIGTERM: never start another energy read
                state = commissioning.operational_state(db, meter["id"], now)
                try:
                    raw = read_energy(modbus, addr)
                    abs_kwh = raw["ImpEp"]["value"]
                    ts = now
                    acc = record_energy(db, meter, ts, abs_kwh)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] Energy fail: %s", addr, exc)
                    _safe("energy disconnected publish",
                          publish_energy_disconnected, meter, db, state)
                else:
                    # Publishing failures must not be mistaken for meter
                    # failures, so they are isolated from the read above.
                    _safe("energy publish", publish_energy, meter, abs_kwh,
                          acc, db, energy_values=energy_flat(raw), state=state)
                    _safe("system publish", publish_system, meter, db, state)
                    _safe("cost publish", publish_cost, meter, db)
                    print(
                        f"[{addr}] {ts.replace(microsecond=0).isoformat()} "
                        f"ImpEp={abs_kwh:.3f} "
                        f"delta={acc['delta_kwh']:.3f} "
                        f"tariff={acc['tariff']} "
                        f"method={acc['method']}"
                    )

            next_energy = next_minute_boundary(now)

        # =====================================================
        # Retention cleanup  (once a day at 03:00)
        # =====================================================
        if now.hour == 3 and now.minute == 0 and now.second == 0:
            _safe("retention cleanup", cleanup_database, db)

        # =====================================================
        # Sleep until the nearer boundary
        # =====================================================
        wait = min(
            (next_second - now_fn()).total_seconds(),
            (next_energy - now_fn()).total_seconds(),
        )
        if wait > 0:
            while runtime.running:
                if max_seconds is not None and (
                    time.monotonic() - _t0 >= max_seconds
                ):
                    runtime.running = False
                    break
                chunk = min(wait, 0.1)
                sleep_fn(chunk)
                wait -= chunk
                if wait <= 0:
                    break

        if max_seconds is not None and (time.monotonic() - _t0 >= max_seconds):
            runtime.running = False

