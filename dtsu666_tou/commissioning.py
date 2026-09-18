"""Commissioning / reference-activation state machine.

Computes the single authoritative operational ``status`` that gates the
HEP-corrected counters.  The state is derived from stored acquisition data
and the ``[HEP Correction]`` configuration on every call - never persisted,
never dependent on process uptime - so a restart with a healthy history
remains ``active``.

States:
  disconnected - no fresh reading (meter communication unavailable)
  acquiring    - connected but the recent record is not yet continuous
  ready        - acquisition established; no reference configured
  missing      - reference configured but not yet usable
  active       - reference usable; corrected counters are valid

Corrected counters are only non-zero in ``active``.
"""

import os
from datetime import timedelta

from . import config
from .config import ESTIMATED_THRESHOLD, SECRETS_FILE
from .hep import _our_partial_sum, parse_hep_corrections
from .time_utils import _parse_timestamp

# Default staleness window; override with [MONITORING] freshness_seconds
# in dtsu666.conf (see config.freshness_seconds()).
FRESHNESS_SECONDS = 2 * ESTIMATED_THRESHOLD      # 240 s - latest reading staleness

COMMISSIONING_LOOKBACK = 300                     # seconds of continuous coverage required

# --- configuration predicate (mtime-cached) -------------------------------

_CONFIG_CACHE = {"path": None, "mtime": 0.0, "configured": False}


def reference_configured(config_path=SECRETS_FILE):
    """True when ``[HEP Correction]`` has at least one valid configured entry.

    Derived from the CONFIGURATION, not from ``reference_readings``, so a
    configured-but-not-yet-inserted reference is already ``missing`` rather
    than ``ready`` (no ``ready -> missing`` startup race).
    """
    try:
        mtime = os.path.getmtime(config_path)
    except OSError:
        return False
    if config_path != _CONFIG_CACHE["path"] or mtime != _CONFIG_CACHE["mtime"]:
        _CONFIG_CACHE.update(
            path=config_path,
            mtime=mtime,
            configured=bool(parse_hep_corrections(config_path)),
        )
    return _CONFIG_CACHE["configured"]


# --- data-driven predicates -----------------------------------------------

def _latest_reading(db, meter_id):
    row = db.execute(
        "SELECT timestamp FROM readings WHERE meter_id = ? ORDER BY id DESC LIMIT 1",
        (meter_id,),
    ).fetchone()
    return _parse_timestamp(row["timestamp"]) if row else None


def connected(db, meter_id, now):
    """True when the latest reading is within the freshness window.

    The window defaults to 240 s and can be tuned with
    ``[MONITORING] freshness_seconds`` in dtsu666.conf.
    """
    latest = _latest_reading(db, meter_id)
    if latest is None:
        return False
    return (now - latest).total_seconds() <= config.freshness_seconds()


def acquisition_established(db, meter_id, now):
    """True when connected AND the stored acquisition history already
    demonstrates enough data.

    Data-based and restart-safe: a reading must exist at least
    ``COMMISSIONING_LOOKBACK`` seconds before *now*.  First commissioning
    (no such history) therefore stays ``acquiring`` until it has accumulated
    LOOKBACK seconds of data, while a restart with a healthy surrounding
    history recovers immediately.  Reference usability is still guarded
    separately by ``reference_usable`` (interval coverage).
    """
    if not connected(db, meter_id, now):
        return False
    lookback_start = now - timedelta(seconds=COMMISSIONING_LOOKBACK)
    row = db.execute(
        "SELECT timestamp FROM readings WHERE meter_id = ? AND timestamp <= ? "
        "ORDER BY id DESC LIMIT 1",
        (meter_id, lookback_start.isoformat()),
    ).fetchone()
    return row is not None


def _load_anchors(db, meter_id):
    refs = db.execute(
        "SELECT timestamp, vt_kwh, nt_kwh FROM reference_readings "
        "WHERE meter_id = ? ORDER BY timestamp ASC",
        (meter_id,),
    ).fetchall()
    return [
        {"ts": _parse_timestamp(r["timestamp"]), "vt": r["vt_kwh"], "nt": r["nt_kwh"]}
        for r in refs
    ]


def _selected_anchor(anchors, now):
    """The anchor ``_hep_counter_at`` would use at *now*."""
    past = [a for a in anchors if a["ts"] <= now]
    if past:
        return past[-1]
    return anchors[0] if anchors else None


def reference_usable(db, meter_id, anchors, now):
    """True when the selected reference anchor exists and the reference-to-now
    interval is fully covered by stored readings (``_our_partial_sum`` reports
    no uncovered head/tail for either tariff)."""
    a = _selected_anchor(anchors, now)
    if a is None or a["ts"] > now:
        return False
    _, inc_v = _our_partial_sum(db, meter_id, a["ts"], now, "vt")
    _, inc_n = _our_partial_sum(db, meter_id, a["ts"], now, "nt")
    return (not inc_v) and (not inc_n)


def operational_state(db, meter_id, now, config_path=SECRETS_FILE):
    """Resolve the five-state operational status."""
    if not connected(db, meter_id, now):
        return "disconnected"
    if not acquisition_established(db, meter_id, now):
        return "acquiring"
    if not reference_configured(config_path):
        return "ready"
    if not reference_usable(db, meter_id, _load_anchors(db, meter_id), now):
        return "missing"
    return "active"


def apply_corrected_gate(state, periods):
    """Zero the corrected counters in every state except ``active``.

    ``active`` periods already carry the computed corrected values (set by
    ``calculate_corrected``); all other states get explicit 0.0 so no
    partially-reconstructed counter is ever exposed.
    """
    for name in ("current_day", "current_week", "current_month"):
        item = periods.get(name)
        if item is None:
            continue
        if state == "active":
            continue
        item["vt_corrected"] = 0.0
        item["nt_corrected"] = 0.0
    return periods
