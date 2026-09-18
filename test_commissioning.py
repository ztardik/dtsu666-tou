"""Tests for the commissioning / reference-activation state machine.

Independent correctness tests: each asserts the explicit five-state
operational status and the rule that corrected counters are exactly zero
in every state except ``active``.
"""

import copy
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dtsu666_tou import commissioning, database, mqtt, periods, publishing

LOCAL_TZ = ZoneInfo("Europe/Zagreb")


def _db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="comm_")
    os.close(fd)
    db = database.open_database(path)
    mid = database.create_initial_meter(db, 1, 1000.0)
    meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
    return db, meter, mid, path


def _seed_readings(db, meter, start, n_minutes):
    """Insert n_minutes minute-aligned readings from *start* via record_energy."""
    abs_ = meter["initial_imp_ep"] or 1000.0
    for i in range(n_minutes):
        t = start + timedelta(minutes=i)
        if i > 0:
            abs_ += 0.01
        database.record_energy(db, meter, t, abs_)


def _insert_anchor(db, mid, ts_iso, vt=47000.0, nt=51900.0):
    db.execute(
        "INSERT INTO reference_readings (meter_id, timestamp, vt_kwh, nt_kwh, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (mid, ts_iso, vt, nt, ts_iso),
    )
    db.commit()


def _write_config(path, hep_entry=None):
    with open(path, "w") as f:
        f.write("[SERIAL]\nport=/dev\nbaudrate=9600\ntimeout=1.0\naddress=1\n")
        f.write("[MQTT]\nhost=local\nport=1883\n")
        if hep_entry:
            f.write(f"[HEP Correction]\n1 = {hep_entry}\n")


def _config_path():
    fd, path = tempfile.mkstemp(suffix=".ini", prefix="comm_")
    os.close(fd)
    return path


# ------------------------------------------------------------------------
# Operational-state scenarios
# ------------------------------------------------------------------------

class TestOperationalState:

    def test_startup_no_reference_is_ready(self):
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, None)  # no reference configured
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 7)
            now = start + timedelta(minutes=6, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "ready"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_config_present_but_no_anchor_yet_is_missing(self):
        """Startup race: configured reference but anchor row not yet inserted
        must be ``missing`` (not ``ready``)."""
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, "2026-08-13 10:00, 47000, 51900")
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 7)
            now = start + timedelta(minutes=6, seconds=30)
            # no reference_readings row inserted yet
            assert commissioning.operational_state(db, mid, now, cfg) == "missing"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_preacquisition_reference_is_missing(self):
        """Reference photo predates acquisition -> reference never usable."""
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, "2026-08-13 10:00, 47000, 51900")
            _insert_anchor(db, mid, "2026-08-13T10:00:00+02:00")
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 7)
            now = start + timedelta(minutes=6, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "missing"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_acquisition_becomes_ready(self):
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, None)
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 2)   # too little continuous history
            now = start + timedelta(minutes=1, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "acquiring"
            _seed_readings(db, meter, start, 7)   # >= lookback of continuous coverage
            now = start + timedelta(minutes=6, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "ready"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_reference_becomes_active(self):
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 41)
            anchor = start + timedelta(minutes=30)
            _write_config(cfg, anchor.strftime("%Y-%m-%d %H:%M") + ", 47000, 51900")
            _insert_anchor(db, mid, anchor.isoformat())
            now = start + timedelta(minutes=40, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "active"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_invalid_config_is_ready(self):
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, "bad-date, 1, 2")   # malformed -> parsed out
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 7)
            now = start + timedelta(minutes=6, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "ready"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_disconnected_when_stale(self):
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, None)
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 7)
            stale = start + timedelta(minutes=6, seconds=30) + timedelta(seconds=300)
            assert commissioning.operational_state(db, mid, stale, cfg) == "disconnected"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_reconnect_recovers(self):
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, None)
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 7)
            stale = start + timedelta(minutes=6, seconds=30) + timedelta(seconds=300)
            assert commissioning.operational_state(db, mid, stale, cfg) == "disconnected"
            _seed_readings(db, meter, stale, 7)   # readings resume
            now = stale + timedelta(minutes=6, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "ready"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_restart_with_history_stays_active(self):
        """No process state: recomputing from stored data keeps active."""
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 41)
            anchor = start + timedelta(minutes=30)
            _write_config(cfg, anchor.strftime("%Y-%m-%d %H:%M") + ", 47000, 51900")
            _insert_anchor(db, mid, anchor.isoformat())
            now = start + timedelta(minutes=40, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "active"
            # 'restart' = a fresh computation with the same data (no reset)
            assert commissioning.operational_state(db, mid, now, cfg) == "active"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_historical_gap_still_active(self):
        """A gap older than the lookback (covered by a stored estimated row)
        must not deactivate an established reference."""
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 11)          # 14:00..14:10
            # gap 14:10 -> 14:40, then 14:40..15:00 continuous
            _seed_readings(db, meter, start + timedelta(minutes=40), 21)
            anchor = start + timedelta(minutes=5)
            _write_config(cfg, anchor.strftime("%Y-%m-%d %H:%M") + ", 47000, 51900")
            _insert_anchor(db, mid, anchor.isoformat())
            now = start + timedelta(minutes=60, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "active"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_transient_gap_churn(self):
        """A >120s tail gap temporarily leaves active; documented behavior."""
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 41)
            anchor = start + timedelta(minutes=30)
            _write_config(cfg, anchor.strftime("%Y-%m-%d %H:%M") + ", 47000, 51900")
            _insert_anchor(db, mid, anchor.isoformat())
            now = start + timedelta(minutes=40, seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "active"
            # a tail gap > 120s makes the reference interval incomplete
            gapped = start + timedelta(minutes=44)
            assert commissioning.operational_state(db, mid, gapped, cfg) != "active"
            # recover with fresh readings
            _seed_readings(db, meter, gapped, 7)
            now2 = gapped + timedelta(minutes=6, seconds=30)
            assert commissioning.operational_state(db, mid, now2, cfg) == "active"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)


