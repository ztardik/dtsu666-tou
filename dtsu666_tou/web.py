"""On-demand, read-only web status page.

Run ``python -m dtsu666_tou.web`` to serve a single self-contained status
page plus a JSON endpoint.  The process is launched only when you want to
look at the logger and stopped with Ctrl-C; it opens the SQLite database
read-only, so it never interferes with the running service and still works
while the service is stopped.

Standard library only: this module and everything it imports (config,
periods, pricing, tariff, commissioning, hep, time_utils) is free of the
paho/serial dependencies, so it runs on the system Python as well as the
service virtualenv.

Binds to 127.0.0.1 by default; pass --host/--port to expose it further.
Credentials are never read or displayed - only the public tariff rates and
the ``[HEP Correction]`` presence are consulted.
"""

import argparse
import html
import json
import os
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import commissioning, config, hep, periods, pricing, tariff
from .config import DATABASE_FILE, RUNTIME_CONF_FILE, SECRETS_FILE, TARIFFS_FILE
from .time_utils import _parse_timestamp, now_local

__all__ = ["open_readonly", "build_status", "serve", "main"]


# ============================================================
# Read-only database access
# ============================================================

def open_readonly(path):
    """Open *path* read-only.  Raises ``sqlite3.Error`` when unavailable.

    Tries ``mode=ro`` first so a live WAL database is read correctly,
    including any frames not yet checkpointed from the ``-wal`` file.  When
    that fails and there is no ``-wal`` file (a cleanly closed database),
    falls back to ``immutable=1``, which needs no ``-shm`` file and therefore
    works on a WAL database that is not currently being written to even when
    the process lacks write permission on its directory (SQLite otherwise
    refuses to create the shared-memory index).  A live ``-wal`` file is
    never read via ``immutable=1``: that would silently show stale,
    checkpointed data for a running service.

    ``sqlite3.connect`` opens lazily, so a harmless PRAGMA forces the file
    open here to surface errors at this point rather than on the first query.
    """
    abspath = os.path.abspath(path)
    try:
        db = sqlite3.connect(f"file:{abspath}?mode=ro", uri=True)
        db.execute("PRAGMA user_version")
    except sqlite3.Error:
        if os.path.exists(abspath + "-wal"):
            raise
        db = sqlite3.connect(f"file:{abspath}?mode=ro&immutable=1", uri=True)
        db.execute("PRAGMA user_version")
    db.row_factory = sqlite3.Row
    return db


# ============================================================
# Status snapshot
# ============================================================

def _last_reading(db, meter_id):
    return db.execute(
        "SELECT absolute_kwh, delta_kwh, tariff, timestamp, allocation_method "
        "FROM readings WHERE meter_id = ? ORDER BY id DESC LIMIT 1",
        (meter_id,),
    ).fetchone()


def _sum_daily(db, meter_id, start_date, end_date):
    row = db.execute(
        "SELECT COALESCE(SUM(total_kwh), 0) AS total, "
        "COALESCE(SUM(vt_kwh), 0) AS vt, "
        "COALESCE(SUM(nt_kwh), 0) AS nt "
        "FROM energy_daily WHERE meter_id = ? AND date >= ? AND date < ?",
        (meter_id, start_date.isoformat(), end_date.isoformat()),
    ).fetchone()
    return row["total"], row["vt"], row["nt"]


def _cost_summary(db, meter_id, now, tariffs):
    """Return {period_name: {…}} or None when no tariffs are configured."""
    if not tariffs:
        return None
    out = {}
    for name, monthly in (("current_day", False), ("current_month", True)):
        start, end = periods.PERIOD_RANGES[name](now)
        total, vt, nt = _sum_daily(db, meter_id, start, end)
        t = pricing.tariff_for_date(tariffs, start)
        cost = pricing.calculate_cost(vt, nt, total, t, monthly)
        cost["vt_kwh"] = round(vt, 3)
        cost["nt_kwh"] = round(nt, 3)
        cost["total_kwh"] = round(total, 3)
        cost["tariff_valid_from"] = (
            t.get("valid_from").isoformat() if t and t.get("valid_from") else ""
        )
        out[name] = cost
    return out


