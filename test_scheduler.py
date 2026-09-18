"""Scheduler lifecycle tests: prompt shutdown at the minute boundary."""

import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dtsu666_tou import database, modbus, runtime, scheduler

LOCAL_TZ = ZoneInfo("Europe/Zagreb")


def _db_meter():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="sched_")
    os.close(fd)
    db = database.open_database(path)
    mid = database.create_initial_meter(db, 1, 1000.0)
    meter = db.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
    return db, meter, path


class TestShutdown:
    def test_no_energy_read_after_signal_at_boundary(self):
        """Regression: a SIGINT that lands during the minute-boundary
        iteration's electrical read must prevent the energy read from
        starting, so shutdown is prompt."""
        db, meter, path = _db_meter()
        clock = {"now": datetime(2026, 1, 15, 14, 0, 0, tzinfo=LOCAL_TZ)}
        boundary = datetime(2026, 1, 15, 14, 1, 0, tzinfo=LOCAL_TZ)
        hard_stop = datetime(2026, 1, 15, 14, 10, 0, tzinfo=LOCAL_TZ)

        class CountingModbus:
            def __init__(self):
                self.energy_reads = 0

            def read_registers(self, slave, address, count):
                if address == 0x101E:
                    self.energy_reads += 1
                    hi, lo = modbus._pack32(1000.0)
                    words = [0] * count
                    words[0] = hi
                    words[1] = lo
                    return words
                if address == 0x2000 and clock["now"] >= boundary:
                    runtime.running = False   # simulate SIGINT mid-iteration
                return [0] * count

        fm = CountingModbus()

        def now_fn():
            return clock["now"]

        def sleep_fn(seconds):
            clock["now"] += timedelta(seconds=seconds)
            if clock["now"] >= hard_stop:
                runtime.running = False

        runtime.running = True
        try:
            scheduler.run_scheduler(db, fm, meter, now_fn=now_fn, sleep_fn=sleep_fn)
        finally:
            runtime.running = True
            db.close()
            os.unlink(path)

        assert fm.energy_reads == 0, \
            f"energy read started after shutdown signal: {fm.energy_reads}"
