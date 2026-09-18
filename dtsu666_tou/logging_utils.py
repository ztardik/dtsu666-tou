"""Structured logging for the DTSU666 logger.

All diagnostics are written to **stderr**, which systemd captures in the
journal, with a severity and a ``dtsu666.*`` logger name:

    journalctl -u dtsu666 -p warning      # only problems
    journalctl -u dtsu666 -f              # follow everything

Informational console output (the startup banner, the once-a-minute
reading line and ``--test`` output) deliberately stays on **stdout**; it is
progress output for a human watching the terminal, not telemetry.
"""

import logging
import sys

LOGGER_NAME = "dtsu666"

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(level=None, stream=None):
    """Configure the ``dtsu666`` logger once and return it.

    *level* defaults to ``[LOGGING] level`` from dtsu666.conf (INFO).
    """
    if level is None:
        from .config import log_level
        level = log_level()
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler)
               for h in logger.handlers):
        handler = logging.StreamHandler(stream if stream is not None
                                        else sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        logger.addHandler(handler)
    return logger


def get_logger(name=None):
    """Return the package logger, or a named child of it."""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
