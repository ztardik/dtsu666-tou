"""Tests for the dtsu666_tou package.

Covers the pure functions (CRC, float decoding, tariff classification,
interval allocation, cost math, timestamp parsing), the database-driven
energy bookkeeping and period/correction summaries, and the Home Assistant
discovery payloads.

Test classification:
  * ``TestOracles`` and ``TestPartialSum`` assert explicit, independently
    computed expected values and are correctness tests.
  * the remaining tests pin the verified outputs so that an accidental
    behaviour change is noticed.
"""

import os
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import dtsu666_tou
from dtsu666_tou import config, time_utils, modbus, tariff, database, periods, hep, pricing

LOCAL_TZ = ZoneInfo("Europe/Zagreb")


def _tmpdb(prefix="tou_"):
    fd, path = tempfile.mkstemp(suffix=".db", prefix=prefix)
    os.close(fd)
    return database.open_database(path), path


# ====================================================================
# Constants & version
# ====================================================================

class TestConstants:
    def test_version(self):
        assert dtsu666_tou.__version__ == "0.9.0"

    def test_constants(self):
        assert config.ELECTRICAL_INTERVAL == 1
        assert config.ENERGY_INTERVAL == 60
        assert config.RETENTION_DAYS == 365
        assert config.DAILY_RETENTION_DAYS == 3650
        assert config.ESTIMATED_THRESHOLD == 120.0
        assert config.ALLOCATION_BASELINE == "baseline"
        assert config.ALLOCATION_MEASURED == "measured"
        assert config.ALLOCATION_ESTIMATED == "estimated"
        assert config.ALLOCATION_RESET == "reset"
        assert config.HA_STATUS_TOPIC == "homeassistant/status"
        assert str(config.LOCAL_TZ) == "Europe/Zagreb"

    def test_register_tables(self):
        assert [r[1] for r in modbus.ELECTRICAL] == [
            "Uab", "Ubc", "Uca", "Ua", "Ub", "Uc", "Ia", "Ib", "Ic",
            "Pt", "Pa", "Pb", "Pc", "Qt", "Qa", "Qb", "Qc"]
        assert modbus.ELECTRICAL[0] == (
            0x2000, "Uab", "Line voltage A-B", "V", 0.1, 1)
        assert modbus.ELECTRICAL[6] == (
            0x200c, "Ia", "Phase A current", "A", 0.001, 3)
        assert modbus.POWER_FACTOR[0] == (
            0x202a, "PFt", "Total power factor", "", 0.001, 3)
        assert modbus.FREQUENCY == [(0x2044, "Freq", "Frequency", "Hz", 0.01, 2)]
        assert modbus.ENERGY[0] == (
            0x101e, "ImpEp", "Total forward active energy", "kWh", 1.0, 3)
        assert modbus.BASIC_REGISTERS[46] == "Addr Communication address"


# ====================================================================
# Pure functions
# ====================================================================

class TestCRCAndFloat:
    def test_crc16_known_values(self):
        """Modbus CRC-16, pinned against independently computed values."""
        assert modbus.crc16(b"") == 0xffff
        assert modbus.crc16(b"\x01\x03") == 0x2140
        assert modbus.crc16(bytes(range(256))) == 0xde6c
        assert modbus.crc16(b"\x01\x10\x00\x01\x00\x0a") == 0xce11
        assert modbus.crc16(b"\x01\x03\x00\x00\x00\x0a") == 0xcdc5

    def test_float32_known_values(self):
        """IEEE-754 float32, big-endian word order."""
        assert modbus.float32([0x4248, 0x0000], 0) == 50.0
        assert modbus.float32([0x42c8, 0x0000], 0) == 100.0
        assert modbus.float32([0xbfc0, 0x0000], 0) == -1.5
        assert modbus.float32([0x0000, 0x0000], 0) == 0.0
        assert modbus.float32([0x4974, 0x2400], 0) == 1e6
        assert abs(modbus.float32([0x4248, 0xcccd], 0) - 50.2) < 1e-6
        # the second argument is a register offset, not a word-pair index
        assert modbus.float32([0x4248, 0x0000, 0x42c8, 0x0000], 2) == 100.0

    def test_decode_block_known_values(self):
        regs = [0x4248, 0x0000, 0x42c8, 0x0000] + [0x4248, 0x0000] * 15
        # pad to 34 for ELECTRICAL block
        regs = regs + [0x0000] * (34 - len(regs))
        block = modbus.decode_block(regs, 0x2000, modbus.ELECTRICAL)
        assert len(block) == len(modbus.ELECTRICAL)
        assert block["Uab"] == {
            "address": "2000", "description": "Line voltage A-B",
            "raw": 50.0, "value": 5.0, "unit": "V", "decimals": 1}
        assert (block["Ubc"]["raw"], block["Ubc"]["value"]) == (100.0, 10.0)
        # scaling per register: volts x0.1, amps x0.001
        assert (block["Ua"]["raw"], block["Ua"]["value"]) == (50.0, 5.0)
        assert (block["Ia"]["raw"], block["Ia"]["value"]) == (50.0, 0.05)
        assert (block["Pt"]["raw"], block["Pt"]["value"]) == (50.0, 5.0)


