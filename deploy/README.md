# DTSU666 logger — deployment and operations

This directory contains everything needed to run the CHINT DTSU666 energy
logger as a proper system service: the installer, the systemd unit, the
udev rule, the command-line tools and the configuration examples.

```
deploy/
├── install.sh              install as a system service (review-first by default)
├── uninstall.sh            remove the service (keeps data unless --purge)
├── build-bundle.sh         build a portable, installable bundle
├── dtsu666_audit.py        read-only integrity & consistency audit
├── dtsu666_backup.py       consistent, verifiable backup (+ verify mode)
├── dtsu666_restore.py      verified restore (guards against mistakes)
├── bin/                    /usr/local/bin wrappers for the tools above
├── systemd/dtsu666.service the unit
├── udev/99-dtsu666.rules   adapter permissions + stable device alias
└── config/                 secrets.ini / tariffs.ini / dtsu666.conf examples
```

## Installed layout

| path | contents | owner / mode |
|---|---|---|
| `/opt/dtsu666/app` | application code | `root:root` 0755 (read-only for the service) |
| `/opt/dtsu666/venv` | virtualenv (paho-mqtt, pyserial) | `root:root` |
| `/opt/dtsu666/etc/secrets.ini` | credentials, Modbus address, HEP corrections | `root:dtsu666` 0640 |
| `/opt/dtsu666/etc/tariffs.ini` | HEP price components | `root:root` 0644 |
| `/opt/dtsu666/etc/dtsu666.conf` | non-secret tuning | `root:root` 0644 |
| `/opt/dtsu666/var/dtsu666_energy.db` | the database | `dtsu666:dtsu666` 0640 |
| `/opt/dtsu666/var/audits` | audit reports | `dtsu666:dtsu666` 0750 |
| `/opt/dtsu666/var/backups` | backup bundles | `dtsu666:dtsu666` 0750 |
| `/usr/local/bin/dtsu666-*` | command wrappers | `root:root` 0755 |
| `/etc/systemd/system/dtsu666.service` | the unit | `root:root` 0644 |
| `/etc/udev/rules.d/99-dtsu666.rules` | adapter rule | `root:root` 0644 |

The service runs as the unprivileged system user **`dtsu666`**, whose only
special privilege is membership in the **`dialout`** group (which owns the
USB-RS485 adapter, `/dev/ttyUSB0`, mode 0660).  It never runs as root.

## Initial installation

```bash
# 1. Review what will happen (no root, nothing is changed)
./deploy/install.sh --dry-run

# 2. Install (installs but does NOT enable/start the service)
sudo ./deploy/install.sh

# 3. Verify before starting anything
sudo dtsu666-audit                       # read-only database audit
sudo -u dtsu666 /opt/dtsu666/venv/bin/python -m dtsu666_tou --test
systemd-analyze verify /etc/systemd/system/dtsu666.service

# 4. Start it
sudo systemctl start dtsu666
journalctl -u dtsu666 -f                 # watch the first minutes

# 5. Once happy, make it survive a reboot
sudo systemctl enable dtsu666
```

Useful installer options: `--prefix`, `--user`, `--src`, `--bundle FILE`,
`--config-dir DIR`, `--no-venv`, `--no-seed-db`, `--no-service`, `--no-udev`,
`--no-wrappers`, `--enable`, `--dry-run`.

### Installing from a portable bundle

```bash
./deploy/build-bundle.sh --include-db --verify        # -> dist/dtsu666-portable-*.tar.gz
# copy the .tar.gz to the target host, then
tar -xzf dtsu666-portable-*.tar.gz && cd dtsu666-portable-*
sudo ./app/deploy/install.sh --src "$PWD/app" --config-dir "$PWD/config"
```

Credentials are **excluded** from bundles by default (`--with-secrets`
includes them, for which the bundle must then be kept private).  If the
bundle has no `secrets.ini`, the installer installs the example and prints
an `ACTION REQUIRED` notice.

## Removal

```bash
sudo ./deploy/uninstall.sh          # stops/disables, removes unit+udev+wrappers,
                                    # keeps data and configuration
sudo ./deploy/uninstall.sh --purge  # also removes /opt/dtsu666 (takes a backup first)
```

## Configuration

`/opt/dtsu666/etc/dtsu666.conf` holds everything non-secret; every key is
optional and the documented default applies when it is missing.  See
`config/dtsu666.conf.example`.  Highlights:

