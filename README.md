# CHINT DTSU666 — Modbus RTU → MQTT / SQLite energy logger

This project was developed with the help of multiple coding agents and ChatGPT.

Reads one CHINT DTSU666 electricity meter over Modbus RTU (RS-485), publishes
electrical data every second and energy every minute, and persists all readings
to SQLite.  Tariff (VT/NT) allocation, meter-replacement handling, HEP
photo-reference corrections, cost accounting and Home Assistant discovery are
built in.

> **Running this as a service?** See [`deploy/README.md`](deploy/README.md)
> for installation, the configuration reference, failure handling, monitoring
> and the recovery playbook.

## Quick start

```bash
# 1. dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. configuration
cp deploy/config/secrets.ini.example secrets.ini
nano secrets.ini                       # serial port, Modbus address, MQTT broker

# 3. run (an NTP-synchronised clock is required; --skip-ntp-check for dev only)
.venv/bin/python -m dtsu666_tou
```

Config files (`secrets.ini`, `tariffs.ini`, `dtsu666.conf`) and the database
(`dtsu666_energy.db`) are resolved relative to the current working directory.

## Configuration (`secrets.ini`)

```ini
[SERIAL]
port=/dev/ttyUSB0
baudrate=9600
timeout=1.0
address=1

[MQTT]
host=127.0.0.1
port=1883
username=
password=
```

An optional `[HEP Correction]` section holds the HEP photo references (see
*Manual corrections* below).  This is the only file that may contain
credentials.

## MQTT topics

| Topic                              | Retained | Description                |
|------------------------------------|----------|----------------------------|
| `Electricity/dtsu666/1/meter`      | yes      | meter info + basic regs    |
| `Electricity/dtsu666/1/electrical` | no       | voltages, currents, power  |
| `Electricity/dtsu666/1/energy`     | yes      | energy + period summaries  |
| `Electricity/dtsu666/1/system`     | yes      | version, host, pid, uptime |
| `Electricity/dtsu666/1/availability` | yes    | `online` / `offline` (LWT) |

`<address>` in the topic comes from the configured Modbus slave address.

## CLI

| Flag                | Purpose                          |
|---------------------|----------------------------------|
| `--test`            | smoke-test with fake hardware    |
| `--replace-meter`   | interactive meter replacement    |
| `--skip-ntp-check`  | bypass the NTP gate (dev only)   |
| `--config PATH`     | alternate `secrets.ini` path     |
| `--db PATH`         | alternate database path          |

## Database schema (`dtsu666_energy.db`)

- **meters** — instance history (`valid_from`, `valid_to`, `initial_imp_ep`)
- **readings** — raw minute samples with `allocation_method`
  (`baseline`/`measured`/`estimated`)
- **energy_daily** — per-day aggregates (`absolute_kwh` = latest counter,
  `total_kwh`, `vt_kwh`, `nt_kwh`)
- **reference_readings** — HEP photo-reference anchors
- **corrections** — manual override for disputed periods (VT/NT per date)

## Missing-interval handling

`delta = current ImpEp − last stored reading`.  If the elapsed time is ≤ 120 s
and both ends are in the same tariff, the allocation is **measured**; otherwise
it is **estimated** by splitting the delta across VT/NT proportionally to the
wall-clock time in each tariff.  Estimated values are tagged in the database —
raw data is never overwritten.

## Manual corrections

Insert rows into the `corrections` table to override VT/NT for a given date.
The correction layer runs at query time; raw historical data is unchanged.

HEP photo references are entered in `secrets.ini`:

```ini
[HEP Correction]
1 = 2026-04-01 16:00, 9000, 18000, photo today
```

Each entry is `timestamp, VT counter, NT counter, reason`.  The logger inserts
them as `reference_readings` anchors and reconstructs cumulative
`vt_corrected` / `nt_corrected` counters for the period summaries.  The file is
watched for changes, so a new photo can be added without a restart.

## Meter replacement

```bash
.venv/bin/python -m dtsu666_tou --replace-meter
```

Confirms interactively, closes the old meter instance and creates a fresh
baseline.  Historical data from the previous meter is preserved.

## Home Assistant discovery

Discovery configs are **retained** and published on every MQTT connect
(startup and reconnects).  The program also subscribes to
`homeassistant/status` and republishes discovery when HA broadcasts `online`
after an HA restart.

**64 entities** in total — 56 on the `CHINT DTSU666 #N` device (22 electrical,
10 energy, 20 period-summary, 3 system, 1 connectivity binary sensor) plus
8 cost sensors on the `ElectricityCost` device.

`unique_id` values follow the `dtsu666_{address}_{name}` scheme, so entity ids
stay stable for existing Home Assistant installations.  Each meter entity
carries a `default_entity_id` (HA ≥ 2026.4) derived from the
device-prefixed name (e.g. `sensor.chint_dtsu666_1_phase_c_power_factor`),
dropping the redundant `dtsu666_1_` from the entity id.

Discovery is driven by the register tables in `dtsu666_tou/modbus.py`, so the
entities follow the meter's register layout.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q          # 125 tests, temp files only
```

| file | tests | covers |
|---|---|---|
| `test_dtsu666_tou.py` | 42 | pure functions, DST/tariff boundaries, DB data handling, period summaries, discovery payloads |
| `test_hardening.py` | 36 | recovery, monitoring, availability, supervisor |
| `test_deploy_tools.py` | 28 | audit / backup / restore tools |
| `test_commissioning.py` | 18 | commissioning and lifecycle |
| `test_scheduler.py` | 1 | wall-clock polling loop |

## Deployment

[`deploy/README.md`](deploy/README.md) covers installation, the configuration
reference, the failure-mode table, monitoring, the recovery playbook and
security notes.

Operational commands (installed by `deploy/install.sh`):

| command | purpose |
|---|---|
| `dtsu666-audit` | read-only integrity & consistency audit (safe at any time) |
| `dtsu666-backup` | consistent, hashed backup bundle of database + config + code |
| `dtsu666-verify-backup` | verify a bundle without touching the system |
| `dtsu666-restore` | verified restore, with safety copy and stale-WAL handling |

Reliability features when run as a service: MQTT last will + per-entity
availability, a systemd `Type=notify` watchdog, a supervised restart loop,
serial auto-reopen after an adapter re-plug, SQLite busy-retry, structured
journald logging, and a status entity for the meter.

## Assumptions

- Tariff allocation uses local wall-clock time (`Europe/Zagreb`); DST is
  handled by `zoneinfo`.
- A "measured" allocation requires a gap of ≤ 120 s and a single tariff at
  both ends of the interval.
- The DTSU666 reports registers as IEEE-754 float32 in big-endian word order.