def build_status(db, secrets_path, tariffs_path):
    """Return a JSON-ready status snapshot for the active meter."""
    now = now_local()
    meters = db.execute("SELECT * FROM meters ORDER BY id").fetchall()
    active = [m for m in meters if m["active"] == 1]
    primary = active[-1] if active else (meters[-1] if meters else None)

    status = {
        "generated_at": now.isoformat(),
        "version": config.__version__,
        "meters": [dict(m) for m in meters],
        "active_meter_id": primary["id"] if primary is not None else None,
        "operational": None,
        "energy": None,
        "periods": None,
        "cost": None,
        "reference_readings": [],
        "corrections": [],
        "recent_readings": [],
        "config": {
            "retention_minute_days": config.retention_minute_days(),
            "retention_daily_days": config.retention_daily_days(),
            "freshness_seconds": config.freshness_seconds(),
            "db_busy_timeout_ms": config.db_busy_timeout_ms(),
            "tariffs_loaded": False,
        },
    }

    if primary is None:
        return status

    mid = primary["id"]
    last = _last_reading(db, mid)

    state = commissioning.operational_state(db, mid, now, config_path=secrets_path)
    last_at = _parse_timestamp(last["timestamp"]) if last else None
    status["operational"] = {
        "status": state,
        "connected": commissioning.connected(db, mid, now),
        "last_reading_at": last_at.isoformat() if last_at else None,
        "age_seconds": round((now - last_at).total_seconds(), 1) if last_at else None,
        "freshness_seconds": config.freshness_seconds(),
    }

    status["energy"] = {
        "absolute_kwh": last["absolute_kwh"] if last else None,
        "last_delta_kwh": last["delta_kwh"] if last else None,
        "last_tariff": last["tariff"] if last else None,
        "current_tariff": tariff.current_tariff(now),
    }

    raw_periods = periods.build_periods(db, mid, now)
    if state == "active":
        status["periods"] = hep.calculate_corrected(db, mid, now, raw_periods)
    else:
        status["periods"] = commissioning.apply_corrected_gate(state, raw_periods)

    tariffs = pricing.load_tariffs(tariffs_path)
    status["config"]["tariffs_loaded"] = bool(tariffs)
    status["cost"] = _cost_summary(db, mid, now, tariffs)

    status["reference_readings"] = [
        dict(r) for r in db.execute(
            "SELECT timestamp, vt_kwh, nt_kwh, reason, source "
            "FROM reference_readings WHERE meter_id = ? ORDER BY timestamp",
            (mid,),
        ).fetchall()
    ]
    status["corrections"] = [
        dict(c) for c in db.execute(
            "SELECT date_from, date_to, vt_kwh, nt_kwh, note "
            "FROM corrections WHERE meter_id = ? ORDER BY date_from",
            (mid,),
        ).fetchall()
    ]
    status["recent_readings"] = [
        dict(r) for r in db.execute(
            "SELECT timestamp, absolute_kwh, delta_kwh, tariff, vt_kwh, nt_kwh, "
            "allocation_method FROM readings WHERE meter_id = ? "
            "ORDER BY id DESC LIMIT 20",
            (mid,),
        ).fetchall()
    ]
    return status


# ============================================================
# HTML rendering
# ============================================================

def _esc(v):
    return "–" if v is None else html.escape(str(v))


def _f(v, dec=3):
    return "–" if v is None else f"{v:.{dec}f}"


_STATE_CLASS = {
    "disconnected": "bad",
    "acquiring": "warn",
    "ready": "info",
    "missing": "warn",
    "active": "good",
}

_PERIOD_ORDER = [
    "current_day", "previous_day", "current_week",
    "previous_week", "current_month", "previous_month",
]
_PERIOD_TITLES = {
    "current_day": "Today", "previous_day": "Yesterday",
    "current_week": "Current week", "previous_week": "Previous week",
    "current_month": "Current month", "previous_month": "Previous month",
}


