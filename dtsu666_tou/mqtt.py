"""MQTT client (paho, Callback API version 2) and best-effort publisher.

Holds the live client in ``runtime.mqtt_client``.  ``mqtt_publish`` reads
it on every call (best-effort, never raises) so callers can monkey-patch
this function and affect all subsystems uniformly.
"""

import json
import logging

import paho.mqtt.client as mqttlib

from . import config, runtime
from .config import HA_STATUS_TOPIC

log = logging.getLogger("dtsu666.mqtt")

_MQTT_CV = mqttlib.CallbackAPIVersion.VERSION2


def publish_availability(address, available=True):
    """Publish the retained availability (birth / last-will) message.

    The payload is a plain string (``online``/``offline``), never a JSON
    object, so it can never be mistaken for a discovery config payload.
    """
    payload = (config.payload_available() if available
               else config.payload_not_available())
    mqtt_publish(config.availability_topic(address), payload, retain=True)


def _mqtt_on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.value == 0:
        log.info("MQTT connected")
        addr = userdata["address"] if isinstance(userdata, dict) else userdata
        try:
            client.subscribe(HA_STATUS_TOPIC)
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT subscribe failed: %s", exc)
        # Announce availability *before* discovery so HA never sees a
        # discovered entity while the device is still marked offline.
        try:
            publish_availability(addr, True)
            # local import to avoid a circular import at module load time
            from .discovery import publish_ha_discovery
            publish_ha_discovery(addr)
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT discovery publish failed: %s", exc)
    else:
        log.error("MQTT connect failed: rc=%s", reason_code)


def _mqtt_on_disconnect(client, userdata, flags, reason_code, properties):
    log.warning("MQTT disconnected (rc=%s); auto-reconnect is active", reason_code)


def _mqtt_on_message(client, userdata, msg):
    if msg.topic == HA_STATUS_TOPIC and msg.payload == b"online":
        try:
            addr = userdata["address"] if isinstance(userdata, dict) else userdata
            from .discovery import publish_ha_discovery
            publish_ha_discovery(addr)
            publish_availability(addr, True)
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT discovery republish failed: %s", exc)


def create_mqtt_client(cfg, address):
    """Return a non-blocking paho client with automatic reconnect
    and Home Assistant discovery."""
    client = mqttlib.Client(
        client_id=f"dtsu666_{address}",
        callback_api_version=_MQTT_CV,
    )
    client.on_connect = _mqtt_on_connect
    client.on_disconnect = _mqtt_on_disconnect
    client.on_message = _mqtt_on_message
    client.user_data_set({"address": address})

    # QoS-1 queue cap (avoid unbounded growth when broker is down)
    client.max_queued_messages_set(5000)

    # Auto-reconnect delay
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    host = cfg["MQTT"]["host"]
    port = int(cfg["MQTT"]["port"])
    username = cfg["MQTT"].get("username", "")
    if username:
        client.username_pw_set(username, cfg["MQTT"].get("password", ""))

    print(f"Connecting MQTT: {host}:{port}")

    # Last will and testament: the broker publishes this retained message
    # if the process dies without a clean disconnect, so Home Assistant
    # marks every entity unavailable instead of showing a stale status.
    client.will_set(config.availability_topic(address),
                    config.payload_not_available(), qos=1, retain=True)
    log.info("MQTT availability topic: %s",
             config.availability_topic(address))

    client.connect_async(host, port)
    client.loop_start()
    runtime.mqtt_client = client
    return client


def mqtt_publish(topic, payload, retain=False):
    """Best-effort publish; never raises."""
    if runtime.mqtt_client is None:
        return
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, separators=(",", ":"))
    try:
        runtime.mqtt_client.publish(topic, payload, qos=1, retain=retain)
    except Exception as exc:  # noqa: BLE001
        log.warning("MQTT publish failed (%s): %s", topic, exc)


# ============================================================
# MQTT topics
# ============================================================

def _topic_base(address):
    return f"Electricity/dtsu666/{address}"
