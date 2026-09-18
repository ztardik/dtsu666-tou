"""
dtsu666_tou - package configuration.

Version, file paths, polling intervals, retention, allocation-method
constants, the secrets.ini loader and the NTP gate.
"""

import configparser
import os
import subprocess
from zoneinfo import ZoneInfo

__version__ = "0.9.0"

# ============================================================
# Constants
# ============================================================

SECRETS_FILE = "secrets.ini"
DATABASE_FILE = "dtsu666_energy.db"
TARIFFS_FILE = "tariffs.ini"
LOCAL_TZ = ZoneInfo("Europe/Zagreb")

ELECTRICAL_INTERVAL = 1
ENERGY_INTERVAL = 60

RETENTION_DAYS = 365
DAILY_RETENTION_DAYS = 3650

ESTIMATED_THRESHOLD = 120.0            # seconds - below this + same
                                       # tariff -> allocation = measured
ALLOCATION_BASELINE  = "baseline"
ALLOCATION_MEASURED  = "measured"
ALLOCATION_ESTIMATED = "estimated"

HA_STATUS_TOPIC = "homeassistant/status"

# ============================================================
# Non-secret runtime configuration (dtsu666.conf)
# ============================================================
#
# Everything in this file is optional: when ``dtsu666.conf`` is absent (or
# a key is missing) the hard-coded defaults below apply, so behaviour is
# unchanged for existing installations.  Secrets never live here - they
# stay in secrets.ini.

RUNTIME_CONF_FILE = "dtsu666.conf"

RUNTIME_DEFAULTS = {
    "MONITORING": {
        # A reading older than this marks the meter as not connected in HA.
        "freshness_seconds": str(int(2 * ESTIMATED_THRESHOLD)),
        # Retained availability (LWT) topic suffix under the meter base.
        "availability_suffix": "availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        # Include version/uptime/host in the retained /system payload.
        "publish_runtime_info": "true",
    },
    "WATCHDOG": {
        # systemd watchdog ping interval in seconds (0 disables sd_notify).
        "interval_seconds": "0",
    },
    "RETENTION": {
        "minute_days": str(RETENTION_DAYS),
        "daily_days": str(DAILY_RETENTION_DAYS),
    },
    "LOGGING": {
        "level": "INFO",
    },
    "DATABASE": {
        # SQLite busy timeout in milliseconds (writer contention).
        "busy_timeout_ms": "5000",
        # Retries when the database reports SQLITE_BUSY.
        "lock_retries": "5",
    },
    "SERIAL": {
        # Re-open the serial port after this many consecutive read failures.
        "reopen_after_failures": "3",
        # Delay between re-open attempts (seconds); doubles up to max_delay.
        "reopen_delay_seconds": "2",
        "reopen_max_delay_seconds": "30",
    },
}

_runtime_cfg = None


def load_runtime_config(path=None):
    """Read ``dtsu666.conf`` and merge it over the built-in defaults.

    Returns a ``configparser.ConfigParser``.  A missing file is not an
    error - the defaults are returned.
    """
    global _runtime_cfg
    cfg = configparser.ConfigParser()
    for section, values in RUNTIME_DEFAULTS.items():
        cfg.add_section(section)
        for key, value in values.items():
            cfg.set(section, key, value)

    p = path if path is not None else RUNTIME_CONF_FILE
    if p and os.path.exists(p):
        cfg.read(p)
    _runtime_cfg = cfg
    return cfg


def runtime_config():
    """Return the cached runtime configuration (loaded on first use)."""
    if _runtime_cfg is None:
        return load_runtime_config()
    return _runtime_cfg


def reset_runtime_config():
    """Drop the cached runtime configuration (used by tests)."""
    global _runtime_cfg
    _runtime_cfg = None


def _get(section, key, default):
    try:
        return runtime_config().get(section, key)
    except (configparser.Error, KeyError, AttributeError):
        return str(default)


def _get_float(section, key, default):
    try:
        return float(_get(section, key, default))
    except (TypeError, ValueError):
        return float(default)


def _get_int(section, key, default):
    try:
        return int(float(_get(section, key, default)))
    except (TypeError, ValueError):
        return int(default)


def _get_bool(section, key, default):
    return str(_get(section, key, default)).strip().lower() in (
        "1", "true", "yes", "on")


def freshness_seconds():
    return _get_float("MONITORING", "freshness_seconds",
                      int(2 * ESTIMATED_THRESHOLD))


def availability_suffix():
    return _get("MONITORING", "availability_suffix", "availability").strip("/")


def payload_available():
    return _get("MONITORING", "payload_available", "online")


def payload_not_available():
    return _get("MONITORING", "payload_not_available", "offline")


def publish_runtime_info():
    return _get_bool("MONITORING", "publish_runtime_info", True)


def watchdog_interval():
    return _get_float("WATCHDOG", "interval_seconds", 0.0)


def retention_minute_days():
    return _get_int("RETENTION", "minute_days", RETENTION_DAYS)


def retention_daily_days():
    return _get_int("RETENTION", "daily_days", DAILY_RETENTION_DAYS)


def log_level():
    return _get("LOGGING", "level", "INFO").upper()


def db_busy_timeout_ms():
    return _get_int("DATABASE", "busy_timeout_ms", 5000)


def db_lock_retries():
    return _get_int("DATABASE", "lock_retries", 5)


def serial_reopen_after_failures():
    return max(0, _get_int("SERIAL", "reopen_after_failures", 3))


def serial_reopen_delay():
    return max(0.0, _get_float("SERIAL", "reopen_delay_seconds", 2.0))


def serial_reopen_max_delay():
    return max(0.0, _get_float("SERIAL", "reopen_max_delay_seconds", 30.0))


def availability_topic(address):
    return f"Electricity/dtsu666/{address}/{availability_suffix()}"



# ============================================================
# NTP verification
# ============================================================

def check_ntp_sync():
    """Return True if ``timedatectl`` reports NTP-synchronized time."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return result.stdout.strip().lower() == "yes"
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Cannot determine NTP status: {exc}")
        return False


# ============================================================
# Configuration
# ============================================================

def load_config(path=SECRETS_FILE):
    """Read secrets.ini and return (cfg, address)."""
    if not os.path.exists(path):
        raise RuntimeError(f"Cannot find {path}")

    cfg = configparser.ConfigParser()
    cfg.read(path)

    for section in ("SERIAL", "MQTT"):
        if section not in cfg:
            raise RuntimeError(f"Missing [{section}] section in {path}")

    address_str = cfg["SERIAL"].get("address", "")
    if not address_str:
        raise RuntimeError(
            "[SERIAL] address is required (Modbus slave address, 1-247)"
        )
    address = int(address_str)
    if address < 1 or address > 247:
        raise RuntimeError(f"Invalid Modbus address: {address} (must be 1-247)")

    return cfg, address
