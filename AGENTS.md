# agent.md — project guide for coding agents

Guidance for any agent (human or AI) working on this repository. Read this
before making changes.

## What this is

A Python service that reads one **CHINT DTSU666** electricity meter over
**Modbus RTU** (RS-485), publishes electrical data every second and energy
every minute to **MQTT** (Home Assistant), and persists everything to
**SQLite**. It handles Croatian **HEP** dual-tariff (VT/NT) allocation,
meter replacement, HEP photo-reference corrections, cost accounting, and
Home Assistant auto-discovery. It is packaged and hardened for running as a
systemd service.

## Terminology (do not rename these)

- **HEP** — *Hrvatska elektroprivreda*, the Croatian utility that bills the
  official meter.
- **VT / NT** — *viša / niža tarifa* (higher / lower tariff), the two price
  bands. VT is 08:00–22:00 (summer/DST) or 07:00–21:00 (winter); NT is the
  rest. The boundary shifts with DST and is re-derived per calendar day via
  `zoneinfo`.
- The DTSU666 is a **private sub-meter**; HEP bills from *its own* meter.
  The HEP-reference feature reconciles drift between the two.

These names appear throughout the code, the DB schema, and MQTT topics.
`vt_kwh` / `nt_kwh` are broken out on every reading, daily aggregate, and
period summary.

## Layout

```
dtsu666_tou/          runtime package (python -m dtsu666_tou)
deploy/               installer, systemd unit, udev rule, ops tools, config examples
tools/                dev helpers (make_mock_db.py)
test_*.py             pytest suites at repo root
```

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | version, constants, file paths, `dtsu666.conf` runtime config, `load_config`, NTP gate |
| `runtime.py` | cross-cutting globals `running`, `mqtt_client`, `meter` |
| `time_utils.py` | `now_local`, boundary helpers, timestamp parsing |
| `modbus.py` | CRC-16, `ModbusRTU`, float32, register tables, readers, `FakeModbus` |
| `tariff.py` | HEP VT/NT schedule + `allocate_interval` (DST-aware) |
| `database.py` | SQLite schema, meter instances, `record_energy`, retention |
| `periods.py` | calendar period ranges + `period_summary` / `build_periods` |
| `hep.py` | HEP photo-reference corrections (`reference_readings`) |
| `pricing.py` | `tariffs.ini` loading + cost math (pure) |
| `cost.py` | `publish_cost` + live tariff hot-reload |
| `discovery.py` | HA MQTT discovery (64 entities, stable `unique_id`s) |
| `mqtt.py` | paho client + best-effort `mqtt_publish` |
| `publishing.py` | meter / electrical / energy / system publishers |
| `lifecycle.py` | meter initialisation + first full read |
| `commissioning.py` | commissioning / reference-activation state machine |
| `scheduler.py` | wall-clock polling loop + HEP/tariff file watchers |
| `watchdog.py` | systemd `sd_notify` readiness, watchdog, status |
| `logging_utils.py` | structured, journald-friendly logging |
| `app.py` | CLI, signal handling, `--test`, supervised `run_forever`, `main` |
| `web.py` | on-demand read-only web status page (`python -m dtsu666_tou.web`) |

### Dependency discipline (critical)

The pure-math modules (`config`, `runtime`, `time_utils`, `modbus`, `tariff`,
`database`, `periods`, `hep`, `pricing`) import **no paho/serial at import
time** — `modbus` uses a lazy `import serial`. This keeps them unit-testable
with the standard library only. Do not add `paho`/`serial` imports to these
modules at module scope; import them lazily inside the function that needs
them, or leave them in `mqtt.py` / `modbus.py` readers.

`runtime.py` holds shared mutable globals; `mqtt_publish` reads
`runtime.mqtt_client` on every call so tests can monkey-patch it once and
affect all subsystems uniformly.

## Commands

```bash
# environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# run
.venv/bin/python -m dtsu666_tou                 # normal operation
.venv/bin/python -m dtsu666_tou --test          # smoke test (fake hardware)
.venv/bin/python -m dtsu666_tou --replace-meter # interactive replacement
.venv/bin/python -m dtsu666_tou --skip-ntp-check

# web status page
.venv/bin/python -m dtsu666_tou.web             # http://127.0.0.1:8080/

# tests (132 tests, temp files only)
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest test_dtsu666_tou.py -v

# deployment dry-runs
./deploy/install.sh --dry-run
./deploy/build-bundle.sh --verify

# dev helper
.venv/bin/python tools/make_mock_db.py output.db
```