class TestTariff:
    def test_current_tariff_known_values(self):
        cases = [
            (datetime(2026, 1, 15, 6, 0, tzinfo=LOCAL_TZ), "NT"),   # winter NT
            (datetime(2026, 1, 15, 12, 0, tzinfo=LOCAL_TZ), "VT"),  # winter VT
            (datetime(2026, 1, 15, 7, 0, tzinfo=LOCAL_TZ), "VT"),   # winter VT edge
            (datetime(2026, 7, 15, 6, 0, tzinfo=LOCAL_TZ), "NT"),   # summer NT
            (datetime(2026, 7, 15, 12, 0, tzinfo=LOCAL_TZ), "VT"),  # summer VT
            (datetime(2026, 7, 15, 8, 0, tzinfo=LOCAL_TZ), "VT"),   # summer VT edge
            (datetime(2026, 7, 15, 23, 0, tzinfo=LOCAL_TZ), "NT"),  # summer NT
            (datetime(2026, 3, 29, 1, 59, tzinfo=LOCAL_TZ), "NT"),  # spring-forward edge
            (datetime(2026, 10, 25, 2, 30, tzinfo=LOCAL_TZ), "NT"), # fall-back ambiguous
        ]
        for dt, expected in cases:
            assert tariff.current_tariff(dt) == expected, dt

    def test_allocate_interval_known_values(self):
        cases = [
            (0.0, datetime(2026,1,1,0,0,tzinfo=LOCAL_TZ), datetime(2026,1,1,0,1,tzinfo=LOCAL_TZ), (0.0, 0.0)),
            (1.5, datetime(2026,3,29,1,59,tzinfo=LOCAL_TZ), datetime(2026,3,29,3,1,tzinfo=LOCAL_TZ), (0.0, 1.5)),  # spring-fwd
            (1.5, datetime(2026,10,25,1,59,tzinfo=LOCAL_TZ), datetime(2026,10,25,3,1,tzinfo=LOCAL_TZ), (0.0, 1.5)), # fall-back
            (3.0, datetime(2026,6,1,6,30,tzinfo=LOCAL_TZ), datetime(2026,6,1,21,30,tzinfo=LOCAL_TZ), (2.7, 0.3)),   # crosses VT/NT
            (10.0, datetime(2026,1,1,23,0,tzinfo=LOCAL_TZ), datetime(2026,1,2,7,0,tzinfo=LOCAL_TZ), (0.0, 10.0)),   # crosses midnight
            (-1.0, datetime(2026,1,1,0,0,tzinfo=LOCAL_TZ), datetime(2026,1,1,1,0,tzinfo=LOCAL_TZ), (0.0, 0.0)),     # negative
        ]
        for delta, t0, t1, expected in cases:
            vt, nt = tariff.allocate_interval(delta, t0, t1)
            assert abs(vt - expected[0]) < 1e-9, (t0, vt, nt)
            assert abs(nt - expected[1]) < 1e-9, (t0, vt, nt)
            # invariant: vt+nt == delta (for delta>0)
            if delta > 0:
                assert abs((vt + nt) - delta) < 1e-6

    def test_current_tariff_is_construction_independent(self):
        """Classification must not depend on how the datetime object was
        built: ZoneInfo vs fixed-offset (fromisoformat) vs naive."""
        cases = [
            ("2026-08-12T06:59:00+02:00", "NT"),   # summer
            ("2026-08-12T07:00:00+02:00", "NT"),
            ("2026-08-12T07:59:00+02:00", "NT"),
            ("2026-08-12T08:00:00+02:00", "VT"),
            ("2026-08-12T21:59:00+02:00", "VT"),
            ("2026-08-12T22:00:00+02:00", "NT"),
            ("2026-01-15T06:59:00+01:00", "NT"),   # winter
            ("2026-01-15T07:00:00+01:00", "VT"),
            ("2026-01-15T20:59:00+01:00", "VT"),
            ("2026-01-15T21:00:00+01:00", "NT"),
            ("2026-03-29T03:30:00+02:00", "NT"),   # spring-forward day
            ("2026-10-25T02:30:00+02:00", "NT"),   # fall-back day (CEST)
            ("2026-10-25T02:30:00+01:00", "NT"),   # fall-back day (CET)
        ]
        for iso, expected in cases:
            from_iso = datetime.fromisoformat(iso)          # fixed-offset tz
            as_zone = from_iso.astimezone(LOCAL_TZ)          # real ZoneInfo
            naive = from_iso.replace(tzinfo=None)            # naive wall-clock
            got = [tariff.current_tariff(from_iso),
                   tariff.current_tariff(as_zone),
                   tariff.current_tariff(naive)]
            assert got == [expected, expected, expected], (iso, got)


class TestPricing:
    def _tariff(self):
        return {
            "energija_vt": 0.097189, "energija_nt": 0.047688,
            "prijenos_vt": 0.021256, "prijenos_nt": 0.008175,
            "distribucija_vt": 0.044446, "distribucija_nt": 0.020514,
            "oie_kwh": 0.013239,
            "opskrba_month": 0.982, "mjerno_mjesto_month": 1.983,
            "vat_percent": 13,
        }

    def test_calculate_cost_known_values(self):
        """All components are VAT-inclusive and rounded to 4 decimals."""
        t = self._tariff()
        expected = {
            (0, 0, 0, False): 0.0,
            (10, 5, 15, False): 2.4966,
            (10, 5, 15, True): 5.847,
            (100, 50, 150, True): 28.3164,
            (1, 0, 1, False): 0.199,
        }
        for (vt, nt, tot, inc), total in expected.items():
            r = pricing.calculate_cost(vt, nt, tot, t, inc)
            assert abs(r["total"] - total) < 1e-6, (vt, nt, tot, inc, r)
            # the prices already contain VAT, so the subtotal is the total
            assert r["total"] == r["subtotal"], r

    def test_full_vat_math_known_value(self):
        t = self._tariff()
        day = pricing.calculate_cost(10, 5, 15, t, False)
        assert abs(day["total"] - 2.4966) < 1e-6
        assert abs(day["energija"] - 1.3677) < 1e-6     # energy component
        assert abs(day["prijenos"] - 0.2864) < 1e-6     # network component
        assert abs(day["distribucija"] - 0.6181) < 1e-6
        assert abs(day["oie"] - 0.2244) < 1e-6
        assert abs(day["vat"] - 0.2872) < 1e-6          # informational VAT share
        assert day["opskrba"] == 0.0                    # monthly fees not requested
        assert day["mjerno_mjesto"] == 0.0

    def test_load_tariffs_versioned_sections(self):
        txt = (
            "[COST]\nenergija_vt = 0.1\nenergija_nt = 0.05\nvat_percent = 13\n"
            "[COST 2026-06-01]\nenergija_vt = 0.15\n"
        )
        fd, p = tempfile.mkstemp(suffix=".ini"); os.close(fd)
        with open(p, "w") as f: f.write(txt)
        try:
            t = pricing.load_tariffs(p)
        finally:
            os.unlink(p)
        assert [x["valid_from"] for x in t] == [date(2000, 1, 1), date(2026, 6, 1)]
        assert t[0]["energija_vt"] == 0.1
        assert t[0]["energija_nt"] == 0.05
        assert t[0]["vat_percent"] == 13.0
        assert t[0]["prijenos_vt"] is None
        # the dated section overrides only the keys it defines
        assert t[1]["energija_vt"] == 0.15
        assert t[1]["energija_nt"] == 0.05
        assert t[1]["vat_percent"] == 13.0
        assert pricing.tariff_for_date(t, date(2026, 5, 1))["energija_vt"] == 0.1
        assert pricing.tariff_for_date(t, date(2026, 7, 1))["energija_vt"] == 0.15


