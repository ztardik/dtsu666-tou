#!/usr/bin/env python3
"""dtsu666_audit.py - read-only integrity & consistency audit for the
CHINT DTSU666 energy logger.

This tool NEVER writes to the production database.  It works on a
transactionally-consistent snapshot of the database taken through the
SQLite online-backup API, so a live WAL database is handled correctly.

It uses only the Python standard library, so it also works when the
application's virtual environment is unavailable.

Usage:
    dtsu666-audit [--db PATH] [--secrets PATH] [--tariffs PATH]
                  [--out-dir DIR] [--long-gap-seconds N] [--json]

Exit code:
    0  no errors (anomalies may still be present)
    1  confirmed corruption/errors found
    2  the audit itself could not be completed
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Zagreb")
DEFAULT_DB = "dtsu666_energy.db"
DEFAULT_SECRETS = "secrets.ini"
DEFAULT_TARIFFS = "tariffs.ini"

# Readings are expected once per wall-clock minute.
EXPECTED_INTERVAL_S = 60.0
# A gap larger than this is considered anomalous (2 missed minutes).
DEFAULT_LONG_GAP_S = 180.0
EPS = 1e-6


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _now():
    return datetime.now(LOCAL_TZ)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(ts):
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_readonly(path):
    """Open *path* read-only.  Raises sqlite3.Error on failure."""
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def consistent_snapshot(path, tmpdir):
    """Return the path of a consistent snapshot copy of the database.

    Uses the SQLite online-backup API, which is safe on a live WAL
    database.  Falls back to a plain file copy (+ -wal/-shm) only when
    the database cannot be opened read-only at all.
    """
    dest = os.path.join(tmpdir, "snapshot.db")
    try:
        src = open_readonly(path)
    except sqlite3.Error:
        src = None
    if src is not None:
        try:
            out = sqlite3.connect(dest)
            with out:
                src.backup(out)
            out.close()
            src.close()
            return dest
        except sqlite3.Error:
            try:
                src.close()
            except Exception:  # noqa: BLE001
                pass

    # Fallback: copy the database file and its WAL companions.
    shutil.copyfile(path, dest)
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.exists(path + suffix):
            shutil.copyfile(path + suffix, dest + suffix)
    return dest


def load_ini(path):
    cfg = configparser.ConfigParser()
    if path and os.path.exists(path):
        cfg.read(path)
    return cfg


# --------------------------------------------------------------------------
# configuration audit (credentials redacted)
# --------------------------------------------------------------------------

def config_audit(secrets_path, tariffs_path):
    """Return a redacted view of the runtime configuration."""
    out = {"secrets_file": secrets_path, "tariffs_file": tariffs_path,
           "present": os.path.exists(secrets_path)}
    cfg = load_ini(secrets_path)
    serial = {}
    mqtt = {}
    if cfg.has_section("SERIAL"):
        s = cfg["SERIAL"]
        serial = {
            "port": s.get("port"),
            "baudrate": s.get("baudrate"),
            "timeout": s.get("timeout"),
            "address": s.get("address"),
        }
    if cfg.has_section("MQTT"):
        m = cfg["MQTT"]
        mqtt = {
            "host": m.get("host"),
            "port": m.get("port"),
            "username_set": bool(m.get("username", "").strip()),
            "password_set": bool(m.get("password", "").strip()),
        }
    hep_count = 0
    if cfg.has_section("HEP Correction"):
        hep_count = len([v for v in cfg["HEP Correction"].values() if v.strip()])
    monitoring = {}
    if cfg.has_section("MONITORING"):
        monitoring = dict(cfg["MONITORING"])

    out["serial"] = serial
    out["mqtt"] = mqtt
    out["hep_corrections_configured"] = hep_count
    out["monitoring"] = monitoring
    out["secrets_file_mode"] = _mode_oct(secrets_path)

    tcfg = load_ini(tariffs_path)
    tariffs = {}
    for section in tcfg.sections():
        if section.startswith("COST") or section.startswith("TARIFF"):
            tariffs[section] = dict(tcfg[section])
    out["tariffs"] = tariffs
    return out


def _mode_oct(path):
    try:
        return oct(os.stat(path).st_mode & 0o777)
    except OSError:
        return None


# --------------------------------------------------------------------------
# SQLite integrity
# --------------------------------------------------------------------------

def sqlite_integrity(db, raw_path):
    out = {}
    out["integrity_check"] = [r[0] for r in db.execute("PRAGMA integrity_check")]
    out["quick_check"] = [r[0] for r in db.execute("PRAGMA quick_check")]
    out["foreign_key_violations"] = [
        dict(r) for r in db.execute("PRAGMA foreign_key_check")
    ]

    pragmas = {}
    for name in ("page_size", "page_count", "freelist_count", "journal_mode",
                 "auto_vacuum", "encoding", "user_version", "schema_version"):
        try:
            pragmas[name] = db.execute(f"PRAGMA {name}").fetchone()[0]
        except sqlite3.Error as exc:
            pragmas[name] = f"error: {exc}"
    out["pragmas"] = pragmas

    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    schema = {}
    for t in tables:
        cols = [{"name": c[1], "type": c[2], "notnull": bool(c[3]),
                 "pk": bool(c[5])} for c in db.execute(f"PRAGMA table_info({t})")]
        idx = [r[1] for r in db.execute(f"PRAGMA index_list({t})")]
        schema[t] = {"columns": cols, "indexes": idx}
    out["tables"] = schema

    wal = raw_path + "-wal"
    shm = raw_path + "-shm"
    out["wal_present"] = os.path.exists(wal)
    out["wal_bytes"] = os.path.getsize(wal) if os.path.exists(wal) else 0
    out["shm_present"] = os.path.exists(shm)
    out["shm_bytes"] = os.path.getsize(shm) if os.path.exists(shm) else 0
    out["db_bytes"] = os.path.getsize(raw_path)
    out["raw_db_sha256"] = _sha256(raw_path)
    return out


# --------------------------------------------------------------------------
# table inventory
# --------------------------------------------------------------------------

def table_counts(db):
    out = {}
    for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        out[name] = db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    return out


def meters(db):
    return [dict(r) for r in db.execute(
        "SELECT * FROM meters ORDER BY id")]


# --------------------------------------------------------------------------
# reading-level integrity
# --------------------------------------------------------------------------

def reading_integrity(db, meter_id, long_gap_s, max_listed=50):
    rows = list(db.execute(
        "SELECT id, timestamp, absolute_kwh, delta_kwh, tariff, vt_kwh, "
        "       nt_kwh, allocation_method FROM readings "
        "WHERE meter_id = ? ORDER BY timestamp, id", (meter_id,)))

    res = {
        "count": len(rows),
        "duplicate_timestamps": [],
        "non_monotonic": [],
        "long_gaps": [],
        "long_gap_count": 0,
        "negative_deltas": [],
        "counter_decreases": [],
        "large_jumps": [],
        "invariant_violations": [],
        "allocation_methods": {},
        "tariffs": {},
        "interval_seconds": {},
        "sum_delta": 0.0,
        "first": None,
        "last": None,
    }
    if not rows:
        return res

    res["allocation_methods"] = dict(db.execute(
        "SELECT allocation_method, COUNT(*) FROM readings WHERE meter_id = ? "
        "GROUP BY allocation_method", (meter_id,)).fetchall())
    res["tariffs"] = dict(db.execute(
        "SELECT tariff, COUNT(*) FROM readings WHERE meter_id = ? "
        "GROUP BY tariff", (meter_id,)).fetchall())

    seen_ts = {}
    prev = None
    gaps = []
    for r in rows:
        ts, abs_kwh, delta = r["timestamp"], float(r["absolute_kwh"]), float(r["delta_kwh"])
        seen_ts[ts] = seen_ts.get(ts, 0) + 1
        res["sum_delta"] += delta

        if abs(delta - (float(r["vt_kwh"]) + float(r["nt_kwh"]))) > EPS:
            if len(res["invariant_violations"]) < max_listed:
                res["invariant_violations"].append({
                    "timestamp": ts, "delta": delta,
                    "vt": r["vt_kwh"], "nt": r["nt_kwh"]})
        if delta < -EPS and len(res["negative_deltas"]) < max_listed:
            res["negative_deltas"].append({"timestamp": ts, "delta": delta})

        if prev is not None:
            try:
                cur = _parse(ts)
                prv = _parse(prev["timestamp"])
                secs = (cur - prv).total_seconds()
            except (ValueError, TypeError):
                secs = None
            if secs is not None:
                gaps.append(secs)
                if secs > long_gap_s:
                    res["long_gap_count"] += 1
                    if len(res["long_gaps"]) < max_listed:
                        res["long_gaps"].append({
                            "after": prev["timestamp"], "before": ts,
                            "seconds": round(secs, 1)})
            p_abs = float(prev["absolute_kwh"])
            if abs_kwh < p_abs - EPS and len(res["counter_decreases"]) < max_listed:
                res["counter_decreases"].append({
                    "timestamp": ts, "previous": p_abs, "current": abs_kwh,
                    "drop": round(p_abs - abs_kwh, 4)})
            step = abs_kwh - p_abs
            if step > 5.0 and len(res["large_jumps"]) < max_listed:
                res["large_jumps"].append({
                    "timestamp": ts, "previous": p_abs, "current": abs_kwh,
                    "jump": round(step, 4)})
        prev = r

    res["duplicate_timestamps"] = [
        {"timestamp": t, "count": n} for t, n in sorted(seen_ts.items())
        if n > 1][:max_listed]

    # Out-of-order detection.  The scan above orders by timestamp (which is
    # what the gap analysis needs), so it can never show a decrease; a row
    # inserted with a timestamp *earlier* than its predecessor is found by
    # ordering on the insertion id instead.
    res["non_monotonic"] = [
        dict(r) for r in db.execute(
            "SELECT id, timestamp, prev_timestamp FROM ("
            "  SELECT id, timestamp, "
            "         LAG(timestamp) OVER (ORDER BY id) AS prev_timestamp "
            "  FROM readings WHERE meter_id = ?) "
            "WHERE prev_timestamp IS NOT NULL AND timestamp < prev_timestamp "
            "LIMIT ?", (meter_id, max_listed))]

    res["first"] = {"timestamp": rows[0]["timestamp"],
                    "absolute_kwh": rows[0]["absolute_kwh"]}
    res["last"] = {"timestamp": rows[-1]["timestamp"],
                   "absolute_kwh": rows[-1]["absolute_kwh"]}
    if gaps:
        positive = sorted(g for g in gaps if g is not None)
        res["interval_seconds"] = {
            "min": round(positive[0], 1) if positive else None,
            "max": round(positive[-1], 1) if positive else None,
            "mean": round(sum(positive) / len(positive), 1) if positive else None,
            "expected": EXPECTED_INTERVAL_S,
        }
    res["counter_delta"] = round(
        float(rows[-1]["absolute_kwh"]) - float(rows[0]["absolute_kwh"]), 6)
    return res


# --------------------------------------------------------------------------
# aggregate consistency (readings vs energy_daily)
# --------------------------------------------------------------------------

def aggregate_consistency(db, meter_id):
    per_day = {}
    for r in db.execute(
            "SELECT substr(timestamp,1,10) AS day, "
            "       COALESCE(SUM(delta_kwh),0) AS total, "
            "       COALESCE(SUM(vt_kwh),0) AS vt, "
            "       COALESCE(SUM(nt_kwh),0) AS nt, "
            "       COUNT(*) AS n "
            "FROM readings WHERE meter_id = ? GROUP BY day ORDER BY day",
            (meter_id,)):
        per_day[r["day"]] = {"total": r["total"], "vt": r["vt"],
                             "nt": r["nt"], "n": r["n"]}

    stored = {r["date"]: dict(r) for r in db.execute(
        "SELECT date, absolute_kwh, total_kwh, vt_kwh, nt_kwh FROM energy_daily "
        "WHERE meter_id = ? ORDER BY date", (meter_id,))}

    mismatches = []
    for day in sorted(set(per_day) | set(stored)):
        calc = per_day.get(day, {"total": 0.0, "vt": 0.0, "nt": 0.0, "n": 0})
        st = stored.get(day)
        if st is None:
            mismatches.append({"date": day, "issue": "missing energy_daily row",
                               "calc_total": round(calc["total"], 4),
                               "calc_readings": calc["n"]})
            continue
        dt = calc["total"] - st["total_kwh"]
        dvt = calc["vt"] - st["vt_kwh"]
        dnt = calc["nt"] - st["nt_kwh"]
        if max(abs(dt), abs(dvt), abs(dnt)) > 1e-3:
            mismatches.append({
                "date": day, "issue": "aggregate mismatch",
                "d_total": round(dt, 4), "d_vt": round(dvt, 4),
                "d_nt": round(dnt, 4), "calc_readings": calc["n"]})

    return {
        "days_with_readings": len(per_day),
        "days_in_energy_daily": len(stored),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "per_day": {d: {"total": round(v["total"], 4),
                        "vt": round(v["vt"], 4), "nt": round(v["nt"], 4),
                        "readings": v["n"]} for d, v in sorted(per_day.items())},
    }


# --------------------------------------------------------------------------
# references / corrections
# --------------------------------------------------------------------------

def _parse_flexible(s):
    """Parse the [HEP Correction] datetime field (space or T separator,
    with or without seconds / timezone)."""
    s = s.strip()
    if not s:
        return None
    candidates = [s, s.replace(" ", "T", 1)]
    for cand in candidates:
        try:
            dt = datetime.fromisoformat(cand)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    return None


def references_and_corrections(db, meter_id, secrets_path):
    refs = [dict(r) for r in db.execute(
        "SELECT * FROM reference_readings WHERE meter_id = ? "
        "ORDER BY timestamp", (meter_id,))]
    corr = [dict(r) for r in db.execute(
        "SELECT * FROM corrections WHERE meter_id = ? ORDER BY date_from",
        (meter_id,))]

    ref_times = []
    for r in refs:
        try:
            ref_times.append(_parse(r["timestamp"]))
        except (ValueError, TypeError):
            pass

    cfg = load_ini(secrets_path)
    configured = []
    unmatched = []
    if cfg.has_section("HEP Correction"):
        for key, value in cfg["HEP Correction"].items():
            parts = [p.strip() for p in value.split(",", 3)]
            item = {"key": key}
            if len(parts) < 3:
                item["error"] = "needs at,vt,nt"
                configured.append(item)
                unmatched.append(item)
                continue
            at = _parse_flexible(parts[0])
            try:
                item["vt"] = float(parts[1])
                item["nt"] = float(parts[2])
            except ValueError:
                item["error"] = "bad numbers"
                configured.append(item)
                unmatched.append(item)
                continue
            item["at"] = at.isoformat(timespec="seconds") if at else parts[0]
            item["note"] = parts[3] if len(parts) > 3 else ""
            configured.append(item)
            if at is None:
                unmatched.append(item)
                continue
            matched = any(abs((at - rt).total_seconds()) < 60 for rt in ref_times)
            item["matched_reference_row"] = matched
            if not matched:
                unmatched.append(item)

    return {
        "reference_readings": refs,
        "reference_count": len(refs),
        "corrections": corr,
        "corrections_count": len(corr),
        "configured_corrections": configured,
        "configured_references_without_matching_row": unmatched,
    }


# --------------------------------------------------------------------------
# outage / liveness
# --------------------------------------------------------------------------

def outage_report(db, meter_id, now, long_gap_s=DEFAULT_LONG_GAP_S,
                  expected_s=EXPECTED_INTERVAL_S):
    """Liveness of the most recent reading plus an estimate of lost
    sampling time (sum of over-threshold gaps minus the expected
    interval)."""
    row = db.execute(
        "SELECT MAX(timestamp) AS last FROM readings WHERE meter_id = ?",
        (meter_id,)).fetchone()
    last = row["last"] if row else None
    age = None
    if last:
        try:
            age = round((now - _parse(last)).total_seconds(), 1)
        except (ValueError, TypeError):
            age = None

    downtime = 0.0
    gap_count = 0
    prev = None
    for r in db.execute(
            "SELECT timestamp FROM readings WHERE meter_id = ? "
            "ORDER BY timestamp", (meter_id,)):
        if prev is not None:
            try:
                gap = (_parse(r["timestamp"]) - _parse(prev)).total_seconds()
            except (ValueError, TypeError):
                gap = None
            if gap is not None and gap > long_gap_s:
                gap_count += 1
                downtime += gap - expected_s
        prev = r["timestamp"]

    return {
        "now": _iso(now),
        "last_reading": last,
        "age_seconds": age,
        "freshness_threshold_seconds": 240,
        "data_current": bool(age is not None and age <= 240),
        "long_gap_count": gap_count,
        "estimated_downtime_seconds": round(downtime, 1),
        "estimated_downtime_hours": round(downtime / 3600.0, 2),
        "estimated_missed_readings": int(round(downtime / expected_s)),
    }


# --------------------------------------------------------------------------
# findings + report
# --------------------------------------------------------------------------

SEVERITIES = ("error", "anomaly", "expected", "unverified")


def build_findings(report):
    f = []

    def add(sev, category, message, detail=""):
        f.append({"severity": sev, "category": category,
                  "message": message, "detail": detail})

    db = report["database"]
    ic = db["integrity_check"]
    if ic and ic != ["ok"]:
        add("error", "sqlite", "PRAGMA integrity_check reported problems",
            "; ".join(map(str, ic[:5])))
    else:
        add("expected", "sqlite", "PRAGMA integrity_check: ok")
    if db["foreign_key_violations"]:
        add("error", "sqlite", "foreign-key violations",
            json.dumps(db["foreign_key_violations"][:5]))
    else:
        add("expected", "sqlite", "foreign-key check: clean")

    r = report["readings"]
    if r["count"] == 0:
        add("error", "readings", "no readings stored")
    else:
        add("expected", "readings",
            f"{r['count']} readings from {r['first']['timestamp']} "
            f"to {r['last']['timestamp']}")

    for key, sev, label in (("duplicate_timestamps", "anomaly",
                             "duplicate reading timestamps"),
                            ("non_monotonic", "error",
                             "reading timestamps not monotonic"),
                            ("invariant_violations", "error",
                             "vt+nt != delta_kwh invariant violated"),
                            ("negative_deltas", "anomaly",
                             "negative energy delta"),
                            ("counter_decreases", "anomaly",
                             "cumulative counter decrease"),
                            ("large_jumps", "anomaly",
                             "unusually large counter jump")):
        items = r.get(key) or []
        if items:
            add(sev, "readings", f"{label}: {len(items)} (first shown)",
                json.dumps(items[:3]))
        else:
            add("expected", "readings", f"no {label}")

    agg = report["aggregate"]
    if agg["mismatch_count"]:
        add("error", "aggregate",
            f"{agg['mismatch_count']} day(s) where readings do not match "
            "energy_daily",
            json.dumps(agg["mismatches"][:5]))
    else:
        add("expected", "aggregate",
            f"all {agg['days_with_readings']} day(s) consistent with "
            "energy_daily")

    outage = report["outage"]
    if not outage["data_current"]:
        add("anomaly", "liveness",
            f"data is stale: last reading {outage['last_reading']} "
            f"({outage['age_seconds']}s ago)")
    else:
        add("expected", "liveness", "data is current")

    refs = report["references"]
    if refs["configured_references_without_matching_row"]:
        add("anomaly", "reference",
            "configured HEP correction(s) without a matching "
            "reference_readings row",
            json.dumps(refs["configured_references_without_matching_row"]))
    else:
        add("expected", "reference",
            f"all configured corrections have matching rows "
            f"({refs['reference_count']} reference row(s))")
    if refs["corrections_count"]:
        add("expected", "corrections",
            f"{refs['corrections_count']} manual correction row(s) present")

    cfg = report["config"]
    mode = cfg.get("secrets_file_mode")
    if cfg.get("present") and mode not in (None, "0o600", "0o400"):
        add("anomaly", "security",
            f"secrets file mode is {mode} (expected 0o600)")
    if not cfg.get("mqtt", {}).get("password_set"):
        add("anomaly", "security", "MQTT password is not set in secrets.ini")
    return f


def render_markdown(report):
    lines = []
    a = lines.append
    a(f"# DTSU666 audit - {report['generated_at']}")
    a("")
    a(f"- database: `{report['db_path']}`")
    a(f"- db sha256: `{report['db_sha256']}`")
    a(f"- access: read-only (consistent snapshot copy)")
    a("")
    counts = report["summary"]
    a(f"**{counts['error']} error(s), {counts['anomaly']} anomaly(ies), "
      f"{counts['expected']} check(s) ok, {counts['unverified']} unverified**")
    a("")
    a("## Findings")
    a("")
    a("| severity | category | finding | detail |")
    a("|---|---|---|---|")
    for f in report["findings"]:
        detail = str(f["detail"]).replace("|", "\\|").replace("\n", " ")[:200]
        a(f"| {f['severity']} | {f['category']} | {f['message']} | {detail} |")
    a("")
    a("## Database")
    a("")
    r = report["readings"]
    a(f"- readings: {r['count']}")
    if r["count"]:
        a(f"- first: {r['first']['timestamp']} @ {r['first']['absolute_kwh']} kWh")
        a(f"- last:  {r['last']['timestamp']} @ {r['last']['absolute_kwh']} kWh")
        a(f"- counter delta: {r['counter_delta']} kWh")
        iv = r.get("interval_seconds") or {}
        a(f"- interval s: min={iv.get('min')} mean={iv.get('mean')} "
          f"max={iv.get('max')} (expected {iv.get('expected')})")
        a(f"- allocation methods: {r['allocation_methods']}")
        a(f"- tariffs: {r['tariffs']}")
        a(f"- long gaps (> {report['long_gap_threshold_s']}s): "
          f"{r['long_gap_count']}")
    a("")
    a("## Tables")
    a("")
    for t, n in report["database"]["table_counts"].items():
        a(f"- `{t}`: {n} row(s)")
    a("")
    a("## Liveness")
    a("")
    o = report["outage"]
    a(f"- now: {o['now']}")
    a(f"- last reading: {o['last_reading']}")
    a(f"- age: {o['age_seconds']} s (freshness threshold "
      f"{o['freshness_threshold_seconds']} s)")
    a(f"- data current: {o['data_current']}")
    a(f"- long gaps: {o.get('long_gap_count')}")
    a(f"- estimated lost sampling time: "
      f"{o.get('estimated_downtime_hours')} h "
      f"({o.get('estimated_missed_readings')} readings)")
    a("")
    a("## Configuration (credentials redacted)")
    a("")
    a("```json")
    a(json.dumps(report["config"], indent=2, default=str))
    a("```")
    a("")
    a("## Per-day totals")
    a("")
    a("| date | readings | total kWh | VT kWh | NT kWh |")
    a("|---|---|---|---|---|")
    for day, v in report["aggregate"]["per_day"].items():
        a(f"| {day} | {v['readings']} | {v['total']} | {v['vt']} | {v['nt']} |")
    a("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dtsu666-audit",
        description="Read-only integrity and consistency audit of the "
                    "DTSU666 energy logger database.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--secrets", default=DEFAULT_SECRETS)
    ap.add_argument("--tariffs", default=DEFAULT_TARIFFS)
    ap.add_argument("--out-dir", default="audits")
    ap.add_argument("--long-gap-seconds", type=float, default=DEFAULT_LONG_GAP_S)
    ap.add_argument("--meter-id", type=int, default=None,
                    help="audit a specific meter (default: active meter)")
    ap.add_argument("--json", action="store_true",
                    help="print the JSON report to stdout")
    ap.add_argument("--no-write", action="store_true",
                    help="do not write report files")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    now = _now()
    tmpdir = tempfile.mkdtemp(prefix="dtsu666-audit-")
    try:
        snapshot = consistent_snapshot(args.db, tmpdir)
        db = sqlite3.connect(snapshot)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")

        report = {
            "tool": "dtsu666_audit",
            "tool_version": "1.0",
            "generated_at": _iso(now),
            "db_path": os.path.abspath(args.db),
            "db_sha256": _sha256(args.db),
            "long_gap_threshold_s": args.long_gap_seconds,
        }
        report["database"] = sqlite_integrity(db, args.db)
        report["database"]["table_counts"] = table_counts(db)
        report["meters"] = meters(db)

        mlist = report["meters"]
        meter = None
        if args.meter_id is not None:
            meter = next((m for m in mlist if m["id"] == args.meter_id), None)
        if meter is None:
            meter = next((m for m in mlist if m.get("active")), None)
        if meter is None and mlist:
            meter = mlist[0]
        if meter is None:
            print("ERROR: no meters configured in database", file=sys.stderr)
            return 2
        report["meter"] = {"id": meter["id"],
                           "instance_name": meter["instance_name"],
                           "modbus_address": meter["modbus_address"],
                           "initial_imp_ep": meter["initial_imp_ep"],
                           "active": meter["active"]}

        report["readings"] = reading_integrity(db, meter["id"],
                                               args.long_gap_seconds)
        report["aggregate"] = aggregate_consistency(db, meter["id"])
        report["references"] = references_and_corrections(db, meter["id"],
                                                          args.secrets)
        report["outage"] = outage_report(db, meter["id"], now)
        report["config"] = config_audit(args.secrets, args.tariffs)
        report["findings"] = build_findings(report)
        report["summary"] = {
            s: sum(1 for f in report["findings"] if f["severity"] == s)
            for s in SEVERITIES}
        db.close()

        stamp = now.strftime("%Y-%m-%d-%H%M%S")
        if not args.no_write:
            os.makedirs(args.out_dir, exist_ok=True)
            json_path = os.path.join(args.out_dir, f"audit-{stamp}.json")
            md_path = os.path.join(args.out_dir, f"audit-{stamp}-summary.md")
            with open(json_path, "w") as fh:
                json.dump(report, fh, indent=2, default=str)
            with open(md_path, "w") as fh:
                fh.write(render_markdown(report))
            report["json_path"] = json_path
            report["md_path"] = md_path

        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(render_markdown(report))

        return 1 if report["summary"]["error"] else 0
    except sqlite3.Error as exc:
        print(f"ERROR: audit failed: {exc}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