`pytest.ini` sets `testpaths = .` only; there is no lint/typecheck
configured. Test files: `test_dtsu666_tou.py` (42), `test_hardening.py`
(36), `test_deploy_tools.py` (28), `test_commissioning.py` (18),
`test_scheduler.py` (1), `test_web.py` (7).

## Configuration files

Resolved **relative to the current working directory** (the systemd unit
sets `WorkingDirectory` accordingly):

- `secrets.ini` — `[SERIAL]` (port/baudrate/timeout/address), `[MQTT]`
  (host/port/username/password), optional `[HEP Correction]`. The **only**
  file that may hold credentials.
- `tariffs.ini` — HEP price components under `[COST]`; re-read on every cost
  calculation (hot-reload).
- `dtsu666.conf` — non-secret runtime tuning (`[MONITORING]`, `[WATCHDOG]`,
  `[RETENTION]`, `[LOGGING]`, `[DATABASE]`, `[SERIAL]`). Every key optional;
  defaults in `config.RUNTIME_DEFAULTS`.
- `dtsu666_energy.db` — SQLite database (gitignored via `*.db`).

## Key invariants and gotchas

- **Naive datetimes are local Europe/Zagreb time** — see
  `tariff.current_tariff` and `allocate_interval`. Never assume UTC.
- **`vt + nt == delta`** is an invariant asserted by the audit tool and
  tests; `allocate_interval` guarantees it.
- **Measured vs estimated allocation**: a gap ≤ 120 s with the same tariff
  at both ends is `measured`; otherwise the delta is split proportionally
  across VT/NT by wall-clock time and tagged `estimated`. Raw data is never
  overwritten — corrections/references layer on top at query time.
- **MQTT availability payload is a plain string** (`online`/`offline`), never
  JSON, so it cannot be mistaken for a discovery config.
- On a **clean stop**, availability is deliberately left at `online` and a
  `disconnected` status is published; the broker's **last will** publishes
  `offline` only on an *unexpected* death.
- `run_forever` in `app.py` supervises the whole acquisition loop with
  exponential backoff — it must not `exit()` on a recoverable fault.
- `scheduler._safe` wraps every optional step so a hot-reload or publish
  error never stops energy recording.
- The DB uses WAL, `synchronous=NORMAL`, `foreign_keys=ON`, and a busy
  timeout with retry (`database.retry_on_locked`). Losing a minute's reading
  to `SQLITE_BUSY` is treated as unacceptable.
- The web status page (`web.open_readonly`) reads the live `-wal` file; it
  falls back to `immutable=1` only when there is no `-wal` (a cleanly
  stopped DB), never for a running service. On install, the DB and its
  `-wal`/`-shm` sidecars are `0644` (world-readable) so `dtsu666-web` runs
  without sudo.

## Code conventions

- 4-space indentation, no tabs.
- Module docstring at the top; `log = logging.getLogger("dtsu666.<module>")`.
- Section divider comments use `# ==== ... ====`.
- Broad `except Exception:` blocks carry `# noqa: BLE001` and log the
  traceback — the service must survive optional failures.
- Follow the pure/impure split: math and data handling stay import-light;
  I/O (MQTT, serial) stays in `mqtt.py` / `modbus.py` readers and the
  scheduler/publishers.
- No em dashes in source; use commas/parentheses/semicolons.
- Tests run with temp files only; never write to the repo's real
  `secrets.ini` / `*.db` from a test.

## Deployment notes

`deploy/install.sh` installs to `/opt/dtsu666` (code + venv + `etc` + `var`)
and runs as the unprivileged `dtsu666` user (member of `dialout`). Ops tools
installed to `/usr/local/bin`: `dtsu666-audit`, `dtsu666-backup`,
`dtsu666-verify-backup`, `dtsu666-restore`, `dtsu666-web`. The systemd unit
uses `Type=notify` + a watchdog; `watchdog.py` talks `sd_notify`. See
`deploy/README.md` for the failure-mode table and recovery playbook.

## Before committing

- Run `.venv/bin/python -m pytest -q` and confirm 132 passing.
- If you touched the CLI, run `.venv/bin/python -m dtsu666_tou --test`.
- Re-read the module map above to confirm you did not break the pure/impure
  import split.
