-- TimescaleDB schema for Fanuc 31i CNC data collection

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Fast-poll status data (every 1s)
CREATE TABLE machine_status (
    time        TIMESTAMPTZ NOT NULL,
    running     BOOLEAN,
    spindle_speed DOUBLE PRECISION,
    feed_rate   DOUBLE PRECISION
);

SELECT create_hypertable('machine_status', 'time');

-- Light-poll data (every 10s)
CREATE TABLE machine_light (
    time             TIMESTAMPTZ NOT NULL,
    spindle_speed    DOUBLE PRECISION,
    feed_rate        DOUBLE PRECISION,
    program_number   TEXT,
    program_name     TEXT,
    executing_line   TEXT,
    tool_number      DOUBLE PRECISION,
    axes_absolute    JSONB,
    axes_relative    JSONB,
    axes_machine     JSONB,
    alarms           JSONB,
    controller_datetime TEXT
);

SELECT create_hypertable('machine_light', 'time');

-- Heavy-poll data (every 5 min)
CREATE TABLE machine_heavy (
    time         TIMESTAMPTZ NOT NULL,
    macros       JSONB,
    pmc          JSONB,
    parameters   JSONB,
    diagnostics  JSONB,
    system_info  JSONB
);

SELECT create_hypertable('machine_heavy', 'time');

-- Alarm events (extracted from snapshots for easier querying)
CREATE TABLE alarm_events (
    time       TIMESTAMPTZ NOT NULL,
    alarm_code TEXT,
    alarm_text TEXT
);

SELECT create_hypertable('alarm_events', 'time');

-- Retention policy: keep status data for 7 days, snapshots for 90 days
SELECT add_retention_policy('machine_status', INTERVAL '7 days');
SELECT add_retention_policy('machine_light', INTERVAL '90 days');
SELECT add_retention_policy('machine_heavy', INTERVAL '90 days');
SELECT add_retention_policy('alarm_events', INTERVAL '365 days');