| key | default | effect |
|---|---|---|
| `MONITORING.freshness_seconds` | 240 | a reading older than this marks the meter `disconnected` and zeroes the corrected counters |
| `MONITORING.availability_suffix` | availability | retained availability topic: `Electricity/dtsu666/<addr>/availability` |
| `LOGGING.level` | INFO | journald severity threshold |
| `WATCHDOG.interval_seconds` | 0 | sd_notify ping interval (normally taken from `$WATCHDOG_USEC`) |
| `DATABASE.busy_timeout_ms` / `lock_retries` | 5000 / 5 | SQLite contention behaviour |
| `SERIAL.reopen_after_failures` | 3 | consecutive transport failures before the port is re-opened |
| `RETENTION.minute_days` / `daily_days` | 365 / 3650 | retention windows |

`secrets.ini` is unchanged from the original design: `[SERIAL]`, `[MQTT]`
and `[HEP Correction]`.  Only `secrets.ini` may contain credentials.

## Failure modes and how they are handled

This table is the core of the hardening work: each failure that used to be
invisible or fatal, and what now happens instead.

| failure | before | now |
|---|---|---|
| process dies (crash, OOM, kill) | retained `/energy` kept saying `status: active` forever; Home Assistant showed the meter as fine | the MQTT **last will** publishes `offline` (retained) on `Electricity/dtsu666/<addr>/availability`; `availability_topic` is set on every discovered entity, so HA immediately marks them **unavailable** |
| process hangs (deadlock, wedged serial read) | no detection at all | `Type=notify` + `WatchdogSec=60`; the loop pings `WATCHDOG=1` every 30 s, so systemd kills and restarts a hung process |
| unexpected exception escapes the loop | process exited; only a systemd restart (if any) brought it back | the top-level supervisor logs the traceback and re-enters the loop with exponential backoff (1 s → 60 s), resetting after a healthy start |
| meter unreachable at boot | `FATAL: meter initialization failed` → `exit(1)` | the failure is logged and retried; the service still reports `READY=1` (it is up, the *meter* is not) and recovers on its own |
| USB-RS485 adapter unplugged / re-plugged | every read raised; the process never re-opened the port, so it stayed dead until a manual restart | after `reopen_after_failures` transport faults the port is closed and re-opened with exponential backoff; protocol faults (device answered with an exception code) do **not** trigger a re-open |
| MQTT broker down | publish errors were only printed | auto-reconnect (unchanged) plus the availability topic, so HA shows the outage; publish failures are isolated so they are never mistaken for meter failures |
| SQLite `database is locked` | that minute's reading was lost | `busy_timeout` + retry with backoff (`DATABASE.lock_retries`), WAL, `synchronous=NORMAL`, `foreign_keys=ON` |
| stale data with the process alive | already surfaced via the 5-state status | unchanged, and the threshold is now configurable (`freshness_seconds`) |
| silent restart / version drift | not visible | the retained `/system` payload carries `version`, `hostname`, `process_id` and `uptime_seconds` |
| a bad `[HEP Correction]` edit or a publish error inside the loop | could take down the acquisition loop | every optional step runs through a guard that logs the traceback and continues |
| secrets readable by other users | `secrets.ini` was world-readable | installed `0640 root:dtsu666`; the audit tool flags any other mode |
| unused venvs / duplicate credentials inside the source tree | shipped inside every backup (21.8 MiB) | removed, and the backup tool skips venvs and nested `secrets.ini` (bundle is now 0.9 MiB) |

### Signals and shutdown

`SIGINT`/`SIGTERM` set a flag; the loop finishes the current iteration and
exits promptly (it never starts another energy read at a minute boundary).
On shutdown the process publishes a retained `status: disconnected` and
`STOPPING=1`, then disconnects cleanly.

The availability topic is deliberately **left at `online`** on a clean stop:
a planned stop should be visible as an explicit `disconnected` status,
whereas an *unexpected* death is what the last will reports as `offline`.

## Monitoring from Home Assistant

There are **no new entities**; the existing ones gain an availability
binding:

| signal | source | meaning |
|---|---|---|
| `sensor.chint_dtsu666_1_status` | `<base>/energy` → `status` | `disconnected` / `acquiring` / `ready` / `missing` / `active` — data liveness |
| entity availability | `<base>/availability` | `online` / `offline` — **process** liveness (LWT) |
| `/system` payload | `<base>/system` | `tariff`, `status`, `version`, `hostname`, `pid`, `uptime_seconds` |

The combination makes failures unambiguous:

* entities **unavailable** → the process is not running (crash, a hang killed
  by the watchdog, or a clean stop);
* entities **available** but status `disconnected` → the process is fine, the
  *meter* is not answering;
* status present but data stale → check the age against
  `MONITORING.freshness_seconds`.

The legacy `binary_sensor ..._connection` is still purged on every discovery
publish, so upgrading leaves no stale entity behind.

## Operations

### Audit (read-only, safe at any time)

```bash
sudo dtsu666-audit                 # summary + var/audits/audit-<ts>-summary.md
sudo dtsu666-audit --json          # machine-readable report
```

