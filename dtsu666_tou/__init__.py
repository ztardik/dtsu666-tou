"""dtsu666_tou - CHINT DTSU666 Modbus RTU to MQTT / SQLite energy logger.

Reads one DTSU666 over Modbus RTU, publishes electrical data every second
and energy every minute, and persists all readings to SQLite.  The code is
organised as independent subsystems - see README.md for the module map.
"""

from .config import __version__  # noqa: F401
