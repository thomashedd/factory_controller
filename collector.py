#!/usr/bin/env python3
"""Collects CNC data from a Fanuc 31i controller via FOCAS protocol
and writes to TimescaleDB."""

import time
import json
import sys
import os
import threading
import urllib.request
import psycopg2
import pyfanuc

# -- Configuration --
CNC_HOST = os.environ.get("CNC_HOST", "192.168.1.100")
CNC_PORT = int(os.environ.get("CNC_PORT", "8193"))
STATUS_INTERVAL = 1.0  # seconds between machine status reads
LIGHT_POLL_INTERVAL = 10.0  # seconds between lightweight data reads
HEAVY_POLL_INTERVAL = 300.0  # seconds (5 min) between heavy data reads
HEARTBEAT_INTERVAL = 60.0  # seconds between heartbeat checks
STATE_DEBOUNCE_COUNT = 60  # consecutive reads before confirming state change

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK", "")

DB_HOST = os.environ.get("DB_HOST", "timescaledb")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "factory")
DB_USER = os.environ.get("DB_USER", "factory")
DB_PASS = os.environ.get("DB_PASS", "factory")


def notify_slack(message):
    """Send a message to Slack via incoming webhook."""
    if not SLACK_WEBHOOK:
        return
    payload = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK, data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        print(f"[slack] sent: {message}")
    except Exception as e:
        print(f"[slack] error: {e}", file=sys.stderr)


def get_db():
    """Connect to TimescaleDB."""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )
    conn.autocommit = True
    return conn


def write_status(db, data):
    """Write status data to machine_status table."""
    cur = db.cursor()
    cur.execute(
        "INSERT INTO machine_status (time, running, spindle_speed, feed_rate) "
        "VALUES (NOW(), %s, %s, %s)",
        (data["running"], data["spindle_speed"], data["feed_rate"])
    )
    cur.close()


def write_light(db, data):
    """Write light poll data to machine_light table."""
    prog = data.get("program_number")
    prog_str = json.dumps(prog, default=str) if prog else None

    exec_line = data.get("executing_line")
    exec_str = json.dumps(exec_line, default=str) if exec_line else None

    tool = data.get("tool_number")
    # tool comes back as dict like {4120: value}, extract the value
    tool_val = None
    if isinstance(tool, dict):
        tool_val = next(iter(tool.values()), None)
    elif tool is not None:
        tool_val = tool

    cur = db.cursor()
    cur.execute(
        "INSERT INTO machine_light "
        "(time, spindle_speed, feed_rate, program_number, program_name, "
        "executing_line, tool_number, axes_absolute, axes_relative, "
        "axes_machine, alarms, controller_datetime) "
        "VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            data.get("spindle_speed"),
            data.get("feed_rate"),
            prog_str,
            data.get("program_name"),
            exec_str,
            tool_val,
            json.dumps(data.get("axes_absolute"), default=str),
            json.dumps(data.get("axes_relative"), default=str),
            json.dumps(data.get("axes_machine"), default=str),
            json.dumps(data.get("alarms"), default=str),
            str(data.get("controller_datetime")) if data.get("controller_datetime") else None,
        )
    )
    cur.close()

    # Write alarm events if any
    alarms = data.get("alarms")
    if alarms:
        cur = db.cursor()
        for alarm in alarms:
            cur.execute(
                "INSERT INTO alarm_events (time, alarm_code, alarm_text) "
                "VALUES (NOW(), %s, %s)",
                (str(alarm.get("alarmcode")), str(alarm.get("text", "")))
            )
        cur.close()


def write_heavy(db, data):
    """Write heavy poll data to machine_heavy table."""
    cur = db.cursor()
    cur.execute(
        "INSERT INTO machine_heavy "
        "(time, macros, pmc, parameters, diagnostics, system_info) "
        "VALUES (NOW(), %s, %s, %s, %s, %s)",
        (
            json.dumps(data.get("macros"), default=str),
            json.dumps(data.get("pmc"), default=str),
            json.dumps(data.get("parameters"), default=str),
            json.dumps(data.get("diagnostics"), default=str),
            json.dumps(data.get("system_info"), default=str),
        )
    )
    cur.close()