class TestTimestamps:
    def test_parse_correction_datetime(self):
        expected = {
            "2026-04-01 16:00": datetime(2026, 4, 1, 16, 0, tzinfo=LOCAL_TZ),
            "2026-01-15 10:00": datetime(2026, 1, 15, 10, 0, tzinfo=LOCAL_TZ),
            "2026-08-11T19:56:00": datetime(2026, 8, 11, 19, 56, tzinfo=LOCAL_TZ),
            "2026-08-11 19:56:00": datetime(2026, 8, 11, 19, 56, tzinfo=LOCAL_TZ),
        }
        for s, want in expected.items():
            got = time_utils._parse_correction_datetime(s)
            assert got == want, s
            # a naive input is interpreted as local wall-clock time
            assert str(got.tzinfo) == "Europe/Zagreb", s

    def test_parse_timestamp_round_trip(self):
        for ts in [datetime(2026, 4, 1, 12, 0, tzinfo=LOCAL_TZ),
                   datetime(2026, 8, 11, 19, 56, tzinfo=LOCAL_TZ)]:
            got = time_utils._parse_timestamp(ts.isoformat())
            assert got == ts, ts
            assert str(got.tzinfo) == "Europe/Zagreb", ts


# ====================================================================
# Database-driven data handling
# ====================================================================

class TestRecordEnergy:
    """``database.record_energy``: delta allocation, the daily aggregate and
    the baseline / measured / estimated decision."""

    def _db(self):
        db, path = _tmpdb("recenergy_")
        mid = database.create_initial_meter(db, 1, 50000.0)
        meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
        return db, meter, path

    def _last_reading(self, db):
        return db.execute(
            "SELECT absolute_kwh, delta_kwh, tariff, vt_kwh, nt_kwh, "
            "allocation_method FROM readings ORDER BY id DESC LIMIT 1").fetchone()

    def test_baseline_then_measured(self):
        db, meter, path = self._db()
        try:
            ts = datetime(2026, 7, 15, 12, 0, tzinfo=LOCAL_TZ)
            a0 = database.record_energy(db, meter, ts, 50000.0)
            assert a0 == {"delta_kwh": 0.0, "tariff": "VT", "vt_kwh": 0.0,
                          "nt_kwh": 0.0, "method": "baseline"}
            assert self._last_reading(db)["allocation_method"] == "baseline"

            a1 = database.record_energy(
                db, meter, ts + timedelta(seconds=60), 50000.034)
            assert a1["method"] == "measured"
            assert a1["tariff"] == "VT"                  # 12:00 is VT
            assert abs(a1["delta_kwh"] - 0.034) < 1e-6
            assert abs(a1["vt_kwh"] - 0.034) < 1e-6
            assert a1["nt_kwh"] == 0.0

            row = self._last_reading(db)
            assert row["absolute_kwh"] == 50000.034
            assert abs(row["delta_kwh"] - 0.034) < 1e-6
            assert row["tariff"] == "VT"
            assert abs(row["vt_kwh"] - 0.034) < 1e-6
            assert row["nt_kwh"] == 0.0
            assert row["allocation_method"] == "measured"
        finally:
            db.close(); os.unlink(path)

    def test_gap_is_estimated_and_negative_delta_is_clamped(self):
        db, meter, path = self._db()
        try:
            ts = datetime(2026, 7, 15, 12, 0, tzinfo=LOCAL_TZ)
            database.record_energy(db, meter, ts, 50000.0)

            gap = ts + timedelta(hours=1)                # 1 h gap -> estimated
            a = database.record_energy(db, meter, gap, 50001.5)
            assert a["method"] == "estimated"
            assert abs(a["delta_kwh"] - 1.5) < 1e-9
            assert abs(a["vt_kwh"] - 1.5) < 1e-9         # 12:00-13:00 is VT
            assert a["nt_kwh"] == 0.0

            a2 = database.record_energy(
                db, meter, gap + timedelta(seconds=60), 50001.0)
            assert a2["delta_kwh"] == 0.0                # decrease clamped to 0
            assert a2["vt_kwh"] == 0.0
            assert a2["nt_kwh"] == 0.0
            assert a2["method"] == "reset"
            # daily "latest counter" must not regress after a decrease
            day_row = db.execute(
                "SELECT absolute_kwh FROM energy_daily WHERE date=?",
                (gap.date().isoformat(),)).fetchone()
            assert day_row["absolute_kwh"] == 50001.5
        finally:
            db.close(); os.unlink(path)

    def test_daily_aggregate(self):
        db, meter, path = self._db()
        try:
            ts = datetime(2026, 7, 15, 12, 0, tzinfo=LOCAL_TZ)
            database.record_energy(db, meter, ts, 50000.0)
            database.record_energy(db, meter, ts + timedelta(seconds=60), 50000.05)
            row = db.execute(
                "SELECT absolute_kwh, total_kwh, vt_kwh, nt_kwh FROM energy_daily "
                "WHERE date=?", (ts.date().isoformat(),)).fetchone()
            assert row["absolute_kwh"] == 50000.05
            assert abs(row["total_kwh"] - 0.05) < 1e-6
            assert abs(row["vt_kwh"] - 0.05) < 1e-6
            assert row["nt_kwh"] == 0.0
        finally:
            db.close(); os.unlink(path)

    def test_08_00_boundary_delta_goes_to_nt(self):
        """Regression: a delta consumed in the last minute before 08:00
        (summer NT) must be booked to NT, not VT.

        Reproduces the fixed-offset DST bug where the previous timestamp,
        parsed from the DB, lost its Europe/Zagreb DST rules and made
        ``same_tariff`` compare 07:59 (winter->VT) against 08:00
        (summer->VT), wrongly taking the ``measured`` branch.
        """
        db, meter, path = self._db()
        try:
            t = datetime(2026, 8, 12, 7, 58, 0, tzinfo=LOCAL_TZ)
            database.record_energy(db, meter, t, 50000.0)      # baseline
            t = t + timedelta(minutes=1)
            database.record_energy(db, meter, t, 50000.0)      # 07:59 (NT)
            boundary = t + timedelta(minutes=1)                # 08:00:00
            acc = database.record_energy(db, meter, boundary, 50000.05)
            assert acc["method"] == "estimated", acc["method"]
            assert acc["vt_kwh"] == 0.0, acc["vt_kwh"]
            assert abs(acc["nt_kwh"] - 0.05) < 1e-9, acc["nt_kwh"]
            assert acc["tariff"] == "VT", acc["tariff"]
        finally:
            db.close(); os.unlink(path)


