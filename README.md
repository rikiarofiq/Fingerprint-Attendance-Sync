# ZKTeco Attendance Sync

Pull attendance logs from one or many ZKTeco fingerprint machines into MySQL, with a small web UI to manage devices, schedule syncs and trigger them manually. Runs with a single `docker compose up`.

> Not affiliated with or endorsed by ZKTeco. "ZKTeco" is a trademark of its respective owner.

## Features

- **Multiple devices**: add, edit, deactivate and delete machines from a *Master Device* page (name, IP, port, comm key, sync interval).
- **Per-device schedule**: every device has its own interval (in minutes). Serial number, MAC address and firmware are read from the machine automatically.
- **Light-weight scheduled sync**: each run first checks the record count on the device (~0.07 s). If nothing changed, nothing is downloaded and nothing is written.
- **Self-healing after outages**: if the network or power was down, the first sync after recovery saves everything that was missed. No manual action needed.
- **Daily full sync** per device as a safety net.
- **Manual sync** for one device or all active devices, with an optional date range (useful for back-filling old data).
- **No duplicates**: a scan is unique per `(device_id, user_id, scan_time)`, and inserts use `INSERT IGNORE`, so only new scans are stored no matter how often you sync.
- **Employee names**: users are synced at 00:00 and 12:00, when a device is added, and on demand from the UI.
- **Dashboard**: filter by date, device and ID/name. Each row shows the machine name, IP and MAC address.
- **Sync history**: per device, with number of scans read and newly stored, and error messages.
- **Works offline**: all web assets are bundled locally, so the UI needs no internet access.

## Quick start

Requirements: Docker with Docker Compose.

```bash
git clone <your-repo-url>
cd <repo>
cp .env.example .env        # then edit .env and set the passwords
docker compose up -d --build
```

Generate strong values with:

```bash
openssl rand -hex 16   # for MYSQL_ROOT_PASSWORD and DB_PASS
openssl rand -hex 32   # for SECRET_KEY
```

Then open **http://localhost:8080**, go to **Master Device**, and add your machine (default port `4370`, comm key `0`). Use the **Tes** (test) button to verify the connection. The first sync starts automatically within 30 seconds.

The MySQL port is bound to `127.0.0.1:3307` only (not exposed to the network). Credentials are in `.env`.

## Configuration (`.env`)

| Variable | Description | Default |
|---|---|---|
| `MYSQL_ROOT_PASSWORD` | MySQL root password (required) | none |
| `DB_NAME` | Database name | `absensi` |
| `DB_USER` | Application DB user | `absensi` |
| `DB_PASS` | Application DB password (required) | none |
| `SECRET_KEY` | Flask secret key (required) | none |
| `TZ` | Time zone for containers | `Asia/Jakarta` |
| `WEB_PORT` | Web UI port on the host | `8080` |
| `DB_HOST_PORT` | MySQL port on the host (localhost only) | `3307` |

> MySQL only reads its passwords from the environment the first time the data volume is created. If you change them in `.env` later, also run `ALTER USER` in MySQL.

## How syncing works

| Run | What it does |
|---|---|
| **Scheduled (quick)** | Every 30 s the scheduler checks which devices are due (per-device interval). For each one it reads the device record count; if unchanged, it skips. Otherwise it downloads the logs and stores scans since *(latest stored scan − 1 day)*. Skipped runs are not written to the history. |
| **Daily full** | Once every 24 h per device, downloads and stores everything not yet in the database. |
| **Users** | At 00:00 and 12:00, when a device is first added, or with the *User* buttons. |
| **Manual** | One device or all active devices. Without dates it is a full sync; with dates it only stores scans in that range and does not move the "synced up to" marker, so nothing is skipped by accident. |

At most 4 devices are synced in parallel (`MAX_PARALLEL` in `app/sync.py`). A device is never synced twice at the same time, and an unreachable device does not block the others.

## Database

| Table | Purpose |
|---|---|
| `devices` | Master device list (+ serial, MAC, firmware, last status, sync markers) |
| `attendance` | Scans: `device_id`, `user_id`, `scan_time`, `status`, `punch` |
| `employees` | Users per device, primary key `(device_id, user_id)` |
| `sync_history` | Log of non-skipped syncs |

The same person may have different IDs on different machines, so employees are stored per device. Example query:

```sql
SELECT a.scan_time, a.user_id, e.name, d.name AS device, d.ip, d.mac
FROM attendance a
JOIN devices d ON d.id = a.device_id
LEFT JOIN employees e ON e.device_id = a.device_id AND e.user_id = a.user_id
WHERE a.scan_time >= '2026-01-01'
ORDER BY a.scan_time DESC;
```

`status` and `punch` are the raw numbers reported by the device. Their meaning depends on the machine's model, firmware and configuration, so check them against your device before mapping them to IN/OUT or verification type.

Upgrading from the earlier single-device version is automatic: existing data is migrated into a first device on startup.

## Limitations

- **No "since date" command**: ZKTeco devices send their whole log, and filtering happens after download. The record-count check is what keeps scheduled syncs cheap.
- **Device capacity is limited** (100,000 scans on the tested model). Once full, behavior depends on the firmware (old logs overwritten, or new scans refused). Keep syncing regularly. Logs deleted on the device before they are synced cannot be recovered.
- **Clock**: timestamps are stored exactly as the device reports them (its local time). Keep device clocks correct.
- **Scans with timestamps more than one day older than the latest stored scan** are only picked up by the daily full sync.
- Tested with a ZKTeco device running firmware `Ver 6.60 Sep 23 2015`, using the [pyzk](https://github.com/fananimi/pyzk) library over TCP port 4370. Other models may behave differently.

## Security

**The web UI has no authentication yet.** Anyone who can reach it can view data, change devices and trigger syncs. Do not expose it to the internet. Run it on a trusted network, behind a VPN, or behind a reverse proxy that adds authentication.

Secrets live only in `.env`, which is git-ignored. Only `.env.example` is committed.

## Running without internet

The UI bundles Bootstrap 5.3.3 locally (`app/static/vendor/bootstrap/`), so it works on an offline network. Internet is only needed to build or pull images (Docker Hub and PyPI). For an offline server, build on a machine with internet, then move the images:

```bash
docker compose build
docker save -o images.tar $(docker compose config --images)
# copy images.tar to the server, then:
docker load -i images.tar
docker compose up -d
```

## Troubleshooting

- **"Gagal konek" / connection failed**: check that the Docker host can reach the device (`nc -vz <device-ip> 4370`), that the port is correct, and that the comm key matches the device setting (usually `0`).
- **A device never syncs**: check it is *Aktif* (active) in Master Device and look at its status and message on that page.
- **Broken pipe / timeout errors**: the device is unreachable or busy. Syncing retries on the next interval.

## Project layout

```
docker-compose.yml, Dockerfile, requirements.txt
.env.example                 # template for .env (copy it)
app/main.py                  # web UI, routes, scheduler
app/sync.py                  # talks to the devices and writes to MySQL
app/db.py                    # connection, schema and migrations
app/templates/               # HTML pages
app/static/vendor/bootstrap/ # bundled Bootstrap
```

## Tech stack

Python 3.12, Flask, APScheduler, [pyzk](https://github.com/fananimi/pyzk), PyMySQL, Gunicorn, MySQL 8, Docker Compose.