def check_connection(conn, host):
    """Verify initial connection to the controller and print system info."""
    print(f"Connecting to Fanuc controller at {host}:{CNC_PORT} ...")
    if not conn.connect():
        print("ERROR: could not connect to controller.", file=sys.stderr)
        return False

    try:
        info = conn.getsysinfo()
        print(f"Connected. System info: {json.dumps(info, default=str)}")
    except Exception as e:
        print(f"Connected, but could not read system info: {e}")

    return True


def heartbeat(conn, stop_event):
    """Periodically verify the connection is alive by reading system info."""
    while not stop_event.is_set():
        stop_event.wait(HEARTBEAT_INTERVAL)
        if stop_event.is_set():
            break
        try:
            conn.getsysinfo()
            print(f"[heartbeat] OK  @ {time.strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"[heartbeat] FAIL @ {time.strftime('%H:%M:%S')}: {e}",
                  file=sys.stderr)


def collect_status(conn):
    """Fast poll: just machine running status (every 1s)."""
    data = {"timestamp": time.time(), "type": "status"}

    try:
        data["spindle_speed"] = conn.readactspindlespeed()
    except Exception:
        data["spindle_speed"] = None

    try:
        data["feed_rate"] = conn.readactfeed()
    except Exception:
        data["feed_rate"] = None

    # A machine is "running" if spindle or feed is non-zero
    running = False
    if data["spindle_speed"] and data["spindle_speed"] != 0:
        running = True
    if data["feed_rate"] and data["feed_rate"] != 0:
        running = True
    data["running"] = running

    return data


def collect_light(conn):
    """Lightweight poll: positions, program, alarms, tool (every 10s)."""
    data = {"timestamp": time.time(), "type": "light"}

    try:
        data["spindle_speed"] = conn.readactspindlespeed()
    except Exception:
        data["spindle_speed"] = None

    try:
        data["feed_rate"] = conn.readactfeed()
    except Exception:
        data["feed_rate"] = None

    try:
        data["program_number"] = conn.readprognum()
    except Exception:
        data["program_number"] = None

    try:
        data["program_name"] = conn.readprogname()
    except Exception:
        data["program_name"] = None

    try:
        data["executing_line"] = conn.readexecprog()
    except Exception:
        data["executing_line"] = None

    try:
        data["axes_absolute"] = conn.readaxes(what=1)
    except Exception:
        data["axes_absolute"] = None

    try:
        data["axes_relative"] = conn.readaxes(what=2)
    except Exception:
        data["axes_relative"] = None

    try:
        data["axes_machine"] = conn.readaxes(what=4)
    except Exception:
        data["axes_machine"] = None

    try:
        data["tool_number"] = conn.readmacro(4120)
    except Exception:
        data["tool_number"] = None

    try:
        data["alarms"] = conn.readalarmcode(type=0, withtext=1)
    except Exception:
        data["alarms"] = None

    try:
        data["controller_datetime"] = conn.getdatetime()
    except Exception:
        data["controller_datetime"] = None

    return data


def collect_heavy(conn):
    """Heavy poll: macro ranges, parameters, diagnostics, system info (every 5 min)."""
    data = {"timestamp": time.time(), "type": "heavy"}

    try:
        data["macros"] = conn.readmacro(100, 199)
    except Exception:
        data["macros"] = None

    try:
        data["pmc"] = conn.readpmc()
    except Exception:
        data["pmc"] = None

    try:
        data["parameters"] = conn.readparam(axis=0, first=1, last=50)
    except Exception:
        data["parameters"] = None

    try:
        data["diagnostics"] = conn.readdiag(axis=0, first=1, last=50)
    except Exception:
        data["diagnostics"] = None

    try:
        data["system_info"] = conn.getsysinfo()
    except Exception:
        data["system_info"] = None

    return data


