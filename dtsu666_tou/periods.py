"""Calendar-based period summaries (day/week/month, current/previous)."""

from datetime import date, timedelta


# ============================================================
# Period summaries  (calendar-based)
# ============================================================

def _start_of_week(day):
    """Monday of the calendar week containing *day*."""
    return day - timedelta(days=day.weekday())


def _start_of_month(day):
    return day.replace(day=1)


def _next_month(day):
    year = day.year + (day.month == 12)
    month = (day.month % 12) + 1
    return day.replace(year=year, month=month, day=1)


PERIOD_RANGES = {
    "current_day":    lambda t: (t.date(), (t + timedelta(days=1)).date()),
    "previous_day":   lambda t: ((t - timedelta(days=1)).date(), t.date()),
    "current_week":   lambda t: (
        _start_of_week(t.date()),
        _start_of_week(t.date()) + timedelta(days=7),
    ),
    "previous_week":  lambda t: (
        _start_of_week(t.date()) - timedelta(days=7),
        _start_of_week(t.date()),
    ),
    "current_month":  lambda t: (
        _start_of_month(t.date()),
        _next_month(_start_of_month(t.date())),
    ),
    "previous_month": lambda t: (
        _start_of_month(_start_of_month(t.date()) - timedelta(days=1)),
        _start_of_month(t.date()),
    ),
}


def period_summary(db, meter_id, start_date, end_date):
    """Return {absolute_kwh, total_kwh, vt_kwh, nt_kwh} for a period."""
    row = db.execute(
        "SELECT COALESCE(SUM(total_kwh), 0) AS total_kwh, "
        "       COALESCE(SUM(vt_kwh), 0)    AS vt_kwh, "
        "       COALESCE(SUM(nt_kwh), 0)    AS nt_kwh "
        "FROM energy_daily "
        "WHERE meter_id = ? AND date >= ? AND date < ?",
        (meter_id, start_date.isoformat(), end_date.isoformat()),
    ).fetchone()

    absolute = db.execute(
        "SELECT absolute_kwh FROM readings "
        "WHERE meter_id = ? AND timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (meter_id, start_date.isoformat(), end_date.isoformat()),
    ).fetchone()

    return {
        "absolute_kwh": absolute["absolute_kwh"] if absolute else None,
        "total_kwh":    row["total_kwh"],
        "vt_kwh":       row["vt_kwh"],
        "nt_kwh":       row["nt_kwh"],
    }


def build_periods(db, meter_id, timestamp):
    """Build the six period summaries for the energy MQTT payload."""
    return {
        name: period_summary(db, meter_id, *fn(timestamp))
        for name, fn in PERIOD_RANGES.items()
    }
