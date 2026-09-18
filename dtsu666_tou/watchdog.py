"""systemd ``sd_notify`` integration: readiness, watchdog and status.

Only the small subset of the protocol we need is implemented, with a plain
``AF_UNIX`` datagram socket, so no external dependency (``systemd-python``)
is required.

Everything here is a no-op when the process was not started by systemd
(``$NOTIFY_SOCKET`` is unset), so running in a terminal or under a test
harness behaves exactly as before.
"""

import logging
import os
import socket

log = logging.getLogger("dtsu666.watchdog")


def enabled():
    """True when sd_notify has somewhere to send messages."""
    return bool(os.environ.get("NOTIFY_SOCKET"))


def notify(message):
    """Send one raw sd_notify datagram.  Never raises."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    # Abstract-namespace sockets are passed as '@name'; Python wants a
    # leading NUL byte instead of the '@'.
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
        return True
    except OSError as exc:
        log.debug("sd_notify(%s) failed: %s", message, exc)
        return False


def status_message(text):
    """Publish a human-readable status (shown by ``systemctl status``)."""
    return notify(f"STATUS={text}")


def ready(text=None):
    """Tell systemd the service finished starting up."""
    if text:
        status_message(text)
    return notify("READY=1")


def notify_stopping():
    return notify("STOPPING=1")


def watchdog_ping():
    """Reset the systemd watchdog timer."""
    return notify("WATCHDOG=1")


def watchdog_interval():
    """Watchdog interval in seconds (0 when disabled).

    ``WATCHDOG_USEC`` (set by systemd for this service) wins; otherwise
    ``[WATCHDOG] interval_seconds`` from dtsu666.conf is used.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    if usec:
        try:
            return max(0.0, int(usec) / 1_000_000.0)
        except ValueError:
            log.warning("Ignoring malformed WATCHDOG_USEC=%r", usec)
    try:
        from .config import watchdog_interval as _configured
        return max(0.0, float(_configured()))
    except Exception:  # noqa: BLE001
        return 0.0
