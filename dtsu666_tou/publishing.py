"""MQTT publishing helpers for meter / electrical / energy / system topics."""

import os
import socket
import time

from . import commissioning, config, mqtt
from .config import __version__
from .hep import calculate_corrected
from .modbus import zero_electrical, zero_frequency, zero_power_factor
from .periods import build_periods
from .tariff import current_tariff
from .time_utils import now_local

_last_energy_values = None   # populated on every successful energy read
_PROCESS_START = time.time()


def runtime_info():
    """Version/host/uptime fingerprint of this process.

    Publishing this on the retained /system topic makes it obvious from Home
    Assistant whether the logger has silently restarted.
    """
    return {
        "version":        __version__,
        "hostname":       socket.gethostname(),
        "process_id":     os.getpid(),
        "uptime_seconds": round(time.time() - _PROCESS_START, 1),
    }



def energy_flat(decoded):
    """Return {name: value} from a decode_block output dict."""
    return {name: item["value"] for name, item in decoded.items()}


def notify_mqtt_disconnected(meter, db):
    """Publish retained disconnected state on all MQTT topics so that Home
    Assistant reflects the meter as offline.  The operational status is forced
    to ``disconnected`` (the reader process is leaving) and corrected counters
    are zeroed via the normal gating."""
    publish_meter(meter, {}, False)
    publish_electrical(meter, zero_electrical(), False)
    publish_energy_disconnected(meter, db, "disconnected")
    publish_system(meter, db, "disconnected")


def publish_system(meter, db, state=None):
    """Publish retained system status (time, date, tariff, operational state).
    Called every minute and on tariff change.  *state* is the authoritative
    five-state operational status; it is computed by the caller once per
    acquisition cycle and must not be re-derived here."""
    now = now_local()
    tariff = current_tariff(now)
    if state is None:
        state = commissioning.operational_state(db, meter["id"], now)
    payload = {
        "timestamp": now.isoformat(),
        "time":      now.strftime("%H:%M:%S"),
        "date":      now.date().isoformat(),
        "tariff":    tariff,
        "status":    state,
    }
    if config.publish_runtime_info():
        payload.update(runtime_info())
    mqtt.mqtt_publish(mqtt._topic_base(meter["modbus_address"]) + "/system",
                 payload, retain=True)


def publish_meter(meter, basic, connected):
    payload = {
        "part":           "connected" if connected else "disconnected",
        "meter_instance": meter["instance_name"],
        "modbus_address": meter["modbus_address"],
        "valid_from":     meter["valid_from"],
        "initial_imp_ep": meter["initial_imp_ep"],
        "basic":          basic,
    }
    mqtt.mqtt_publish(mqtt._topic_base(meter["modbus_address"]) + "/meter",
                 payload, retain=True)


def publish_electrical(meter, electrical, connected,
                       power_factor=None, frequency=None):
    payload = {
        "part":           "connected" if connected else "disconnected",
        "meter_instance": meter["instance_name"],
        "modbus_address": meter["modbus_address"],
        "timestamp":      now_local().isoformat(),
        "data":           electrical,
        "power_factor":   power_factor if power_factor is not None else zero_power_factor(),
        "frequency":      frequency if frequency is not None else zero_frequency(),
    }
    mqtt.mqtt_publish(mqtt._topic_base(meter["modbus_address"]) + "/electrical",
                 payload, retain=False)


def _last_valid_from_db(db, meter_id):
    """Return (absolute_kwh, delta_kwh, tariff) of the last stored
    reading, or (None, None, None)."""
    row = db.execute(
        "SELECT absolute_kwh, delta_kwh, tariff FROM readings "
        "WHERE meter_id = ? ORDER BY id DESC LIMIT 1",
        (meter_id,),
    ).fetchone()
    if row is None:
        return None, None, None
    return row["absolute_kwh"], row["delta_kwh"], row["tariff"]


def publish_energy(meter, absolute_kwh, accounting, db, energy_values=None, state=None):
    timestamp = now_local()
    if state is None:
        state = commissioning.operational_state(db, meter["id"], timestamp)
    periods = build_periods(db, meter["id"], timestamp)
    if state == "active":
        periods = calculate_corrected(db, meter["id"], timestamp, periods)
    else:
        periods = commissioning.apply_corrected_gate(state, periods)

    global _last_energy_values
    if energy_values is not None:
        _last_energy_values = energy_values

    payload = {
        "part":           "connected",
        "meter_instance": meter["instance_name"],
        "modbus_address": meter["modbus_address"],
        "timestamp":      timestamp.isoformat(),
        "active_tariff":  accounting["tariff"],
        "absolute_kwh":   absolute_kwh,
        "last_delta_kwh": accounting["delta_kwh"],
        "energy":         _last_energy_values or {},
        "status":         state,
        "periods":        periods,
    }
    mqtt.mqtt_publish(mqtt._topic_base(meter["modbus_address"]) + "/energy",
                 payload, retain=True)


def publish_energy_disconnected(meter, db, state=None):
    timestamp = now_local()
    if state is None:
        state = commissioning.operational_state(db, meter["id"], timestamp)
    abs_kwh, last_delta, tariff = _last_valid_from_db(db, meter["id"])
    periods = build_periods(db, meter["id"], timestamp)
    if state == "active":
        periods = calculate_corrected(db, meter["id"], timestamp, periods)
    else:
        periods = commissioning.apply_corrected_gate(state, periods)

    payload = {
        "part":           "disconnected",
        "meter_instance": meter["instance_name"],
        "modbus_address": meter["modbus_address"],
        "timestamp":      timestamp.isoformat(),
        "active_tariff":  tariff or current_tariff(timestamp),
        "absolute_kwh":   abs_kwh,
        "last_delta_kwh": last_delta,
        "energy":         _last_energy_values or {},
        "status":         state,
        "periods":        periods,
    }
    mqtt.mqtt_publish(mqtt._topic_base(meter["modbus_address"]) + "/energy",
                 payload, retain=True)
