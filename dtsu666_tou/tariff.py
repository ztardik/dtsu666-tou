"""HEP Tariff (Europe/Zagreb, DST-aware).

VT/NT window selection and wall-clock proportional interval allocation.
"""

from datetime import datetime, timedelta
from datetime import time as dtime

from .config import LOCAL_TZ


def _is_dst_noon(day):
    """Return True if local noon on *day* is in summer (DST != 0)."""
    noon = datetime.combine(day, dtime(12, 0), tzinfo=LOCAL_TZ)
    return noon.dst() is not None and noon.dst() != timedelta(0)


def _vt_window(day):
    """Return (vt_start_hour, vt_end_hour) for the given calendar date."""
    if _is_dst_noon(day):
        return (8, 22)   # summer
    return (7, 21)       # winter


def current_tariff(dt):
    """Return "VT" or "NT" for the given datetime.

    The function normalises its input to ``LOCAL_TZ`` before classifying,
    so the result is deterministic regardless of how *dt* was constructed
    (``datetime.now()``, ``datetime.fromisoformat()``, a SQLite-stored
    timestamp, a foreign timezone, …).  A fixed-offset timezone — such as
    the ``+02:00`` produced by ``fromisoformat`` — carries no DST rules, so
    consulting ``dt.dst()`` directly would mis-classify summer/winter.

    Invariant: **naive datetimes are interpreted as local Europe/Zagreb
    time.**
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    else:
        dt = dt.astimezone(LOCAL_TZ)
    is_summer = dt.dst() is not None and dt.dst() != timedelta(0)
    if is_summer:
        return "VT" if 8 <= dt.hour < 22 else "NT"
    return "VT" if 7 <= dt.hour < 21 else "NT"


def allocate_interval(delta_kwh, t0, t1):
    """Split *delta_kwh* between VT/NT proportionally to **wall-clock**
    time in each tariff across the interval [t0, t1].

    Returns ``(vt_kwh, nt_kwh)``.  Guarantees ``vt+nt == delta``.
    """
    if delta_kwh <= 0:
        return 0.0, 0.0

    # Use naive local clock for wall-clock overlap (tariffs are
    # defined in local wall-clock, DST shifts are inside NT hours
    # so per-day noon-offset correctly selects the active schedule.)
    n0 = t0.replace(tzinfo=None)
    n1 = t1.replace(tzinfo=None)
    total_seconds = (n1 - n0).total_seconds()
    if total_seconds <= 0:
        return delta_kwh, 0.0

    # Walk calendar days
    d = n0.date()
    end_date = n1.date()
    vt_seconds = 0.0

    while d <= end_date:
        sh, eh = _vt_window(d)
        win_start = datetime.combine(d, dtime(sh, 0))
        win_end   = datetime.combine(d, dtime(eh, 0))
        overlap = max(
            0.0,
            (min(n1, win_end) - max(n0, win_start)).total_seconds(),
        )
        vt_seconds += overlap
        d += timedelta(days=1)

    fraction = vt_seconds / total_seconds
    vt = round(delta_kwh * fraction, 6)
    nt = round(delta_kwh - vt, 6)
    return max(vt, 0.0), max(nt, 0.0)
