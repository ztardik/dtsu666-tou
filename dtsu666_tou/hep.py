"""HEP photo-reference corrections.

Parse [HEP Correction] entries from secrets.ini, insert them as
reference_readings anchors, and reconstruct cumulative HEP counters
(vt_corrected / nt_corrected) for period summaries.  Raw historical
data is never overwritten.
"""

import configparser as _cp
from datetime import datetime
from datetime import time as dtime

from .config import ESTIMATED_THRESHOLD, LOCAL_TZ, SECRETS_FILE
from .periods import PERIOD_RANGES, period_summary
from .tariff import allocate_interval
from .time_utils import _parse_correction_datetime, _parse_timestamp, now_local

# ============================================================
# HEP photo-reference corrections  (secrets.ini driven)
# ============================================================

def parse_hep_corrections(config_path):
    """Read [HEP Correction] from secrets.ini.

    Returns a list of dicts: {at: aware datetime, vt: float, nt: float,
    reason: str}.  Skips lines that don't parse (logged).
    """
    cfg = _cp.ConfigParser()
    if not cfg.read(config_path):
        return []

    entries = []
    try:
        items = cfg.items("HEP Correction")
    except KeyError:
        # Section not present (Python < 3.13)
        return []
    except _cp.NoSectionError:
        # Section not present (Python >= 3.13)
        return []

    for key, value in items:
        value = value.strip()
        if not value:
            continue
        # format: <datetime>, <VT counter>, <NT counter>[, reason]
        parts = [p.strip() for p in value.split(",", 3)]
        if len(parts) < 3:
            print(f"[HEP Correction] malformed '{key}': need at,vt,nt")
            continue
        at_str, vt_str, nt_str = parts[0], parts[1], parts[2]
        reason = parts[3] if len(parts) > 3 else "photo of HEP meter"

        try:
            at = _parse_correction_datetime(at_str)
        except ValueError as exc:
            print(f"[HEP Correction] bad datetime '{at_str}' ({key}): {exc}")
            continue

        try:
            vt = float(vt_str)
            nt = float(nt_str)
        except ValueError:
            print(f"[HEP Correction] bad numbers '{key}' (vt,nt)")
            continue
        if vt < 0 or nt < 0:
            print(f"[HEP Correction] negative values '{key}'")
            continue
        if vt == 0 and nt == 0:
            print(f"[HEP Correction] both zero on '{key}' - skipped")
            continue

        entries.append({
            "at": at, "vt": vt, "nt": nt, "reason": reason,
        })

    entries.sort(key=lambda e: e["at"])
    return entries


def process_hep_corrections(db, meter, config_path=SECRETS_FILE):
    """Parse [HEP Correction] from secrets.ini and insert new entries
    into reference_readings.  Returns number of newly inserted rows."""
    entries = parse_hep_corrections(config_path)
    inserted = 0
    mid = meter["id"]

    for e in entries:
        ts_iso = e["at"].isoformat()
        existing = db.execute(
            "SELECT id FROM reference_readings "
            "WHERE meter_id = ? AND timestamp = ?",
            (mid, ts_iso),
        ).fetchone()
        if existing:
            continue

        db.execute(
            "INSERT OR IGNORE INTO reference_readings "
            "(meter_id, timestamp, vt_kwh, nt_kwh, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, ts_iso, e["vt"], e["nt"],
             e["reason"], now_local().isoformat()),
        )
        inserted += 1
        print(
            f"[HEP Correction] anchor: "
            f"{ts_iso}  VT={e['vt']:.1f} kWh  NT={e['nt']:.1f} kWh"
        )

    if inserted:
        db.commit()
    return inserted


def _first_reading_date(db, meter_id):
    row = db.execute(
        "SELECT timestamp FROM readings WHERE meter_id = ? "
        "ORDER BY timestamp ASC LIMIT 1",
        (meter_id,),
    ).fetchone()
    if row is None:
        return None
    return _parse_timestamp(row["timestamp"])


def _hep_counter_at(anchors, ref_time, key, meter_id, db):
    """Reconstructed HEP cumulative counter at *ref_time*.

    Returns ``(value, incomplete)`` where *value* is the reconstructed
    counter (None when there are no anchors) and *incomplete* marks an
    interval for which part of the meter allocation is unavailable.
    """
    if not anchors:
        return None, False
    past = [a for a in anchors if a["ts"] <= ref_time]
    if past:
        a = past[-1]
        if ref_time <= a["ts"]:
            return a[key], False
        extra, incomplete = _our_partial_sum(db, meter_id, a["ts"], ref_time, key)
        return a[key] + extra, incomplete
    else:
        a = anchors[0]
        if ref_time >= a["ts"]:
            return a[key], False
        extra, incomplete = _our_partial_sum(db, meter_id, ref_time, a["ts"], key)
        return a[key] - extra, incomplete


