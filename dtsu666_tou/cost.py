"""Electricity cost publishing (retained) with live tariff hot-reload.

Computes per-period gross (VAT-inclusive) cost breakdowns and publishes
them on ``Electricity/cost/{address}``.  Reloads ``tariffs.ini`` when its
mtime changes.
"""

import os

from datetime import timedelta

from . import mqtt
from .config import TARIFFS_FILE
from .time_utils import now_local
from .periods import PERIOD_RANGES, _start_of_month
from .pricing import load_tariffs, tariff_for_date, calculate_cost


_COST_LAST_MTIME = 0.0
_COST_CACHE = None   # parsed tariffs list


def _maybe_process_cost(meter):
    """Check tariffs.ini mtime; if changed, reload."""
    global _COST_LAST_MTIME, _COST_CACHE
    try:
        mtime = os.path.getmtime(TARIFFS_FILE)
    except OSError:
        return
    if mtime <= _COST_LAST_MTIME:
        return
    _COST_LAST_MTIME = mtime
    _COST_CACHE = load_tariffs()


def publish_cost(meter, db):
    """Publish retained ElectricityCost payload (all values gross/VAT-inclusive)."""
    _maybe_process_cost(meter)
    tariffs = _COST_CACHE
    if not tariffs:
        return

    now = now_local()

    def _range_for(name):
        if name in PERIOD_RANGES:
            return PERIOD_RANGES[name](now)
        cm = _start_of_month(now.date())
        if name == "last_6_months":
            start = (cm - timedelta(days=1)).replace(day=1)
            for _ in range(5):
                start = (start - timedelta(days=1)).replace(day=1)
            return (start, cm)
        if name == "last_year":
            start = (cm - timedelta(days=1)).replace(day=1)
            for _ in range(11):
                start = (start - timedelta(days=1)).replace(day=1)
            return (start, cm)
        return None

    cost_periods = {}
    _MONTHLY_ONLY = frozenset({"current_month", "previous_month",
                               "last_6_months", "last_year"})
    for name in ("current_day", "previous_day", "current_week",
                 "previous_week", "current_month", "previous_month",
                 "last_6_months", "last_year"):
        rng = _range_for(name)
        if rng is None:
            continue
        d = rng[0]
        end_d = rng[1]
        acc = None
        months_with_data = set()
        months_tariff = {}
        while d < end_d:
            row = db.execute(
                "SELECT vt_kwh, nt_kwh, total_kwh FROM energy_daily "
                "WHERE meter_id = ? AND date = ?",
                (meter["id"], d.isoformat()),
            ).fetchone()
            dv = row["vt_kwh"] if row else 0.0
            dn = row["nt_kwh"] if row else 0.0
            dt = row["total_kwh"] if row else 0.0

            t = tariff_for_date(tariffs, d)
            day_cost = calculate_cost(dv, dn, dt, t, False)
            if acc is None:
                acc = dict(day_cost)
            else:
                for k in acc:
                    acc[k] += day_cost[k]

            m_key = d.strftime("%Y-%m")
            months_tariff.setdefault(m_key, t)
            if row is not None:
                months_with_data.add(m_key)
            d += timedelta(days=1)

        # Per-month fixed fees: gross, once per calendar month WITH ENERGY DATA
        # only for month-based periods - daily/weekly show variable costs only
        if name in _MONTHLY_ONLY:
            for m_key in months_with_data:
                t = months_tariff[m_key]
                ops = t.get("opskrba_month", 0) or 0
                mm  = t.get("mjerno_mjesto_month", 0) or 0
                factor = 1.0 + (t.get("vat_percent", 0) or 0) / 100.0
                acc["opskrba"] = round(acc["opskrba"] + ops * factor, 4)
                acc["mjerno_mjesto"] = round(acc["mjerno_mjesto"] + mm * factor, 4)
                acc["subtotal"] = round(acc["subtotal"] + (ops + mm) * factor, 4)
                acc["total"] = round(acc["total"] + (ops + mm) * factor, 4)
                acc["vat"] = round(acc["vat"] + (ops + mm) * (factor - 1.0), 4)

        if acc is None:
            continue

        vt_v = 0.0; nt_v = 0.0; tot_v = 0.0
        dd = rng[0]
        while dd < rng[1]:
            row = db.execute(
                "SELECT vt_kwh, nt_kwh, total_kwh FROM energy_daily "
                "WHERE meter_id = ? AND date = ?",
                (meter["id"], dd.isoformat()),
            ).fetchone()
            vt_v += row["vt_kwh"] if row else 0.0
            nt_v += row["nt_kwh"] if row else 0.0
            tot_v += row["total_kwh"] if row else 0.0
            dd += timedelta(days=1)

        cost_periods[name] = {
            "vt_kwh": round(vt_v, 3), "nt_kwh": round(nt_v, 3),
            "total_kwh": round(tot_v, 3),
            "tariff_valid_from":
                tariff_for_date(tariffs, rng[0]).get("valid_from", "").isoformat(),
            **acc,
        }

    payload = {
        "timestamp": now.isoformat(),
        "currency": "EUR",
        "vat_percent": tariffs[0].get("vat_percent", 0) if tariffs else 0,
        "periods": cost_periods,
    }
    address = meter["modbus_address"]
    mqtt.mqtt_publish(f"Electricity/cost/{address}", payload, retain=True)