# ------------------------------------------------------------------------
# Corrected-counter gating
# ------------------------------------------------------------------------

class TestCorrectedGating:

    def test_all_non_active_states_zero_corrected(self):
        """THE key invariant: every state != active => corrected == 0.0."""
        db, meter, mid, path = _db()
        try:
            start = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            _seed_readings(db, meter, start, 7)
            now = start + timedelta(minutes=6, seconds=30)
            base = periods.build_periods(db, mid, now)
            for name in ("current_day", "current_week", "current_month"):
                base[name]["vt_corrected"] = 123.0
                base[name]["nt_corrected"] = 456.0
            for state in ("disconnected", "acquiring", "ready", "missing"):
                gated = commissioning.apply_corrected_gate(state, copy.deepcopy(base))
                for name in ("current_day", "current_week", "current_month"):
                    assert gated[name]["vt_corrected"] == 0.0
                    assert gated[name]["nt_corrected"] == 0.0
            active = commissioning.apply_corrected_gate("active", copy.deepcopy(base))
            assert active["current_day"]["vt_corrected"] == 123.0
        finally:
            db.close(); os.unlink(path)

    def test_publish_energy_gates_corrected(self, monkeypatch):
        """integration: a non-active state publishes corrected == 0."""
        db, meter, _mid, path = _db()
        try:
            captures = []
            monkeypatch.setattr(mqtt, "mqtt_publish",
                                lambda t, p, retain=False: captures.append((t, p)))
            accounting = {"tariff": "VT", "delta_kwh": 0.01}
            publishing.publish_energy(meter, 1000.06, accounting, db, state="missing")
            payload = captures[0][1]
            assert payload["status"] == "missing"
            assert payload["periods"]["current_day"]["vt_corrected"] == 0.0
            assert payload["periods"]["current_day"]["nt_corrected"] == 0.0
        finally:
            db.close(); os.unlink(path)

    def test_publish_energy_active_nonzero(self, monkeypatch):
        """integration: the active state publishes the computed corrected value."""
        db, meter, mid, path = _db()
        try:
            ts = (datetime.now(LOCAL_TZ) - timedelta(hours=1)).replace(second=0, microsecond=0)
            _insert_anchor(db, mid, ts.isoformat())
            captures = []
            monkeypatch.setattr(mqtt, "mqtt_publish",
                                lambda t, p, retain=False: captures.append((t, p)))
            accounting = {"tariff": "VT", "delta_kwh": 0.0}
            publishing.publish_energy(meter, 1000.0, accounting, db, state="active")
            payload = captures[0][1]
            assert payload["status"] == "active"
            assert payload["periods"]["current_day"]["vt_corrected"] > 0
        finally:
            db.close(); os.unlink(path)

    def test_publish_system_has_status(self, monkeypatch):
        db, meter, _mid, path = _db()
        try:
            captures = []
            monkeypatch.setattr(mqtt, "mqtt_publish",
                                lambda t, p, retain=False: captures.append((t, p)))
            publishing.publish_system(meter, db, state="acquiring")
            payload = captures[0][1]
            assert payload["status"] == "acquiring"
        finally:
            db.close(); os.unlink(path)