def test_connection(host):
    """Connect, check if machine is running, print result, and exit."""
    conn = pyfanuc.pyfanuc(host)

    if not check_connection(conn, host):
        sys.exit(1)

    status = collect_status(conn)
    print(f"\nSpindle speed: {status['spindle_speed']}")
    print(f"Feed rate:     {status['feed_rate']}")
    print(f"Running:       {status['running']}")

    conn.disconnect()


def run(host):
    """Main polling loop with three collection tiers, writing to TimescaleDB."""
    cnc = pyfanuc.pyfanuc(host)

    if not check_connection(cnc, host):
        sys.exit(1)

    print("Connecting to TimescaleDB ...")
    db = get_db()
    print("TimescaleDB connected.")

    # Start heartbeat thread
    stop_event = threading.Event()
    hb_thread = threading.Thread(target=heartbeat, args=(cnc, stop_event),
                                 daemon=True)
    hb_thread.start()

    print(f"Polling: status={STATUS_INTERVAL}s, "
          f"light={LIGHT_POLL_INTERVAL}s, "
          f"heavy={HEAVY_POLL_INTERVAL}s (Ctrl+C to stop)...\n")

    # Send initial state to Slack on startup
    initial = collect_status(cnc)
    if initial["running"]:
        notify_slack("Collector started — CNC Machine is *RUNNING*")
    else:
        notify_slack("Collector started — CNC Machine is *NOT RUNNING*")

    try:
        last_light_poll = 0.0
        last_heavy_poll = 0.0
        confirmed_running = initial["running"]
        pending_state = initial["running"]
        pending_count = 0
        while True:
            now = time.time()

            # Heavy data collection every 5 min
            if now - last_heavy_poll >= HEAVY_POLL_INTERVAL:
                snapshot = collect_heavy(cnc)
                last_heavy_poll = now
                try:
                    write_heavy(db, snapshot)
                except Exception as e:
                    print(f"[db] heavy write error: {e}", file=sys.stderr)
                print(f"[heavy] collected @ {time.strftime('%H:%M:%S')}")

            # Lightweight data collection every 10s
            # (light poll already includes spindle_speed and feed_rate)
            if now - last_light_poll >= LIGHT_POLL_INTERVAL:
                snapshot = collect_light(cnc)
                last_light_poll = now
                try:
                    write_light(db, snapshot)
                except Exception as e:
                    print(f"[db] light write error: {e}", file=sys.stderr)
                print(f"[light] collected @ {time.strftime('%H:%M:%S')}")
                # Derive running flag from light poll data
                running = bool(
                    (snapshot.get("spindle_speed") and snapshot["spindle_speed"] != 0)
                    or (snapshot.get("feed_rate") and snapshot["feed_rate"] != 0)
                )
            else:
                # Status-only every 1s
                status = collect_status(cnc)
                try:
                    write_status(db, status)
                except Exception as e:
                    print(f"[db] status write error: {e}", file=sys.stderr)
                running = status["running"]

            # Debounced state change detection
            if running != confirmed_running:
                if running == pending_state:
                    pending_count += 1
                else:
                    pending_state = running
                    pending_count = 1
                if pending_count >= STATE_DEBOUNCE_COUNT:
                    confirmed_running = running
                    pending_count = 0
                    if running:
                        notify_slack("CNC Machine is now *RUNNING*")
                    else:
                        notify_slack("CNC Machine has *STOPPED*")
            else:
                pending_count = 0

            time.sleep(STATUS_INTERVAL)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop_event.set()
        hb_thread.join(timeout=5)
        cnc.disconnect()
        db.close()


def main():
    host = CNC_HOST
    args = sys.argv[1:]

    test_mode = "--test" in args
    if test_mode:
        args.remove("--test")

    if args:
        host = args[0]

    if test_mode:
        test_connection(host)
    else:
        run(host)


if __name__ == "__main__":
    main()
