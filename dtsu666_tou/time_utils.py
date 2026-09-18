"""Time helpers (Europe/Zagreb, DST-aware) and timestamp parsing."""

from datetime import datetime, timedelta

from .config import LOCAL_TZ


def now_local():
    """Return the current local (Europe/Zagreb) aware datetime."""
    return datetime.now(LOCAL_TZ)


def next_second_boundary(dt):
    """Return the next wall-clock **second** boundary (local time)."""
    naive = dt.replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    return naive.replace(tzinfo=LOCAL_TZ)


def next_minute_boundary(dt):
    """Return the next HH:MM:00 boundary (strictly in the future)."""
    naive = dt.replace(tzinfo=None, second=0, microsecond=0)
    if naive <= dt.replace(tzinfo=None):
        naive += timedelta(minutes=1)
    return naive.replace(tzinfo=LOCAL_TZ)


def local_to_epoch(dt):
    """Convert an aware local datetime for *sleep-duration* calculation.

    Uses system ``time.time()`` (UTC epoch) subtracted from the UTC
    equivalent of *dt*, which is safe across DST transitions when only
    used for relative sleep durations.
    """
    return dt.timestamp()


def _parse_timestamp(ts):
    """Parse an ISO timestamp and normalise it to LOCAL_TZ.

    ``datetime.fromisoformat`` returns a *fixed-offset* timezone for
    strings such as ``"2026-08-12T07:00:00+02:00"``, whose ``dst()`` is
    ``None``.  Normalising to LOCAL_TZ guarantees that downstream
    DST-aware code (e.g. ``current_tariff``) sees the correct rules.
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def _parse_correction_datetime(text):
    """Parse a local datetime string (Europe/Zagreb) into an aware dt."""
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        pass
    else:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(text, fmt)  # noqa: DTZ007
            return naive.replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    raise ValueError(f"cannot parse '{text}' as date/time")
