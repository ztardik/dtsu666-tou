# dtsu666_tou — CHINT DTSU666 logger package

The runtime package: reads one DTSU666 over Modbus RTU, publishes to MQTT and
persists to SQLite.  See the repository `README.md` for installation and
configuration; this file is a map of the code.  The Croatian utility and tariff
terms used throughout (HEP, VT/NT) are explained under *Terminology* in the
repository README.

## Run

```bash
.venv/bin/python -m dtsu666_tou                 # normal operation
.venv/bin/python -m dtsu666_tou --test          # smoke-test with fake hardware
.venv/bin/python -m dtsu666_tou --replace-meter # interactive meter replacement
.venv/bin/python -m dtsu666_tou --skip-ntp-check
.venv/bin/python -m dtsu666_tou --config /path/to/secrets.ini
.venv/bin/python -m dtsu666_tou --db /path/to/dtsu666_energy.db
```

Config files (`secrets.ini`, `tariffs.ini`, `dtsu666.conf`) and the database
(`dtsu666_energy.db`) are resolved relative to the current working directory,
so the service starts with `WorkingDirectory` set to the config directory and
`PYTHONPATH` pointing at the parent of this package — see
`deploy/systemd/dtsu666.service`.

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | version, constants, file paths, `dtsu666.conf`, `load_config`, `check_ntp_sync` |
| `runtime.py` | cross-cutting globals: `running`, `mqtt_client` |
| `time_utils.py` | `now_local`, boundary helpers, timestamp parsing |
| `modbus.py` | CRC-16, `ModbusRTU`, `float32`, register tables, decode, readers, `FakeModbus` |
| `tariff.py` | HEP VT/NT schedule + `allocate_interval` (DST-aware) |
| `database.py` | SQLite schema, meter instances, `record_energy`, retention |
| `periods.py` | calendar period ranges + `period_summary` / `build_periods` |
| `hep.py` | HEP photo-reference corrections (`reference_readings`) |
| `pricing.py` | `tariffs.ini` loading + cost math (pure, no MQTT) |
| `cost.py` | `publish_cost` + live tariff hot-reload |
| `discovery.py` | Home Assistant MQTT discovery (64 entities, stable `unique_id`s) |
| `mqtt.py` | paho client + best-effort `mqtt_publish` |
| `publishing.py` | meter / electrical / energy / system publishers |
| `lifecycle.py` | meter initialisation and the first full read |
| `commissioning.py` | commissioning / reference-activation state machine |
| `scheduler.py` | wall-clock polling loop + HEP/tariff file watchers |
| `watchdog.py` | systemd `sd_notify` readiness, watchdog and status |
| `logging_utils.py` | structured, journald-friendly logging setup |
| `app.py` | CLI, signal handling, `--test` smoke test, `main` |
| `__main__.py` | `python -m dtsu666_tou` entry point |

## Dependency graph (acyclic)

`config` ← `time_utils` ← {`tariff`,`database`,`hep`,`cost`,`scheduler`};
`modbus` (lazy `import serial`); `tariff` ← `database`; `periods` ← `hep`;
`pricing` ← `cost`; `runtime` ← `mqtt`; `mqtt` ← {`discovery`,`publishing`,`cost`};
`lifecycle`/`scheduler` ← many; `app` ← all.

The pure-math modules (`config`, `runtime`, `time_utils`, `modbus`, `tariff`,
`database`, `periods`, `hep`, `pricing`) import **no paho/serial at import
time**, so they are unit-testable with the standard library only.

## Tests

```bash
.venv/bin/python -m pytest test_dtsu666_tou.py -v
.venv/bin/python -m dtsu666_tou --test
```