class TestPeriodsAndCorrections:
    """``periods`` / ``hep`` summaries over a small, fully explicit fixture."""

    READINGS = [
        # (timestamp, absolute, delta, tariff, vt, nt, method)
        ("2026-04-01T19:00:00+02:00", 50000.0, 0.0, "VT", 0.0, 0.0, "baseline"),
        ("2026-04-01T20:00:00+02:00", 50001.0, 1.0, "VT", 1.0, 0.0, "measured"),
        ("2026-04-01T21:00:00+02:00", 50001.0, 0.0, "VT", 0.0, 0.0, "measured"),
        ("2026-04-01T22:00:00+02:00", 50002.0, 1.0, "NT", 0.0, 1.0, "measured"),
        ("2026-04-01T23:00:00+02:00", 50003.0, 1.0, "NT", 0.0, 1.0, "measured"),
        ("2026-04-02T00:00:00+02:00", 50004.0, 1.0, "NT", 0.0, 1.0, "measured"),
        ("2026-04-02T01:00:00+02:00", 50005.0, 1.0, "NT", 0.0, 1.0, "measured"),
        # 5 h scheduler gap -> the estimated row preserves the gap energy
        ("2026-04-02T06:00:00+02:00", 50010.0, 5.0, "NT", 0.0, 5.0, "estimated"),
        ("2026-04-02T08:00:00+02:00", 50012.0, 2.0, "VT", 2.0, 0.0, "measured"),
        ("2026-04-02T10:00:00+02:00", 50014.0, 2.0, "VT", 2.0, 0.0, "measured"),
        ("2026-04-02T12:00:00+02:00", 50016.0, 2.0, "VT", 2.0, 0.0, "measured"),
    ]

    # Apr 1 is complete; Apr 2 has readings but no daily aggregate yet, so
    # the periods that overlap Apr 1 report its 3.0 kWh (1.0 VT + 2.0 NT).
    EXPECTED_PERIODS = {
        "current_day": {"absolute_kwh": 50016.0, "total_kwh": 0, "vt_kwh": 0, "nt_kwh": 0},
        "previous_day": {"absolute_kwh": 50003.0, "total_kwh": 3.0, "vt_kwh": 1.0, "nt_kwh": 2.0},
        "current_week": {"absolute_kwh": 50016.0, "total_kwh": 3.0, "vt_kwh": 1.0, "nt_kwh": 2.0},
        "previous_week": {"absolute_kwh": None, "total_kwh": 0, "vt_kwh": 0, "nt_kwh": 0},
        "current_month": {"absolute_kwh": 50016.0, "total_kwh": 3.0, "vt_kwh": 1.0, "nt_kwh": 2.0},
        "previous_month": {"absolute_kwh": None, "total_kwh": 0, "vt_kwh": 0, "nt_kwh": 0},
    }

    def _fixture(self):
        db, path = _tmpdb("periods_")
        mid = database.create_initial_meter(db, 1, 50000.0)
        db.execute(
            "INSERT OR REPLACE INTO energy_daily "
            "(meter_id, date, absolute_kwh, total_kwh, vt_kwh, nt_kwh) "
            "VALUES (?, ?, 0, 3.0, 1.0, 2.0)", (mid, "2026-04-01"))
        for ts, abs_, delta, tarr, vt, nt, method in self.READINGS:
            db.execute(
                "INSERT INTO readings (meter_id, timestamp, absolute_kwh, "
                "delta_kwh, tariff, vt_kwh, nt_kwh, allocation_method) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, ts, abs_, delta, tarr, vt, nt, method))
        db.commit()
        return db, mid, path

    def test_fixture_reconciles_with_daily_aggregates(self):
        db, mid, path = self._fixture()
        try:
            for row in db.execute(
                "SELECT date, total_kwh, vt_kwh, nt_kwh FROM energy_daily "
                "WHERE meter_id = ?", (mid,)).fetchall():
                agg = db.execute(
                    "SELECT COALESCE(SUM(delta_kwh),0) AS d, "
                    "COALESCE(SUM(vt_kwh),0) AS v, COALESCE(SUM(nt_kwh),0) AS n "
                    "FROM readings WHERE meter_id = ? AND substr(timestamp,1,10) = ?",
                    (mid, row["date"])).fetchone()
                assert abs(agg["d"] - row["total_kwh"]) < 1e-9, ("total", row["date"])
                assert abs(agg["v"] - row["vt_kwh"]) < 1e-9, ("vt", row["date"])
                assert abs(agg["n"] - row["nt_kwh"]) < 1e-9, ("nt", row["date"])
        finally:
            db.close(); os.unlink(path)

    def test_period_summary(self):
        db, mid, path = self._fixture()
        try:
            now = datetime(2026, 4, 2, 12, 0, tzinfo=LOCAL_TZ)
            got = {name: periods.period_summary(db, mid, *fn(now))
                   for name, fn in periods.PERIOD_RANGES.items()}
            assert got == self.EXPECTED_PERIODS
        finally:
            db.close(); os.unlink(path)

    def test_build_periods(self):
        db, mid, path = self._fixture()
        try:
            now = datetime(2026, 4, 2, 12, 0, tzinfo=LOCAL_TZ)
            assert periods.build_periods(db, mid, now) == self.EXPECTED_PERIODS
        finally:
            db.close(); os.unlink(path)

    def test_calculate_corrected_single_anchor(self):
        db, mid, path = self._fixture()
        try:
            db.execute(
                "INSERT INTO reference_readings "
                "(meter_id, timestamp, vt_kwh, nt_kwh, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (mid, "2026-04-01T19:56:00+02:00", 9000.0, 18000.0,
                 "2026-04-01T19:56:00+02:00"))
            db.commit()
            now = datetime(2026, 4, 2, 12, 0, tzinfo=LOCAL_TZ)
            got = hep.calculate_corrected(
                db, mid, now, periods.build_periods(db, mid, now))
            assert got == {
                "current_day": {"absolute_kwh": 50016.0, "total_kwh": 0,
                                "vt_kwh": 0, "nt_kwh": 0,
                                "vt_corrected": 9007.0, "nt_corrected": 18009.0},
                "previous_day": {"total_kwh": 3.0, "vt_kwh": 1.0, "nt_kwh": 2.0},
                "current_week": {"absolute_kwh": 50016.0, "total_kwh": 3.0,
                                 "vt_kwh": 1.0, "nt_kwh": 2.0,
                                 "vt_corrected": 9007.0, "nt_corrected": 18009.0},
                "previous_week": {"total_kwh": 0, "vt_kwh": 0, "nt_kwh": 0},
                "current_month": {"absolute_kwh": 50016.0, "total_kwh": 3.0,
                                  "vt_kwh": 1.0, "nt_kwh": 2.0,
                                  "vt_corrected": 9007.0, "nt_corrected": 18009.0},
                "previous_month": {"total_kwh": 0, "vt_kwh": 0, "nt_kwh": 0},
            }
            # only the period in progress exposes the cumulative counter
            assert "vt_corrected" not in got["previous_day"]
            assert "vt_corrected" in got["current_day"]
        finally:
            db.close(); os.unlink(path)