def _our_partial_sum(db, meter_id, t_from, t_to, key):
    """Reconstruct consumption of *key* ('vt'|'nt') between t_from and t_to.

    Returns ``(kwh, incomplete)``.

    The minute-level ``readings`` rows are the authoritative source.  Rows
    fully inside the requested interval contribute their stored
    ``<key>_kwh`` allocation unchanged, preserving both the ``measured``
    exactness and the logger's own ``estimated`` allocations for scheduler
    gaps (the cumulative-counter delta across a gap is already captured by
    the next reading, so no energy is lost and no second estimation is
    performed).

    A row whose interval crosses *t_from* is re-allocated proportionally
    over the truncated sub-interval using ``allocate_interval`` - the same
    tariff-timing logic the logger uses for estimated intervals.

    Portions of the requested interval for which no meter data exists (a
    head before the first reading, a tail after the last reading, or no
    rows at all) contribute nothing; *incomplete* is set True whenever an
    uncovered part is larger than ``ESTIMATED_THRESHOLD`` seconds.

    Contract: an empty interval (``t_from == t_to``) is complete (``(0.0,
    False)``); a reversed interval (``t_from > t_to``) is invalid input and
    raises ``ValueError``.
    """
    if t_to < t_from:
        raise ValueError("_our_partial_sum: t_from must be <= t_to")
    if t_to == t_from:
        return 0.0, False

    rows = db.execute(
        """
        SELECT id, timestamp, absolute_kwh, delta_kwh, vt_kwh, nt_kwh, prev_ts
        FROM (
            SELECT r.id, r.timestamp, r.absolute_kwh, r.delta_kwh, r.vt_kwh, r.nt_kwh,
                   LAG(r.timestamp) OVER (PARTITION BY r.meter_id ORDER BY r.id) AS prev_ts
            FROM readings r
            WHERE r.meter_id = ?
        )
        WHERE timestamp > ? AND timestamp <= ?
        ORDER BY id
        """,
        (meter_id, t_from.isoformat(), t_to.isoformat()),
    ).fetchall()

    if not rows:
        return 0.0, True

    total = 0.0
    incomplete = False

    # Head: the interval may start before the first row's coverage.
    first_prev = _parse_timestamp(rows[0]["prev_ts"]) if rows[0]["prev_ts"] else None
    if first_prev is None or (first_prev - t_from).total_seconds() > ESTIMATED_THRESHOLD:
        incomplete = True

    for r in rows:
        if r["prev_ts"] is None:
            continue                      # baseline row: nothing allocated yet
        ts = _parse_timestamp(r["timestamp"])
        prev_ts = _parse_timestamp(r["prev_ts"])
        os_ = max(prev_ts, t_from)
        oe_ = min(ts, t_to)               # ts <= t_to by the query filter
        if oe_ <= os_:
            continue
        if prev_ts >= t_from:
            total += r[f"{key}_kwh"]
        else:
            vt, nt = allocate_interval(r["delta_kwh"], os_, oe_)
            total += vt if key == "vt" else nt

    # Tail: the interval may extend past the last row.
    last_ts = _parse_timestamp(rows[-1]["timestamp"])
    if (t_to - last_ts).total_seconds() > ESTIMATED_THRESHOLD:
        incomplete = True

    return total, incomplete


def apply_reference_corrections_calc(db, meter_id, timestamp, periods):
    """Compute corrected period vt/nt as reconstructed HEP counters."""
    ranges = {name: fn(timestamp) for name, fn in PERIOD_RANGES.items()}

    refs = db.execute(
        "SELECT timestamp, vt_kwh, nt_kwh FROM reference_readings "
        "WHERE meter_id = ? ORDER BY timestamp ASC",
        (meter_id,),
    ).fetchall()

    anchors = [{
        "ts": _parse_timestamp(r["timestamp"]),
        "vt": r["vt_kwh"],
        "nt": r["nt_kwh"],
    } for r in refs]

    result = {}
    for name, (p_start, p_end) in ranges.items():
        s = period_summary(db, meter_id, p_start, p_end)
        item = dict(s)

        ref_time = now_local() if name.startswith("current_") else datetime.combine(p_end, dtime(0, 0), tzinfo=LOCAL_TZ)
        cv, inc_v = _hep_counter_at(anchors, ref_time, "vt", meter_id, db)
        cn, inc_n = _hep_counter_at(anchors, ref_time, "nt", meter_id, db)

        if name.startswith("previous_"):
            item.pop("absolute_kwh", None)
        else:
            item["vt_corrected"] = round(cv, 3) if cv is not None else item["vt_kwh"]
            item["nt_corrected"] = round(cn, 3) if cn is not None else item["nt_kwh"]
            if inc_v or inc_n:
                print(
                    f"WARNING: HEP-corrected {name} is incomplete (no meter data "
                    f"for part of the reference interval): "
                    f"vt={item['vt_corrected']} nt={item['nt_corrected']}"
                )
        result[name] = item

    return result


def calculate_corrected(db, meter_id, timestamp, periods):
    """Compute vt_corrected/nt_corrected using HEP photo references."""
    return apply_reference_corrections_calc(db, meter_id, timestamp, periods)
