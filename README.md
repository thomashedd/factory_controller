# Factory Controller — Fanuc 31i CNC Data Collection & Monitoring

A Dockerized application that collects real-time data from a Fanuc 31i CNC controller over the FOCAS protocol, stores it in TimescaleDB, visualizes it in Grafana, and sends Slack notifications on machine state changes.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [Components](#components)
  - [Collector (collector.py)](#collector-collectorpy)
  - [pyfanuc.py (Vendored Library)](#pyfanucpy-vendored-library)
  - [TimescaleDB](#timescaledb)
  - [Grafana](#grafana)
  - [Slack Notifications](#slack-notifications)
- [CNC Machine Details](#cnc-machine-details)
- [Database Schema](#database-schema)
- [Environment Variables](#environment-variables)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [First-Time Setup](#first-time-setup)
  - [Testing the CNC Connection](#testing-the-cnc-connection)
  - [Running the Full Stack](#running-the-full-stack)
  - [Verifying Data Collection](#verifying-data-collection)
- [Development Guide](#development-guide)
  - [Making Changes to the Collector](#making-changes-to-the-collector)
  - [Modifying the Database Schema](#modifying-the-database-schema)
  - [Editing the Grafana Dashboard](#editing-the-grafana-dashboard)
  - [Changing Poll Intervals](#changing-poll-intervals)
  - [Adding a New Data Point](#adding-a-new-data-point)
- [Deploying to a VM](#deploying-to-a-vm)
- [Troubleshooting](#troubleshooting)
- [Key Decisions & Gotchas](#key-decisions--gotchas)

---

## Architecture Overview

```
┌──────────────┐    FOCAS/TCP     ┌──────────────┐     SQL      ┌──────────────┐
│  Fanuc 31i   │◄────────────────►│  Collector    │─────────────►│ TimescaleDB  │
│  CNC Machine │   port 8193      │  (Python)     │              │  (Postgres)  │
│  $CNC_HOST   │                  └──────┬───────┘              └──────┬───────┘
└──────────────┘                         │                             │
                                         │ HTTP                       │ SQL
                                         ▼                            ▼
                                  ┌──────────────┐           ┌──────────────┐
                                  │    Slack      │           │   Grafana    │
                                  │  (Webhooks)   │           │  Dashboard   │
                                  └──────────────┘           └──────────────┘
```

All application components run as Docker containers via `docker-compose.yml`. The collector talks directly to the CNC machine over the shop floor network.

## Project Structure

```
factory_controller/
├── collector.py                              # Main data collection script
├── pyfanuc.py                                # Vendored FOCAS protocol library (pure Python)
├── Dockerfile                                # Container image for the collector
├── docker-compose.yml                        # Orchestrates all services
├── requirements.txt                          # Python deps (currently empty — psycopg2 installed in Dockerfile)
├── db/
│   └── init.sql                              # TimescaleDB schema (runs on first container start only)
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── timescaledb.yml               # Auto-configures the DB connection in Grafana
        └── dashboards/
            ├── dashboards.yml                # Tells Grafana to load dashboards from this directory
            └── cnc-overview.json             # Pre-built dashboard: status, speeds, axes, alarms, etc.
```

## Components

### Collector (`collector.py`)

The core of the application. A single Python script that:

1. **Connects** to the Fanuc 31i controller via FOCAS (TCP port 8193)
2. **Polls data** on three tiers to avoid overloading the controller:
   - **Status (every 1s):** Spindle speed, feed rate, running/not-running flag
   - **Light (every 10s):** Program info, axis positions (abs/rel/machine), tool number, alarms, controller datetime
   - **Heavy (every 5 min):** Macro variables (100–199), PMC data, controller parameters (1–50), diagnostics (1–50), system info
3. **Writes** each tier to its own TimescaleDB hypertable
4. **Sends Slack notifications** on startup (current state) and on every running/stopped state transition
5. **Heartbeat** thread pings `getsysinfo()` every 60s to detect connection loss

The "running" state is determined by: `spindle_speed != 0 OR feed_rate != 0`.

**CLI flags:**
- `python collector.py` — full collection mode (requires TimescaleDB)
- `python collector.py --test` — connect, read status once, print, disconnect (no DB needed)
- `python collector.py --test 192.168.1.50` — test against a different IP

### `pyfanuc.py` (Vendored Library)

A pure-Python implementation of the Fanuc FOCAS protocol. Sourced from [github.com/diohpix/pyfanuc](https://github.com/diohpix/pyfanuc) (Unlicense / public domain).

**Why vendored:** The `pyfanuc` package on PyPI depends on `fwlipy`, which is not available as a downloadable package. The GitHub repo is not set up as an installable pip package (no `setup.py` or `pyproject.toml`). Since it's a single file with no dependencies beyond the Python standard library, we vendor it directly.

**Key methods used by the collector:**
| Method | What it reads |
|---|---|
| `readactspindlespeed()` | Spindle RPM |
| `readactfeed()` | Feed rate |
| `readprognum()` | Running + main program number |
| `readprogname()` | Program name with path |
| `readexecprog()` | Currently executing G-code line |
| `readaxes(what=N)` | Axis positions (1=abs, 2=rel, 4=machine) |
| `readmacro(first, last)` | Macro variable values |
| `readalarmcode(type, withtext)` | Alarm codes with message text |
| `readpmc(datatype, section, first, count)` | PMC/ladder data |
| `readparam(axis, first, last)` | Controller parameters |
| `readdiag(axis, first, last)` | Diagnostic values |
| `getsysinfo()` | CNC type, axis count, version |
| `getdatetime()` | Controller date/time |
| `statinfo()` | Machine state (aut, run, motion, alarm, etc.) |

### TimescaleDB

PostgreSQL 16 with the TimescaleDB extension. Stores all CNC data in hypertables optimized for time-series queries.

**Retention policies (automatic cleanup):**
- `machine_status`: 7 days
- `machine_light`: 90 days
- `machine_heavy`: 90 days
- `alarm_events`: 365 days

The schema is initialized from `db/init.sql` on first container creation only. If you change the schema, you must either:
- Drop the volume (`docker compose down -v`) and recreate, OR
- Run migrations manually via `psql`

### Grafana

Pre-configured with:
- TimescaleDB datasource (auto-provisioned via `timescaledb.yml`)
- CNC Overview dashboard (auto-provisioned via `cnc-overview.json`)

**Dashboard layout:**
- **Top (full width):** Big bold RUNNING / NOT RUNNING status indicator
- **Row 2:** Spindle speed and feed rate time-series graphs
- **Row 3:** Program number, program name, tool number, executing line (stat panels)
- **Row 4:** Axis positions — absolute, relative, machine (tables)
- **Row 5:** Active alarms table
- **Row 6:** Uptime gauge (% running in time window), macros, diagnostics
- **Bottom:** Controller datetime, system info

**Default credentials:** admin / admin

### Slack Notifications

Uses a Slack Incoming Webhook (no bot token needed). Sends a message:
- On collector startup: "Collector started — CNC Machine is RUNNING/NOT RUNNING"
- On state change: "CNC Machine is now RUNNING" or "CNC Machine has STOPPED"

The webhook URL is configured via the `SLACK_WEBHOOK` environment variable. If unset, Slack notifications are disabled.

To get a new webhook URL: Slack API ([api.slack.com/apps](https://api.slack.com/apps)) → Create App → Incoming Webhooks → Add to Workspace → Copy URL.

## CNC Machine Requirements

| Property | Value |
|---|---|
| Controller | Fanuc 30i / 31i / 32i (or compatible) |
| Protocol | FOCAS over TCP |
| Default Port | 8193 |
| Tool number macro | #4120 (standard, may vary by machine) |

The controller must have Embedded Ethernet configured with FOCAS enabled on port 8193. Set the controller's IP via the `CNC_HOST` environment variable.

## Database Schema

### `machine_status` (every 1s)
| Column | Type | Description |
|---|---|---|
| time | TIMESTAMPTZ | Timestamp |
| running | BOOLEAN | True if spindle or feed is non-zero |
| spindle_speed | DOUBLE PRECISION | RPM |
| feed_rate | DOUBLE PRECISION | mm/min (or in/min depending on controller config) |

### `machine_light` (every 10s)
| Column | Type | Description |
|---|---|---|
| time | TIMESTAMPTZ | Timestamp |
| spindle_speed | DOUBLE PRECISION | RPM |
| feed_rate | DOUBLE PRECISION | Feed rate |
| program_number | TEXT | JSON: {"run": N, "main": N} |
| program_name | TEXT | Program name with path |
| executing_line | TEXT | JSON: {"block": N, "text": "..."} |
| tool_number | DOUBLE PRECISION | Current tool number from macro #4120 |
| axes_absolute | JSONB | Absolute axis positions |
| axes_relative | JSONB | Relative axis positions |
| axes_machine | JSONB | Machine axis positions |
| alarms | JSONB | Active alarm codes and text |
| controller_datetime | TEXT | Controller's internal date/time |

### `machine_heavy` (every 5 min)
| Column | Type | Description |
|---|---|---|
| time | TIMESTAMPTZ | Timestamp |
| macros | JSONB | Macro variables 100–199 |
| pmc | JSONB | PMC/ladder data |
| parameters | JSONB | Controller parameters 1–50 |
| diagnostics | JSONB | Diagnostic values 1–50 |
| system_info | JSONB | CNC type, axes, version |

### `alarm_events` (extracted from light polls)
| Column | Type | Description |
|---|---|---|
| time | TIMESTAMPTZ | Timestamp |
| alarm_code | TEXT | Alarm code |
| alarm_text | TEXT | Alarm message text |

## Environment Variables

All configuration can be overridden via environment variables. Defaults are suitable for the Docker Compose setup.

| Variable | Default | Description |
|---|---|---|
| `CNC_HOST` | `192.168.1.100` | CNC controller IP address |
| `CNC_PORT` | `8193` | FOCAS TCP port |
| `DB_HOST` | `timescaledb` | TimescaleDB hostname (Docker service name) |
| `DB_PORT` | `5432` | TimescaleDB port (internal Docker port) |
| `DB_NAME` | `factory` | Database name |
| `DB_USER` | `factory` | Database user |
| `DB_PASS` | `factory` | Database password |
| `SLACK_WEBHOOK` | *(empty)* | Slack Incoming Webhook URL (notifications disabled if unset) |

To override in Docker Compose, add an `environment` block to the `collector` service in `docker-compose.yml`.

## Getting Started

### Prerequisites

- **Docker** and **Docker Compose** (v2)
- Network access from the Docker host to `YOUR_CNC_IP:8193` (the CNC controller)
- Internet access for pulling Docker images on first run

### First-Time Setup

```bash
git clone <this-repo>
cd factory_controller
docker compose build
```

### Testing the CNC Connection

Before running the full stack, verify you can reach the controller. This does NOT start TimescaleDB or Grafana — it just connects, reads three values, and exits:

```bash
docker compose run --rm --no-deps collector python -u collector.py --test
```

Expected output:
```
Connecting to Fanuc controller at YOUR_CNC_IP:8193 ...
Connected. System info: {"addinfo": ..., "cnctype": "31", ...}

Spindle speed: 0.0
Feed rate:     0.0
Running:       False
```

If it hangs or errors, the machine is unreachable or FOCAS is not enabled on the controller.

### Running the Full Stack

```bash
docker compose up -d
```

This starts three containers:
1. `factory_timescaledb` — database (port 5433 on host, 5432 internally)
2. `factory_grafana` — dashboards (port 3001 on host)
3. `factory_collector` — data collection

**Note:** Host ports 5433 and 3001 are used to avoid conflicts with other services. When deploying to a dedicated VM, change these back to 5432 and 3000 in `docker-compose.yml`.

### Verifying Data Collection

Check collector logs:
```bash
docker compose logs -f collector
```

Query the database:
```bash
docker exec factory_timescaledb psql -U factory -c "SELECT count(*) FROM machine_status;"
```

Run the above twice a few seconds apart — the count should increase.

Open Grafana: **http://localhost:3001** (admin / admin)

## Development Guide

### Making Changes to the Collector

1. Edit `collector.py`
2. Rebuild and restart:
   ```bash
   docker compose build collector
   docker compose up -d collector
   ```
3. Check logs: `docker compose logs -f collector`

### Modifying the Database Schema

The schema in `db/init.sql` only runs on **first container creation** (when the Docker volume is empty). To apply schema changes:

**Option A — Nuke and recreate (loses all data):**
```bash
docker compose down -v
docker compose up -d
```

**Option B — Manual migration (preserves data):**
```bash
docker exec -it factory_timescaledb psql -U factory
# Run your ALTER TABLE / CREATE TABLE statements
```

### Editing the Grafana Dashboard

Two approaches:

**Via the Grafana UI (quick iteration):**
1. Open http://localhost:3001, edit the dashboard
2. Save it (this saves to Grafana's internal storage)
3. To persist: export the dashboard JSON and overwrite `grafana/provisioning/dashboards/cnc-overview.json`

**Via the JSON file directly:**
1. Edit `grafana/provisioning/dashboards/cnc-overview.json`
2. Restart Grafana: `docker compose restart grafana`

### Changing Poll Intervals

Edit the constants at the top of `collector.py`:

```python
STATUS_INTERVAL = 1.0       # Machine running check (seconds)
LIGHT_POLL_INTERVAL = 10.0  # Positions, program, alarms (seconds)
HEAVY_POLL_INTERVAL = 300.0 # Macros, params, diagnostics (seconds)
HEARTBEAT_INTERVAL = 60.0   # Connection health check (seconds)
```

**Important — FOCAS polling limits:**
- Lightweight reads (spindle speed, feed rate, status) are safe at 200ms+
- Heavy reads (macro ranges, parameter ranges, diagnostics) should stay at 30s+ to avoid `EW_BUSY` errors from the controller
- Fanuc document TMN21-198E warns that aggressive polling of "low-speed" FOCAS functions can cause response times exceeding 1 second
- The CNC's real-time machining loop (servo, interpolation) is NOT affected by FOCAS polling, but the PMC ladder and other FOCAS consumers sharing the controller can be

### Adding a New Data Point

Example: adding spindle load to the light poll.

1. **Add the read to `collect_light()` in `collector.py`:**
   ```python
   try:
       data["spindle_load"] = conn.readpmc(1, 9, 2204, 1)  # example PMC address
   except Exception:
       data["spindle_load"] = None
   ```

2. **Add the column to the database.** Either alter the table manually or add it to `db/init.sql` and recreate the volume:
   ```sql
   ALTER TABLE machine_light ADD COLUMN spindle_load DOUBLE PRECISION;
   ```

3. **Update `write_light()` to include the new column** in the INSERT statement.

4. **Add a panel to the Grafana dashboard** — easiest via the Grafana UI, then export the JSON.

5. Rebuild: `docker compose build collector && docker compose up -d collector`

## Deploying to a VM

1. Install Docker and Docker Compose on the VM
2. Clone this repo to the VM
3. Update `docker-compose.yml`:
   - Change port `5433:5432` back to `5432:5432`
   - Change port `3001:3000` back to `3000:3000`
   - Change default DB password in both `docker-compose.yml` and the `DB_PASS` env var
4. Ensure the VM can reach `YOUR_CNC_IP:8193` (the CNC controller)
5. Ensure the VM can reach `hooks.slack.com` (for Slack notifications)
6. Run:
   ```bash
   docker compose up -d
   ```

## Troubleshooting

| Problem | Diagnosis | Fix |
|---|---|---|
| Collector can't connect to CNC | `docker compose run --rm --no-deps collector python -u collector.py --test` | Check network routing to YOUR_CNC_IP:8193. Verify FOCAS is enabled on the controller's Embedded Ethernet settings. |
| Port conflict on startup | `docker ps \| grep <port>` | Another container is using the port. Change the host port mapping in `docker-compose.yml` (left side of the colon). |
| Schema not applied | `psql` shows "relation does not exist" | `init.sql` only runs on first volume creation. Run `docker compose down -v && docker compose up -d` to reset. |
| Grafana shows "no default database" | Datasource config issue | Ensure `database: factory` is inside `jsonData` in `grafana/provisioning/datasources/timescaledb.yml`. Restart Grafana. |
| `[db] write error` in collector logs | DB connection or schema mismatch | Check that TimescaleDB is healthy (`docker compose ps`). Verify schema matches collector's INSERT statements. |
| `[heartbeat] FAIL` in logs | CNC connection dropped | Collector will keep retrying. If persistent, check network and controller. |
| Slack notifications not sending | `[slack] error` in logs | Verify the webhook URL is valid. Test with: `curl -X POST -H 'Content-Type: application/json' -d '{"text":"test"}' <webhook-url>` |
| `pyfanuc` import error | `pyfanuc.py` missing from container | Ensure `pyfanuc.py` exists in the project root and `COPY pyfanuc.py .` is in the Dockerfile. |

## Key Decisions & Gotchas

1. **pyfanuc is vendored, not pip-installed.** The PyPI package is broken (depends on nonexistent `fwlipy`). The GitHub repo has no `setup.py`. It's one file with zero dependencies — vendoring is the simplest path.

2. **Three-tier polling is deliberate.** Fanuc controllers can return `EW_BUSY` if FOCAS is called too aggressively. Lightweight reads (spindle/feed) are safe every 1s. Heavy reads (macro ranges, parameter ranges) are kept to every 5 minutes per Fanuc's own guidance (TMN21-198E).

3. **"Running" is derived from spindle speed + feed rate.** If either is non-zero, the machine is considered running. This is a heuristic — it won't detect a machine that is powered on but idle in MDI mode with no spindle or feed active. A more precise check would use `statinfo()` to read the `run` field, but this was simpler to start with. Consider switching to `statinfo()` if the heuristic proves unreliable.

4. **`init.sql` only runs once.** TimescaleDB mounts it into `/docker-entrypoint-initdb.d/`, which PostgreSQL only executes when the data volume is brand new. Schema migrations must be done manually or by wiping the volume.

5. **Slack webhook URL must be set via env var.** If `SLACK_WEBHOOK` is empty, Slack notifications are silently disabled. Never commit webhook URLs to source control.

6. **Host ports are non-standard (5433, 3001).** This avoids conflicts with other Docker services on the dev machine. Change them back to 5432/3000 when deploying to a dedicated VM.

7. **Tool number comes from macro #4120.** This is the standard Fanuc macro for current tool number, but it can vary by machine configuration. If it returns null, check your controller's macro assignments.

8. **The `readpmc()` call in `collect_heavy()` requires specific arguments** (datatype, section, address, count) that are machine-specific. The current call may fail if the PMC addresses don't exist on your controller. Adjust the parameters based on your machine's ladder program.