class TestPartialSum:
    """Readings-based _our_partial_sum: accuracy, gap handling, incomplete."""

    def _db_with_readings(self, rows):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="partialsum_")
        os.close(fd)
        db = database.open_database(path)
        mid = database.create_initial_meter(db, 1, rows[0][1])
        for ts, abs_, delta, tarr, vt, nt, method in rows:
            db.execute(
                "INSERT INTO readings (meter_id, timestamp, absolute_kwh, "
                "delta_kwh, tariff, vt_kwh, nt_kwh, allocation_method) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, ts, abs_, delta, tarr, vt, nt, method),
            )
        db.commit()
        return db, mid, path

    def test_continuous_full_coverage_uses_stored_allocation(self):
        rows = [
            ("2026-07-15T00:00:00+02:00", 100.00, 0.00, "NT", 0.0, 0.0, "baseline"),
            ("2026-07-15T00:01:00+02:00", 100.05, 0.05, "NT", 0.0, 0.05, "measured"),
            ("2026-07-15T00:02:00+02:00", 100.10, 0.05, "NT", 0.0, 0.05, "measured"),
            ("2026-07-15T08:00:00+02:00", 100.60, 0.50, "VT", 0.50, 0.0, "measured"),
            ("2026-07-15T08:01:00+02:00", 100.70, 0.10, "VT", 0.10, 0.0, "measured"),
        ]
        db, mid, p = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 7, 15, 0, 1, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 7, 15, 8, 1, tzinfo=LOCAL_TZ)
            vt, inc_v = hep._our_partial_sum(db, mid, t0, t1, "vt")
            nt, inc_n = hep._our_partial_sum(db, mid, t0, t1, "nt")
            assert abs(vt - 0.60) < 1e-9, vt
            assert abs(nt - 0.05) < 1e-9, nt
            assert abs((vt + nt) - 0.65) < 1e-9   # vt+nt == covered delta
            assert not inc_v and not inc_n
        finally:
            db.close(); os.unlink(p)

    def test_mid_interval_gap_does_not_drop_energy(self):
        """Mandatory: a scheduler gap (10:01->10:30) captured by the next
        estimated reading must NOT be lost by the reconstruction."""
        rows = [
            ("2026-07-15T10:00:00+02:00", 200.00, 0.00, "VT", 0.0, 0.0, "baseline"),
            ("2026-07-15T10:01:00+02:00", 200.01, 0.01, "VT", 0.01, 0.0, "measured"),
            ("2026-07-15T10:30:00+02:00", 200.50, 0.49, "VT", 0.49, 0.0, "estimated"),
            ("2026-07-15T10:31:00+02:00", 200.51, 0.01, "VT", 0.01, 0.0, "measured"),
        ]
        db, mid, p = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 7, 15, 10, 1, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 7, 15, 10, 31, tzinfo=LOCAL_TZ)
            vt, inc = hep._our_partial_sum(db, mid, t0, t1, "vt")
            nt, _ = hep._our_partial_sum(db, mid, t0, t1, "nt")
            assert abs(vt - 0.50) < 1e-9, vt      # 0.49 (gap) + 0.01
            assert nt == 0.0
            assert not inc
        finally:
            db.close(); os.unlink(p)

    def test_head_reference_mid_day_truncates_first_row(self):
        rows = [
            ("2026-07-15T19:00:00+02:00", 300.00, 0.00, "VT", 0.0, 0.0, "baseline"),
            ("2026-07-15T20:00:00+02:00", 301.00, 1.00, "VT", 1.0, 0.0, "measured"),
            ("2026-07-15T21:00:00+02:00", 302.00, 1.00, "VT", 1.0, 0.0, "measured"),
            ("2026-07-15T22:00:00+02:00", 303.00, 1.00, "NT", 0.0, 1.0, "measured"),
        ]
        db, mid, p = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 7, 15, 19, 30, tzinfo=LOCAL_TZ)  # reference mid-day
            t1 = datetime(2026, 7, 15, 22, 0, tzinfo=LOCAL_TZ)
            vt, inc_v = hep._our_partial_sum(db, mid, t0, t1, "vt")
            nt, inc_n = hep._our_partial_sum(db, mid, t0, t1, "nt")
            assert abs(vt - 2.0) < 1e-9, vt
            assert abs(nt - 1.0) < 1e-9, nt
            assert not inc_v and not inc_n
        finally:
            db.close(); os.unlink(p)

    def test_crosses_midnight(self):
        rows = [
            ("2026-07-15T23:00:00+02:00", 400.00, 0.00, "NT", 0.0, 0.0, "baseline"),
            ("2026-07-15T23:30:00+02:00", 400.50, 0.50, "NT", 0.0, 0.50, "measured"),
            ("2026-07-16T00:30:00+02:00", 401.00, 0.50, "NT", 0.0, 0.50, "measured"),
            ("2026-07-16T01:00:00+02:00", 401.50, 0.50, "NT", 0.0, 0.50, "measured"),
        ]
        db, mid, p = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 7, 15, 23, 30, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 7, 16, 1, 0, tzinfo=LOCAL_TZ)
            nt, inc = hep._our_partial_sum(db, mid, t0, t1, "nt")
            vt, _ = hep._our_partial_sum(db, mid, t0, t1, "vt")
            assert abs(nt - 1.0) < 1e-9, nt   # 00:30 + 01:00 (23:30 row is at t0)
            assert vt == 0.0
            assert not inc
        finally:
            db.close(); os.unlink(p)

    def test_small_tail_not_incomplete(self):
        rows = [
            ("2026-07-15T09:00:00+02:00", 500.00, 0.00, "VT", 0.0, 0.0, "baseline"),
            ("2026-07-15T10:00:00+02:00", 501.00, 1.00, "VT", 1.0, 0.0, "measured"),
        ]
        db, mid, p = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 7, 15, 9, 0, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 7, 15, 10, 0, 30, tzinfo=LOCAL_TZ)  # 30s tail
            vt, inc = hep._our_partial_sum(db, mid, t0, t1, "vt")
            assert abs(vt - 1.0) < 1e-9, vt
            assert not inc
        finally:
            db.close(); os.unlink(p)

    def test_no_readings_after_reference(self):
        rows = [
            ("2026-07-15T09:00:00+02:00", 600.00, 0.00, "VT", 0.0, 0.0, "baseline"),
            ("2026-07-15T09:01:00+02:00", 600.10, 0.10, "VT", 0.10, 0.0, "measured"),
        ]
        db, mid, p = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 7, 15, 10, 0, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 7, 15, 12, 0, tzinfo=LOCAL_TZ)
            kwh, inc = hep._our_partial_sum(db, mid, t0, t1, "vt")
            assert kwh == 0.0
            assert inc is True
        finally:
            db.close(); os.unlink(p)

    def test_tariff_boundary_truncation_summer(self):
        """07:30-08:00 is NT in summer; a row crossing t_from must be split
        with the correct (summer) tariff timing."""
        rows = [
            ("2026-08-12T07:00:00+02:00", 700.00, 0.00, "NT", 0.0, 0.0, "baseline"),
            ("2026-08-12T08:00:00+02:00", 700.50, 0.50, "VT", 0.50, 0.0, "measured"),
            ("2026-08-12T08:30:00+02:00", 701.00, 0.50, "VT", 0.50, 0.0, "measured"),
        ]
        db, mid, p = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 8, 12, 7, 30, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 8, 12, 8, 30, tzinfo=LOCAL_TZ)
            vt, inc_v = hep._our_partial_sum(db, mid, t0, t1, "vt")
            nt, inc_n = hep._our_partial_sum(db, mid, t0, t1, "nt")
            assert abs(nt - 0.50) < 1e-9, nt   # 08:00 row re-allocated to NT
            assert abs(vt - 0.50) < 1e-9, vt   # 08:30 row stored VT
            assert not inc_v and not inc_n
        finally:
            db.close(); os.unlink(p)


