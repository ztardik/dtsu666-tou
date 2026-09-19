#!/usr/bin/env python3
"""Generate a mock DTSU666 database mirroring a real audit report.

Produces a minute-resolution SQLite database in the project's own schema
(via ``dtsu666_tou.database``) that reproduces the key characteristics of a
real installation's audit:

- one meter (address 1), first reading 13077.66 kWh on 2026-08-11 15:22
- minute-resolution readings through 2026-09-19 04:22, ~465 kWh total
- a ~4.9-day outage (2026-09-13 13:30 -> 2026-09-18 10:54)
- a 53.68 kWh counter jump at the outage recovery (flagged by dtsu666-audit)
- a 37-minute gap on 2026-09-07 plus a couple of smaller gaps
- two HEP photo-reference anchors
- 36 energy_daily rows (built automatically by ``record_energy``)

The database file is gitignored (``*.db``).  Usage::

    .venv/bin/python tools/make_mock_db.py [output.db]

Then inspect it with::

    .venv/bin/python -m dtsu666_tou.web --db output.db
    dtsu666-audit --db output.db --secrets /dev/null --tariffs /dev/null
"""

import os
import sys
from datetime import datetime, timedelta

# Allow running straight from a source checkout without PYTHONPATH set.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from dtsu666_tou import database
from dtsu666_tou.config import LOCAL_TZ
from dtsu666_tou.tariff import current_tariff

DEFAULT_OUT = "mock_dtsu666_energy.db"

START = datetime(2026, 8, 11, 15, 22, 0, tzinfo=LOCAL_TZ)
END = datetime(2026, 9, 19, 4, 22, 0, tzinfo=LOCAL_TZ)

FIRST_KWH = 13077.66
LAST_KWH = 13542.94

JUMP_KWH = 53.68
JUMP_AT = datetime(2026, 9, 18, 10, 54, 0, tzinfo=LOCAL_TZ)

# (start, end) minute ranges excluded from the timeline.
GAPS = [
    # The ~4.9-day outage that produces the big counter jump on recovery.
    (datetime(2026, 9, 13, 13, 30, 0, tzinfo=LOCAL_TZ), JUMP_AT),
    # A 37-minute gap on 2026-09-07.
    (datetime(2026, 9, 7, 3, 0, 0, tzinfo=LOCAL_TZ),
     datetime(2026, 9, 7, 3, 37, 0, tzinfo=LOCAL_TZ)),
    # A couple of smaller gaps to produce additional "long gap" findings.
    (datetime(2026, 8, 19, 2, 10, 0, tzinfo=LOCAL_TZ),
     datetime(2026, 8, 19, 2, 17, 0, tzinfo=LOCAL_TZ)),
    (datetime(2026, 8, 27, 5, 5, 0, tzinfo=LOCAL_TZ),
     datetime(2026, 8, 27, 5, 12, 0, tzinfo=LOCAL_TZ)),
]

REFERENCE_READINGS = [
    (datetime(2026, 9, 1, 0, 10, 0, tzinfo=LOCAL_TZ), 13300.0, 0.0,
     "photo 1", "photo"),
    (datetime(2026, 9, 18, 10, 54, 0, tzinfo=LOCAL_TZ), 13523.94, 0.0,
     "photo 2", "photo"),
]


def _in_gap(t):
    return any(s <= t < e for s, e in GAPS)


def build_timeline():
    """Minute-aligned timestamps across the range, minus the gaps."""
    timeline = []
    t = START
    while t <= END:
        if not _in_gap(t):
            timeline.append(t)
        t += timedelta(minutes=1)
    return timeline


def make(path):
    db = database.open_database(path)
    mid = database.create_initial_meter(db, 1, FIRST_KWH)
    meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()

    timeline = build_timeline()
    # Every minute except the baseline and the jump minute carries a normal
    # delta; NT minutes carry 4x the load of VT minutes (night-heavy, as the
    # source audit shows), scaled so the total is exactly reproduced.
    normal = [t for t in timeline[1:] if t != JUMP_AT]
    n_vt = sum(1 for t in normal if current_tariff(t) == "VT")
    n_nt = len(normal) - n_vt
    total_normal = LAST_KWH - FIRST_KWH - JUMP_KWH
    vt_rate = total_normal / (n_vt + 4.0 * n_nt)
    nt_rate = 4.0 * vt_rate

    deltas = [nt_rate if current_tariff(t) == "NT" else vt_rate for t in normal]
    deltas[-1] += total_normal - sum(deltas)  # absorb float error exactly

    # Baseline reading.
    record_count = 1
    record_energy = database.record_energy
    record_energy(db, meter, timeline[0], FIRST_KWH)

    cum = FIRST_KWH
    delta_iter = iter(deltas)
    for t in timeline[1:]:
        if t == JUMP_AT:
            cum += JUMP_KWH
        else:
            cum += next(delta_iter)
        record_energy(db, meter, t, cum)
        record_count += 1
        if record_count % 10000 == 0:
            print(f"  ... {record_count} readings", file=sys.stderr)

    # HEP photo-reference anchors.
    now_iso = timeline[-1].isoformat()
    for ts, vt, nt, reason, source in REFERENCE_READINGS:
        db.execute(
            "INSERT INTO reference_readings "
            "(meter_id, timestamp, vt_kwh, nt_kwh, reason, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, ts.isoformat(), vt, nt, reason, source, now_iso),
        )
    db.commit()
    db.close()

    print(f"wrote {path}: {record_count} readings, "
          f"{cum:.2f} kWh final counter")
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    if os.path.exists(out):
        print(f"refusing to overwrite existing {out}", file=sys.stderr)
        sys.exit(1)
    make(out)