def render_html(status):
    """Return the full status page as an HTML document."""
    if "error" in status:
        detail = f"<p class='sub'>{_esc(status.get('detail'))}</p>" \
            if status.get("detail") else ""
        return _page(f"<p class='bad'>{_esc(status['error'])}</p>{detail}")

    op = status["operational"] or {}
    state = op.get("status", "unknown")
    energy = status["energy"] or {}
    meters = status["meters"] or []
    active_id = status["active_meter_id"]

    meter_rows = "".join(
        f"<tr><td>{_esc(m['instance_name'])}</td>"
        f"<td>{_esc(m['modbus_address'])}</td>"
        f"<td>{_esc(m['valid_from'])}</td>"
        f"<td>{_esc(m['valid_to'])}</td>"
        f"<td>{_esc(m['replacement_reason'])}</td>"
        f"<td>{'active' if m['id'] == active_id else ''}</td></tr>"
        for m in meters
    )

    period_rows = []
    for name in _PERIOD_ORDER:
        p = (status["periods"] or {}).get(name)
        if p is None:
            continue
        corr_vt = _f(p.get("vt_corrected"))
        corr_nt = _f(p.get("nt_corrected"))
        period_rows.append(
            f"<tr><td>{_PERIOD_TITLES[name]}</td>"
            f"<td>{_f(p.get('total_kwh'))}</td>"
            f"<td>{_f(p.get('vt_kwh'))}</td>"
            f"<td>{_f(p.get('nt_kwh'))}</td>"
            f"<td>{corr_vt}</td><td>{corr_nt}</td></tr>"
        )

    cost_rows = ""
    if status["cost"]:
        for name, title in (("current_day", "Today"), ("current_month", "This month")):
            c = status["cost"].get(name)
            if not c:
                continue
            cost_rows += (
                f"<tr><td>{title}</td>"
                f"<td>{_f(c.get('vt_kwh'))}</td>"
                f"<td>{_f(c.get('nt_kwh'))}</td>"
                f"<td>{_f(c.get('total_kwh'))}</td>"
                f"<td>{_f(c.get('total'), 4)} €</td></tr>"
            )

    reading_rows = "".join(
        f"<tr><td>{_esc(r['timestamp'])}</td>"
        f"<td>{_f(r['absolute_kwh'])}</td>"
        f"<td>{_f(r['delta_kwh'])}</td>"
        f"<td>{_esc(r['tariff'])}</td>"
        f"<td>{_f(r['vt_kwh'])}</td><td>{_f(r['nt_kwh'])}</td>"
        f"<td>{_esc(r['allocation_method'])}</td></tr>"
        for r in (status["recent_readings"] or [])
    )

    ref_rows = "".join(
        f"<tr><td>{_esc(r['timestamp'])}</td>"
        f"<td>{_f(r['vt_kwh'])}</td><td>{_f(r['nt_kwh'])}</td>"
        f"<td>{_esc(r.get('reason'))}</td></tr>"
        for r in (status["reference_readings"] or [])
    )
    corr_rows = "".join(
        f"<tr><td>{_esc(c['date_from'])}</td><td>{_esc(c['date_to'])}</td>"
        f"<td>{_f(c['vt_kwh'])}</td><td>{_f(c['nt_kwh'])}</td>"
        f"<td>{_esc(c.get('note'))}</td></tr>"
        for c in (status["corrections"] or [])
    )

    cfg = status["config"]
    age = op.get("age_seconds")
    age_txt = f"{age} s ago" if age is not None else "no reading yet"

    body = f"""
<div class="status {_STATE_CLASS.get(state, 'bad')}">{_esc(state.upper())}</div>
<p class="sub">version {_esc(status['version'])} · generated {_esc(status['generated_at'])}</p>

<h2>Operational</h2>
<table class="kv">
  <tr><th>Status</th><td>{_esc(state)}</td></tr>
  <tr><th>Connected</th><td>{'yes' if op.get('connected') else 'no'}</td></tr>
  <tr><th>Last reading</th><td>{_esc(op.get('last_reading_at'))} ({_esc(age_txt)})</td></tr>
  <tr><th>Freshness window</th><td>{_esc(op.get('freshness_seconds'))} s</td></tr>
</table>

<h2>Energy</h2>
<table class="kv">
  <tr><th>ImpEp (absolute)</th><td>{_f(energy.get('absolute_kwh'))} kWh</td></tr>
  <tr><th>Last delta</th><td>{_f(energy.get('last_delta_kwh'))} kWh</td></tr>
  <tr><th>Last tariff</th><td>{_esc(energy.get('last_tariff'))}</td></tr>
  <tr><th>Current tariff</th><td>{_esc(energy.get('current_tariff'))}</td></tr>
</table>

<h2>Meters</h2>
<table>
  <tr><th>Instance</th><th>Addr</th><th>Valid from</th><th>Valid to</th>
      <th>Reason</th><th>State</th></tr>
  {meter_rows}
</table>

<h2>Periods (kWh)</h2>
<table>
  <tr><th>Period</th><th>Total</th><th>VT</th><th>NT</th>
      <th>VT corr.</th><th>NT corr.</th></tr>
  {''.join(period_rows)}
</table>

<h2>Cost (gross, VAT included)</h2>
{('<table><tr><th>Period</th><th>VT kWh</th><th>NT kWh</th><th>Total kWh</th><th>Total</th></tr>' + cost_rows + '</table>') if cost_rows else '<p>No tariffs.ini configured.</p>'}

<h2>HEP references</h2>
{('<table><tr><th>Timestamp</th><th>VT kWh</th><th>NT kWh</th><th>Reason</th></tr>' + ref_rows + '</table>') if ref_rows else '<p>None.</p>'}

<h2>Manual corrections</h2>
{('<table><tr><th>From</th><th>To</th><th>VT kWh</th><th>NT kWh</th><th>Note</th></tr>' + corr_rows + '</table>') if corr_rows else '<p>None.</p>'}

<h2>Recent readings</h2>
<table>
  <tr><th>Timestamp</th><th>Abs kWh</th><th>Δ kWh</th><th>Tariff</th>
      <th>VT</th><th>NT</th><th>Method</th></tr>
  {reading_rows}
</table>

<p class="sub">retention: {cfg['retention_minute_days']} d (minute) /
{cfg['retention_daily_days']} d (daily) · tariffs:
{'yes' if cfg['tariffs_loaded'] else 'no'}</p>
"""
    return _page(body)