class TestOracles:
    """Independent correctness tests: each asserts explicit, manually
    computed expected values (the oracle) rather than re-using the
    production algorithm."""

    def _db(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="oracle_")
        os.close(fd)
        db = database.open_database(path)
        mid = database.create_initial_meter(db, 1, 0.0)
        meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
        return db, meter, path

    def _db_with_readings(self, rows):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="oracle_")
        os.close(fd)
        db = database.open_database(path)
        mid = database.create_initial_meter(db, 1, rows[0][1])
        for ts, abs_, delta, tarr, vt, nt, method in rows:
            db.execute(
                "INSERT INTO readings (meter_id, timestamp, absolute_kwh, "
                "delta_kwh, tariff, vt_kwh, nt_kwh, allocation_method) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, ts, abs_, delta, tarr, vt, nt, method))
        db.commit()
        return db, mid, path

    def test_current_tariff_oracle_boundary_matrix(self):
        """implementation: tariff.current_tariff
           oracle: explicit HEP schedule (summer VT 08-22, winter VT 07-21)"""
        cases = [
            ("2026-08-12T07:00:00+02:00", "NT"),
            ("2026-08-12T07:59:59+02:00", "NT"),
            ("2026-08-12T08:00:00+02:00", "VT"),
            ("2026-08-12T08:00:01+02:00", "VT"),
            ("2026-08-12T20:59:59+02:00", "VT"),
            ("2026-08-12T21:00:00+02:00", "VT"),
            ("2026-08-12T21:59:59+02:00", "VT"),
            ("2026-08-12T22:00:00+02:00", "NT"),
            ("2026-01-15T06:00:00+01:00", "NT"),
            ("2026-01-15T06:59:59+01:00", "NT"),
            ("2026-01-15T07:00:00+01:00", "VT"),
            ("2026-01-15T20:59:59+01:00", "VT"),
            ("2026-01-15T21:00:00+01:00", "NT"),
            ("2026-03-29T01:59:59+01:00", "NT"),   # spring-forward day
            ("2026-03-29T03:00:00+02:00", "NT"),
            ("2026-10-25T02:30:00+02:00", "NT"),   # fall-back day (CEST)
            ("2026-10-25T02:30:00+01:00", "NT"),   # fall-back day (CET)
        ]
        for iso, expected in cases:
            got = tariff.current_tariff(datetime.fromisoformat(iso))
            assert got == expected, (iso, got)

    def test_current_tariff_hostile_representations(self):
        """implementation: tariff.current_tariff
           oracle: explicit expected tariff per instant; the SAME instant as
           ZoneInfo / UTC / fixed +02:00 / fixed +01:00 / naive must classify
           identically and equal the oracle."""
        from datetime import timezone as _tz
        instants = [
            (datetime(2026, 8, 12, 7, 30, tzinfo=LOCAL_TZ), "NT"),
            (datetime(2026, 8, 12, 8, 30, tzinfo=LOCAL_TZ), "VT"),
            (datetime(2026, 1, 15, 7, 30, tzinfo=LOCAL_TZ), "VT"),
            (datetime(2026, 1, 15, 6, 30, tzinfo=LOCAL_TZ), "NT"),
        ]
        for z, expected in instants:
            reps = [
                z,
                z.astimezone(_tz.utc),
                z.astimezone(_tz(timedelta(hours=2))),
                z.astimezone(_tz(timedelta(hours=1))),
                z.replace(tzinfo=None),
            ]
            for r in reps:
                assert tariff.current_tariff(r) == expected, (r, tariff.current_tariff(r), expected)

    def test_allocate_interval_billing_boundary(self):
        """implementation: tariff.allocate_interval
           oracle: manual wall-clock arithmetic on the HEP windows"""
        vt, nt = tariff.allocate_interval(0.50, datetime(2026, 8, 12, 7, 59, tzinfo=LOCAL_TZ),
                                          datetime(2026, 8, 12, 8, 0, tzinfo=LOCAL_TZ))
        assert vt == 0.0 and abs(nt - 0.50) < 1e-9
        vt, nt = tariff.allocate_interval(0.50, datetime(2026, 8, 12, 8, 0, tzinfo=LOCAL_TZ),
                                          datetime(2026, 8, 12, 8, 1, tzinfo=LOCAL_TZ))
        assert abs(vt - 0.50) < 1e-9 and nt == 0.0
        vt, nt = tariff.allocate_interval(0.60, datetime(2026, 8, 12, 7, 59, 30, tzinfo=LOCAL_TZ),
                                          datetime(2026, 8, 12, 8, 0, 30, tzinfo=LOCAL_TZ))
        assert abs(vt - 0.30) < 1e-9 and abs(nt - 0.30) < 1e-9
        vt, nt = tariff.allocate_interval(0.50, datetime(2026, 1, 15, 6, 59, tzinfo=LOCAL_TZ),
                                          datetime(2026, 1, 15, 7, 0, tzinfo=LOCAL_TZ))
        assert vt == 0.0 and abs(nt - 0.50) < 1e-9
        vt, nt = tariff.allocate_interval(0.50, datetime(2026, 1, 15, 7, 0, tzinfo=LOCAL_TZ),
                                          datetime(2026, 1, 15, 7, 1, tzinfo=LOCAL_TZ))
        assert abs(vt - 0.50) < 1e-9 and nt == 0.0

    def test_allocation_method_semantics(self):
        """implementation: database.record_energy (same_tariff classification)
           oracle: semantic meaning of allocation_method"""
        db, m, path = self._db()
        try:
            database.record_energy(db, m, datetime(2026, 8, 12, 7, 0, tzinfo=LOCAL_TZ), 100.0)
            a = database.record_energy(db, m, datetime(2026, 8, 12, 7, 5, tzinfo=LOCAL_TZ), 100.0)
            assert a["method"] == "measured", a["method"]
            b = database.record_energy(db, m, datetime(2026, 8, 12, 7, 6, tzinfo=LOCAL_TZ), 100.02)
            assert b["method"] == "measured", b["method"]
            database.record_energy(db, m, datetime(2026, 8, 12, 7, 59, tzinfo=LOCAL_TZ), 100.05)
            c = database.record_energy(db, m, datetime(2026, 8, 12, 8, 0, tzinfo=LOCAL_TZ), 100.06)
            assert c["method"] == "estimated", c["method"]
            assert c["vt_kwh"] == 0.0 and abs(c["nt_kwh"] - 0.01) < 1e-9
        finally:
            db.close(); os.unlink(path)

    def test_partial_sum_contract(self):
        """implementation: hep._our_partial_sum
           oracle: documented contract (empty -> (0.0, False); reversed -> ValueError)"""
        db, m, path = self._db()
        try:
            t = datetime(2026, 8, 12, 12, 0, tzinfo=LOCAL_TZ)
            assert hep._our_partial_sum(db, m["id"], t, t, "vt") == (0.0, False)
            raised = False
            try:
                hep._our_partial_sum(db, m["id"], t + timedelta(hours=1), t, "vt")
            except ValueError:
                raised = True
            assert raised, "expected ValueError for reversed interval"
        finally:
            db.close(); os.unlink(path)

    def test_partial_sum_gap_crossing_boundary(self):
        """implementation: hep._our_partial_sum (boundary re-allocation)
           oracle: a gap row crossing t_from AND the 08:00 summer boundary is
           re-allocated 5 min NT / 10 min VT of the 0.20 kWh delta."""
        rows = [
            ("2026-08-12T07:50:00+02:00", 800.00, 0.00, "NT", 0.0, 0.0, "baseline"),
            ("2026-08-12T08:10:00+02:00", 800.20, 0.20, "VT", 0.10, 0.10, "estimated"),
        ]
        db, mid, path = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 8, 12, 7, 55, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 8, 12, 8, 10, tzinfo=LOCAL_TZ)
            vt, _ = hep._our_partial_sum(db, mid, t0, t1, "vt")
            nt, _ = hep._our_partial_sum(db, mid, t0, t1, "nt")
            assert abs(vt - 0.133333) < 1e-4, vt
            assert abs(nt - 0.066667) < 1e-4, nt
        finally:
            db.close(); os.unlink(path)

    def test_partial_sum_dst_transition_season(self):
        """implementation: hep._our_partial_sum (boundary re-allocation)
           oracle: on 2026-10-25 (DST fall-back) noon is CET -> winter, so the
           07:00-08:00 interval is VT (not NT)."""
        rows = [
            ("2026-10-25T06:00:00+01:00", 950.00, 0.00, "NT", 0.0, 0.0, "baseline"),
            ("2026-10-25T08:00:00+01:00", 950.60, 0.60, "VT", 0.60, 0.0, "estimated"),
        ]
        db, mid, path = self._db_with_readings(rows)
        try:
            t0 = datetime(2026, 10, 25, 7, 0, tzinfo=LOCAL_TZ)
            t1 = datetime(2026, 10, 25, 8, 0, tzinfo=LOCAL_TZ)
            vt, _ = hep._our_partial_sum(db, mid, t0, t1, "vt")
            nt, _ = hep._our_partial_sum(db, mid, t0, t1, "nt")
            assert abs(vt - 0.60) < 1e-9, vt
            assert nt == 0.0, nt
        finally:
            db.close(); os.unlink(path)

    def test_record_energy_threshold_boundary(self):
        """implementation: database.record_energy (ESTIMATED_THRESHOLD = 120s)
           oracle: exactly 120.0s same-tariff -> measured; 120.001s -> estimated"""
        db, m, path = self._db()
        try:
            base = datetime(2026, 8, 12, 12, 0, tzinfo=LOCAL_TZ)
            database.record_energy(db, m, base, 200.0)
            t120 = base + timedelta(seconds=120.0)
            a = database.record_energy(db, m, t120, 200.10)
            assert a["method"] == "measured", a["method"]
            t1201 = t120 + timedelta(seconds=120.001)
            b = database.record_energy(db, m, t1201, 200.20)
            assert b["method"] == "estimated", b["method"]
        finally:
            db.close(); os.unlink(path)