Checks: SQLite `integrity_check` and `foreign_key_check`, WAL state, schema,
duplicate and non-monotonic timestamps, gaps, counter decreases, negative and
implausible deltas, the `vt + nt == delta` invariant, per-day agreement
between `readings` and `energy_daily`, HEP reference coverage, and the
configuration (credentials **redacted**).  Exit codes: `0` clean, `1` errors
found, `2` the audit itself failed.

### Backup

```bash
sudo dtsu666-backup                          # -> var/backups/dtsu666-backup-<ts>.tar.gz
sudo dtsu666-backup --no-secrets             # for off-site copies
dtsu666-verify-backup <bundle.tar.gz>        # verify at any time
```

The snapshot uses the SQLite online-backup API, so it is consistent even
while the service is writing.  Every file is hashed into `manifest.json`;
bundles are mode 0600 and the newest 10 are kept (`--keep`).

### Restore

```bash
dtsu666-verify-backup <bundle>       # ALWAYS verify first
sudo systemctl stop dtsu666
sudo dtsu666-restore <bundle> --force
sudo systemctl start dtsu666
journalctl -u dtsu666 -n 50
```

Safety properties: the bundle is always verified before anything is written
(a failure aborts with exit 1); the service must be stopped and an existing
database requires `--force`; the current database is preserved as a
consistent `<db>.pre-restore-<ts>` copy; the old `-wal`/`-shm` files are
moved aside so a restored database cannot inherit a stale write-ahead log.
`--dry-run`, `--target` and `--restore-config` are also available.

## Recovery playbook

| symptom | diagnosis | action |
|---|---|---|
| HA entities unavailable | `systemctl status dtsu666` | if failed, read `journalctl -u dtsu666 -n 100`; the supervisor usually recovers by itself within a minute |
| status `disconnected`, entities available | `dtsu666-audit` for the age of the last reading | check adapter and cabling: `ls -l /dev/ttyUSB0`, `udevadm info -n /dev/ttyUSB0` |
| adapter appears under a different name | `journalctl -u dtsu666 \| grep -i 'serial port'` | use `/dev/ttyDTSU666` (the udev alias) in `secrets.ini`, or update the port |
| no readings after a reboot | `systemctl is-enabled dtsu666` | `sudo systemctl enable dtsu666` |
| permission denied on the serial port | `sudo -u dtsu666 test -r /dev/ttyUSB0` | `sudo udevadm control --reload-rules && sudo udevadm trigger`; re-plug the adapter |
| `database is locked` repeatedly | `journalctl -u dtsu666 \| grep -i locked` | another process holds the write lock; increase `DATABASE.busy_timeout_ms` or avoid concurrent tools |
| database corrupt | `sudo dtsu666-audit` reports errors | stop the service, `sudo dtsu666-restore <newest verified bundle> --force`, restart |
| disk full | `df -h /opt` | prune `var/backups` / `var/audits`; lower `RETENTION.minute_days` |

Handy commands:

```bash
systemctl status dtsu666                      # state, last log lines, watchdog info
journalctl -u dtsu666 -p warning --since -1h  # problems only
journalctl -u dtsu666 | grep -i restart       # supervisor restarts
systemctl show dtsu666 -p NRestarts           # systemd restart count
```

## Upgrading / redeploying

```bash
sudo dtsu666-backup             # 1. always back up first
sudo ./deploy/install.sh        # 2. re-install code + config (idempotent)
sudo systemctl restart dtsu666  # 3. restart and watch
journalctl -u dtsu666 -f
```

`install.sh` keeps the existing database, `dtsu666.conf` and (unless newer
ones are supplied) the configuration, backing up `secrets.ini` before
replacing it.  Application code under `/opt/dtsu666/app` is replaced
wholesale, so files deleted upstream do not linger.

## Security notes

* Only `secrets.ini` holds credentials; it is `0640 root:dtsu666`.
  `dtsu666.conf`, `tariffs.ini` and every report are credential-free — the
  audit and backup tools actively redact them.
* Backup bundles contain `secrets.ini` (so they are self-contained) and are
  therefore created mode 0600.  Use `--no-secrets` for off-site copies.
* The service never runs as root: `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, an empty `CapabilityBoundingSet`, and write
  access only to `/opt/dtsu666/var`.
* MQTT credentials should be a dedicated broker user restricted to the
  `Electricity/#` and `homeassistant/#` topics.

## Development

```bash
cd <repo>
.venv/bin/python -m pytest -q          # 125 tests, temp files only
.venv/bin/python -m dtsu666_tou --test     # end-to-end smoke test
./deploy/install.sh --dry-run          # rehearse an install
./deploy/build-bundle.sh --verify      # rehearse a release
```