_PAGE_CSS = """
body { font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 960px; }
h1 { margin-bottom: 0; }
h2 { border-bottom: 1px solid #ccc; padding-bottom: .2rem; margin-top: 1.6rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
th, td { border: 1px solid #ddd; padding: .3rem .5rem; text-align: left; }
th { background: #f5f5f5; }
table.kv { width: auto; }
table.kv th { background: none; border: none; padding-right: 1rem; }
table.kv td { border: none; }
.sub { color: #666; font-size: .9rem; }
.status { display: inline-block; padding: .3rem .7rem; border-radius: 4px;
          font-weight: bold; color: #fff; }
.status.good { background: #2e7d32; }
.status.info { background: #1565c0; }
.status.warn { background: #ef6c00; }
.status.bad  { background: #c62828; }
"""


def _page(body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>CHINT DTSU666 — status</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<h1>CHINT DTSU666 — status</h1>
{body}
</body>
</html>"""


# ============================================================
# HTTP server
# ============================================================

class _Handler(BaseHTTPRequestHandler):
    server_version = "dtsu666-web"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve("text/html; charset=utf-8", render_html(self._status()))
        elif path == "/api/status":
            self._serve("application/json; charset=utf-8",
                        json.dumps(self._status(), indent=2))
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _serve(self, ctype, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _status(self):
        try:
            db = open_readonly(self.server.db_path)
        except sqlite3.Error as exc:
            return {"error": "cannot open database", "detail": str(exc)}
        try:
            return build_status(db, self.server.secrets_path,
                                self.server.tariffs_path)
        finally:
            db.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db_path, secrets_path, tariffs_path):
        self.db_path = db_path
        self.secrets_path = secrets_path
        self.tariffs_path = tariffs_path
        super().__init__(addr, _Handler)


# ============================================================
# CLI
# ============================================================

def serve(host, port, db_path, secrets_path, tariffs_path,
          runtime_config_path, open_browser):
    config.load_runtime_config(runtime_config_path)
    server = _Server((host, port), db_path, secrets_path, tariffs_path)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"DTSU666 web status : {url}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="On-demand read-only web status page for the DTSU666 logger",
    )
    parser.add_argument("--db", default=DATABASE_FILE,
                        help=f"SQLite database path (default: {DATABASE_FILE})")
    parser.add_argument("--config", "--secrets", default=SECRETS_FILE,
                        dest="secrets",
                        help=f"secrets.ini path, used only for HEP reference "
                             f"detection (default: {SECRETS_FILE})")
    parser.add_argument("--tariffs", default=TARIFFS_FILE,
                        help=f"tariffs.ini path (default: {TARIFFS_FILE})")
    parser.add_argument("--runtime-config", default=RUNTIME_CONF_FILE,
                        help=f"dtsu666.conf path (default: {RUNTIME_CONF_FILE})")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="bind port (default: 8080)")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="open the page in a browser on startup")
    args = parser.parse_args(argv)

    serve(args.host, args.port, args.db, args.secrets, args.tariffs,
          args.runtime_config, args.open_browser)


if __name__ == "__main__":
    main()