class TestHepParsing:
    def test_parse_hep_corrections(self):
        txt = (
            "[SERIAL]\nport=/dev\nbaudrate=9600\ntimeout=1.0\naddress=1\n"
            "[MQTT]\nhost=local\nport=1883\n"
            "[HEP Correction]\n"
            "1 = 2026-04-01 16:00, 9000, 18000, photo today\n"
            "2 = 2026-02-25 10:00, 7500, 16000, older photo\n"
        )
        fd, p = tempfile.mkstemp(suffix=".ini"); os.close(fd)
        with open(p, "w") as f: f.write(txt)
        try:
            got = hep.parse_hep_corrections(p)
        finally:
            os.unlink(p)
        # oldest reference first, regardless of the section numbering
        assert got == [
            {"at": datetime(2026, 2, 25, 10, 0, tzinfo=LOCAL_TZ),
             "vt": 7500.0, "nt": 16000.0, "reason": "older photo"},
            {"at": datetime(2026, 4, 1, 16, 0, tzinfo=LOCAL_TZ),
             "vt": 9000.0, "nt": 18000.0, "reason": "photo today"},
        ]


# ====================================================================
# Discovery parity (needs paho)
# ====================================================================

class TestDiscovery:
    def _capture(self, monkeypatch):
        from dtsu666_tou import mqtt
        calls = []
        monkeypatch.setattr(mqtt, "mqtt_publish",
                            lambda t, p, retain=False: calls.append((t, p, retain)))
        from dtsu666_tou.discovery import publish_ha_discovery
        publish_ha_discovery(1)
        return calls

    def test_entity_count_and_uniqueness(self, monkeypatch):
        calls = self._capture(monkeypatch)
        configs = [c for c in calls if isinstance(c[1], dict)]
        assert len(configs) == 64
        uids = [p["unique_id"] for _t, p, _r in configs]
        assert len(uids) == len(set(uids))

    def test_connection_entity_removed_and_purged(self, monkeypatch):
        """The legacy Connection binary_sensor is no longer discovered, and its
        old config topic is purged (empty retained payload) for existing HA installs."""
        calls = self._capture(monkeypatch)
        conn_topic = "homeassistant/binary_sensor/dtsu666_1_connection/config"
        assert not any(t == conn_topic and isinstance(p, dict) for t, p, _r in calls)
        assert any(t == conn_topic and not isinstance(p, dict) for t, p, _r in calls)

    def test_status_entity_present(self, monkeypatch):
        """The five-state status sensor is discovered, backed by the energy topic."""
        calls = self._capture(monkeypatch)
        status = [p for _t, p, _r in calls
                  if isinstance(p, dict) and p.get("unique_id") == "dtsu666_1_status"]
        assert len(status) == 1
        cfg = status[0]
        assert cfg["state_topic"] == "Electricity/dtsu666/1/energy"
        assert cfg["value_template"] == "{{ value_json.status }}"
        assert cfg["default_entity_id"] == "sensor.chint_dtsu666_1_status"

    def test_no_part_based_entity(self, monkeypatch):
        """No HA entity is based on the internal 'part' field anymore."""
        calls = self._capture(monkeypatch)
        for _t, p, _r in calls:
            if isinstance(p, dict) and isinstance(p.get("value_template"), str):
                assert "value_json.part" not in p["value_template"]