class TestShutdownAndRestart:
    def test_restart_with_history_skips_acquiring(self):
        """Requirement #10: a restart with stored history must not re-enter
        the acquiring state."""
        db, meter, mid, path = _db()
        cfg = _config_path()
        try:
            _write_config(cfg, None)
            old = datetime(2026, 8, 13, 10, 0, tzinfo=LOCAL_TZ)
            fresh = datetime(2026, 8, 13, 14, 0, tzinfo=LOCAL_TZ)
            database.record_energy(db, meter, old, 1000.0)
            database.record_energy(db, meter, fresh, 1000.01)
            now = fresh + timedelta(seconds=30)
            assert commissioning.operational_state(db, mid, now, cfg) == "ready"
        finally:
            db.close(); os.unlink(path); os.unlink(cfg)

    def test_shutdown_publishes_disconnected(self, monkeypatch):
        """Process shutdown publishes status=disconnected with zeroed
        corrected counters (energy + system topics)."""
        db, meter, mid, path = _db()
        try:
            now = datetime.now(LOCAL_TZ).replace(second=0, microsecond=0)
            start = now - timedelta(minutes=40)
            _seed_readings(db, meter, start, 41)
            anchor = start + timedelta(minutes=30)
            _insert_anchor(db, mid, anchor.isoformat())
            captures = []
            monkeypatch.setattr(mqtt, "mqtt_publish",
                                lambda t, p, retain=False: captures.append((t, p)))
            publishing.notify_mqtt_disconnected(meter, db)
            energy = next(p for t, p in captures if t.endswith("/energy"))
            system = next(p for t, p in captures if t.endswith("/system"))
            assert energy["status"] == "disconnected"
            assert energy["periods"]["current_day"]["vt_corrected"] == 0.0
            assert energy["periods"]["current_day"]["nt_corrected"] == 0.0
            assert system["status"] == "disconnected"
        finally:
            db.close(); os.unlink(path)

    def test_initial_read_uses_fresh_state(self, monkeypatch):
        """initial_read computes the state AFTER recording the fresh reading,
        so the first publish is not 'disconnected' (stale pre-restart state)."""
        from dtsu666_tou import lifecycle
        from dtsu666_tou import modbus as mb
        db, meter, _mid, path = _db()
        try:
            monkeypatch.setattr(commissioning, "reference_configured",
                                lambda config_path=None: False)
            old = datetime.now(LOCAL_TZ) - timedelta(hours=1)
            database.record_energy(db, meter, old, 1000.0)
            regs = {}
            hi, lo = mb._pack32(1000.05)
            regs[0x101E] = hi
            regs[0x101F] = lo
            for addr, _n, _d, _u, mult, _dec in mb.ELECTRICAL:
                h, l = mb._pack32(230.0 / mult)
                regs[addr] = h
                regs[addr + 1] = l
            fm = mb.FakeModbus(regs)
            captures = []
            monkeypatch.setattr(mqtt, "mqtt_publish",
                                lambda t, p, retain=False: captures.append((t, p)))
            lifecycle.initial_read(db, fm, meter)
            energy = next(p for t, p in captures if t.endswith("/energy"))
            assert energy["status"] == "ready"
        finally:
            db.close(); os.unlink(path)
