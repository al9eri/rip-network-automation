"""
capture_rip_debug.py - record 'debug ip rip' + 'debug ip routing' from one router.

Turns the debugs on, records for a fixed time (60 s by default, use about
330 s for T5), then ALWAYS turns them off again with 'undebug all', even
after an error or Ctrl+C. Saves the capture where parse_rip_debug.py
expects it:

    data/debug_logs/<test>-run<n>_<router>.txt        the capture (parse this)
    data/debug_logs/<test>-run<n>_<router>.live.txt   live terminal-monitor copy (mode 'both'/'live')
    data/debug_logs/<test>-run<n>_<router>.meta.json  times, router clock offset, warnings

    python scripts/capture_rip_debug.py LIB-R1 --test T1 --run 1
    python scripts/capture_rip_debug.py LIB-R1 --test T5 --run 2 --duration 330
    python scripts/capture_rip_debug.py TCH-R2 --label practice --duration 40 --mode live

How it records (the plan's "Handoff from Mouhamed" suggests the buffer):
  buffer  'clear logging', debugs on, wait, then read 'show logging' back.
          The router keeps the debug lines in its 64 KB log buffer
          (configs/push/timestamps.txt), so nothing is lost if the SSH
          session drops in the middle (T2, T3).
  live    'terminal monitor' and read the lines as they arrive. Lost while
          the session is down.
  both    (default) does both: the buffer copy is the main capture, the live
          copy is a backup in case the buffer wrapped or was not set up.

Debugging is global on IOS: it keeps running after the SSH session drops.
That is why the cleanup reconnects (for up to --reconnect-timeout seconds)
just to send 'undebug all'. If even that fails it prints the exact
commands to type on the console.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEBUG_DIR = DATA_DIR / "debug_logs"

DEBUG_COMMANDS = ("debug ip rip", "debug ip routing")
LOG_BUFFER_BYTES = 64000  # from configs/push/timestamps.txt

# 'show clock' -> '*10:15:02.123 UTC Wed Oct 21 2026' ('*' = clock never set,
# '.' = not synchronised). The timezone is whatever the router is set to.
CLOCK_RE = re.compile(
    r"[*.]?(?P<time>\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s+\S+\s+\w{3}\s+"
    r"(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})"
)


class CaptureError(Exception):
    """The router refused a command (e.g. '% Invalid input')."""


def _default_connect(name):
    # Imported here so the parsing helpers and the tests work without netmiko.
    from inventory import connect
    return connect(name)


def capture_name(router, label):
    """File stem used by the plan: 'T1-run1_LIB-R1'."""
    return f"{label}_{router}"


def _rejected(output):
    """IOS prints '% Invalid input...' etc. for a bad command but carries on."""
    return [line.strip() for line in output.splitlines() if line.strip().startswith("% ")]


def send_timing(conn, command, read_timeout=15):
    """Send a command that may not return a normal prompt quickly.

    'clear logging' asks '[confirm]'; with terminal monitor on, debug lines can
    land between the echo and the prompt, so the echo check is turned off.
    """
    output = conn.send_command_timing(command, read_timeout=read_timeout, cmd_verify=False)
    if "confirm" in output.lower():
        output += conn.send_command_timing("\n", read_timeout=read_timeout, cmd_verify=False)
    return output


def parse_show_clock(text, year=None):
    """'*10:15:02.123 UTC Wed Oct 21 2026' -> datetime (no timezone), or None.

    `year` replaces the router's year. Debug timestamps carry no year and
    parse_rip_debug.py assumes the current one, so the offset must too
    (an unset router clock says 1993 or 2006).
    """
    m = CLOCK_RE.search(text or "")
    if not m:
        return None
    clock = m["time"] if "." in m["time"] else m["time"] + ".0"
    try:
        dt = datetime.strptime(f"{m['year']} {m['mon']} {m['day']} {clock}", "%Y %b %d %H:%M:%S.%f")
    except ValueError:
        return None
    return dt.replace(year=year) if year else dt


def router_clock(conn, now=datetime.now):
    """Read the router clock and work out router-minus-laptop offset in seconds.

    The offset converts a time in a router log to laptop time:
        laptop_time = router_time - offset
    Accuracy is about half the command round trip ('uncertainty_s').
    """
    sent = now()
    text = conn.send_command("show clock", read_timeout=15)
    received = now()
    middle = sent + (received - sent) / 2
    router_dt = parse_show_clock(text, year=middle.year)
    return {
        "laptop_sent": sent.isoformat(timespec="milliseconds"),
        "laptop_received": received.isoformat(timespec="milliseconds"),
        "router_text": (text or "").strip(),
        "router_time": router_dt.isoformat(timespec="milliseconds") if router_dt else None,
        "offset_s": round((router_dt - middle).total_seconds(), 3) if router_dt else None,
        "uncertainty_s": round((received - sent).total_seconds() / 2, 3),
        "clock_set": not (text or "").strip().startswith("*"),
    }


def check_router_setup(conn):
    """Return warnings if the router is not set up the way the tests need."""
    warnings = []
    try:
        logging = conn.send_command("show logging | include Buffer logging", read_timeout=15)
        if "level debugging" not in logging:
            warnings.append("buffer logging is not at level debugging: push configs/push/timestamps.txt "
                            "(python scripts/push_config.py configs/push/timestamps.txt all)")
        stamps = conn.send_command("show running-config | include service timestamps", read_timeout=30)
        if "debug datetime msec" not in stamps:
            warnings.append("'service timestamps debug datetime msec' missing: debug lines will have no "
                            "usable times (push configs/push/timestamps.txt)")
    except Exception as exc:  # a failed check must not stop the capture
        warnings.append(f"could not check router setup ({exc.__class__.__name__})")
    return warnings


def start_debug(conn, clear=True, monitor=False):
    """Clear the log buffer (optional), terminal monitor (optional), debugs on."""
    output = ""
    if clear:
        output += send_timing(conn, "clear logging")
    if monitor:
        output += send_timing(conn, "terminal monitor")
    for command in DEBUG_COMMANDS:
        reply = send_timing(conn, command)
        output += reply
        bad = _rejected(reply)
        if bad:
            raise CaptureError(f"{command!r} rejected: {bad[0]}")
    return output


def stop_debug(conn):
    """'undebug all' and 'terminal no monitor'. Returns the router's reply."""
    output = send_timing(conn, "undebug all")
    try:
        output += send_timing(conn, "terminal no monitor")
    except Exception:
        pass  # undebug all is what matters
    return output


def read_buffer(conn):
    """Return 'show logging' (header + buffered debug lines)."""
    return conn.send_command("show logging", read_timeout=90)


def buffer_warnings(text):
    warnings = []
    if len(text.encode(errors="replace")) >= LOG_BUFFER_BYTES - 2000:
        warnings.append("log buffer is (nearly) full: the oldest lines may have been overwritten; "
                        "check the .live.txt copy")
    if "RIP:" not in text:
        warnings.append("no 'RIP:' lines in the buffer: were the debugs on and buffer logging at debugging level?")
    if "RT:" not in text:
        warnings.append("no 'RT:' lines (debug ip routing): parse_rip_debug.py cannot measure convergence "
                        "without them (fine if no route changed)")
    return warnings


def _close(conn):
    if conn is None:
        return
    try:
        conn.disconnect()
    except Exception:
        pass


def _alive(conn):
    if conn is None:
        return False
    try:
        return bool(conn.is_alive())
    except Exception:
        return False


def reconnect(router, connect_fn, timeout, sleep=time.sleep, clock=time.monotonic, log=print):
    """Try to log in again until `timeout` seconds pass. Returns a session or None."""
    deadline = clock() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            return connect_fn(router)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if clock() >= deadline:
                log(f"  reconnect to {router} failed after {attempt} attempts ({exc.__class__.__name__})")
                return None
            log(f"  {router} not reachable yet ({exc.__class__.__name__}), retrying...")
            sleep(3)


def live_record(conn, out, duration, clock=time.monotonic, sleep=time.sleep, poll=0.5, now=datetime.now):
    """Copy everything the router prints to `out` for `duration` seconds.

    Returns None, or {time, error} if the session died part way.
    """
    end = clock() + duration
    while clock() < end:
        try:
            chunk = conn.read_channel()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return {"time": now().isoformat(timespec="milliseconds"), "error": f"{exc.__class__.__name__}: {exc}"}
        if chunk:
            out.write(chunk.replace("\r\n", "\n"))
            out.flush()
        sleep(poll)
    return None


def wait(duration, clock=time.monotonic, sleep=time.sleep, log=print, step=10):
    """Sleep for `duration` s, printing a countdown every `step` s."""
    end = clock() + duration
    while (left := end - clock()) > 0:
        if left >= step:
            log(f"  recording... {left:.0f} s left")
        sleep(min(step, left))


def capture(router, label, duration=60, mode="both", clear=True, out_dir=DEBUG_DIR,
            connect_fn=_default_connect, reconnect_timeout=120, check_setup=True,
            clock=time.monotonic, sleep=time.sleep, now=datetime.now, log=print):
    """Run one timed capture. Returns the metadata dict (also saved as .meta.json).

    meta['ok'] is True when the capture was saved and debugging was turned off.
    """
    if mode not in ("buffer", "live", "both"):
        raise ValueError("mode must be 'buffer', 'live' or 'both'")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = capture_name(router, label)
    main_path = out_dir / f"{stem}.txt"
    live_path = out_dir / f"{stem}.live.txt"
    meta = {
        "router": router, "label": label, "mode": mode, "duration_s": duration,
        "capture_file": str(main_path), "laptop_start": None, "laptop_end": None,
        "clock_start": None, "clock_end": None, "debug_enabled": False, "undebug_ok": False,
        "session_drop": None, "interrupted": False, "warnings": [], "error": None, "ok": False,
    }

    conn = None
    buffer_text = None
    try:
        log(f"Connecting to {router}...")
        conn = connect_fn(router)
        if check_setup:
            meta["warnings"] += check_router_setup(conn)
        meta["clock_start"] = router_clock(conn, now)
        meta["laptop_start"] = now().isoformat(timespec="milliseconds")
        meta["debug_enabled"] = True  # set first: a half-applied debug must still be undone
        start_debug(conn, clear=clear, monitor=mode in ("live", "both"))
        log(f"Debugs on ({', '.join(DEBUG_COMMANDS)}). Recording {duration} s from {router}.")

        if mode == "buffer":
            wait(duration, clock, sleep, log)
        else:
            with live_path.open("w", encoding="utf-8") as live:
                meta["session_drop"] = live_record(conn, live, duration, clock, sleep, now=now)
            if meta["session_drop"]:
                log(f"  session dropped at {meta['session_drop']['time']} "
                    f"({meta['session_drop']['error']}); the router keeps logging to its buffer")
                wait(max(0.0, duration - _elapsed(meta, now)), clock, sleep, log)
    except KeyboardInterrupt:
        meta["interrupted"] = True
        log("Interrupted: stopping early and cleaning up.")
    except Exception as exc:
        meta["error"] = f"{exc.__class__.__name__}: {exc}".splitlines()[0]
        log(f"ERROR: {meta['error']}")
    finally:
        meta["laptop_end"] = now().isoformat(timespec="milliseconds")
        if meta["debug_enabled"]:
            if not _alive(conn):
                _close(conn)
                log(f"Session to {router} is gone; reconnecting to turn debugging off...")
                conn = reconnect(router, connect_fn, reconnect_timeout, sleep, clock, log)
            if conn is not None:
                try:
                    stop_debug(conn)
                    meta["undebug_ok"] = True
                    log("undebug all sent.")
                    if mode in ("buffer", "both"):
                        buffer_text = read_buffer(conn)
                    meta["clock_end"] = router_clock(conn, now)
                except Exception as exc:
                    meta["error"] = meta["error"] or f"cleanup: {exc.__class__.__name__}: {exc}".splitlines()[0]
                    if not meta["undebug_ok"]:
                        # One more try on a fresh session.
                        _close(conn)
                        conn = reconnect(router, connect_fn, 15, sleep, clock, log)
                        if conn is not None:
                            try:
                                stop_debug(conn)
                                meta["undebug_ok"] = True
                            except Exception:
                                pass
        _close(conn)

    # Save whatever we have.
    if buffer_text is not None:
        main_path.write_text(buffer_text.replace("\r\n", "\n"), encoding="utf-8")
        meta["warnings"] += buffer_warnings(buffer_text)
    elif mode == "live" and live_path.exists():
        live_path.replace(main_path)
        meta["capture_file"] = str(main_path)
    elif live_path.exists() and live_path.stat().st_size:
        meta["warnings"].append("buffer read-back failed; only the live copy was saved")
        meta["capture_file"] = str(live_path)
    else:
        meta["capture_file"] = None

    if meta["debug_enabled"] and not meta["undebug_ok"]:
        meta["warnings"].append(f"DEBUGGING MAY STILL BE ON on {router}: on its console type "
                                "'undebug all' then 'terminal no monitor'")
    meta["ok"] = bool(meta["capture_file"]) and (meta["undebug_ok"] or not meta["debug_enabled"]) \
        and meta["error"] is None
    (out_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    for w in meta["warnings"]:
        log(f"WARNING: {w}")
    if meta["capture_file"]:
        log(f"Saved {meta['capture_file']}")
    return meta


def _elapsed(meta, now):
    start = datetime.fromisoformat(meta["laptop_start"])
    return (now() - start).total_seconds()


def main(argv=None):
    from experiments import get_experiment, run_label

    parser = argparse.ArgumentParser(description="Record debug ip rip + debug ip routing from one router.")
    parser.add_argument("router", nargs="?", help="LIB-R1, TCH-R2 or LAB-R3 (default: the test's router)")
    parser.add_argument("--test", help="T0..T5: sets the router and the default duration")
    parser.add_argument("--run", type=int, default=1, help="run number, used in the file name (default 1)")
    parser.add_argument("--label", help="file label instead of <test>-run<n>, e.g. practice")
    parser.add_argument("--duration", type=float, help="seconds to record (default 60, or the test's duration)")
    parser.add_argument("--mode", choices=("both", "buffer", "live"), default="both")
    parser.add_argument("--no-clear", action="store_true", help="do not 'clear logging' first")
    parser.add_argument("--reconnect-timeout", type=float, default=120)
    parser.add_argument("--out-dir", type=Path, default=DEBUG_DIR)
    args = parser.parse_args(argv)

    exp = get_experiment(args.test) if args.test else None
    router = args.router or (exp.router if exp else None)
    if not router:
        parser.error("give a router name or --test")
    label = args.label or (run_label(args.test, args.run) if exp else f"capture-{datetime.now():%Y%m%dT%H%M%S}")
    duration = args.duration if args.duration is not None else (exp.duration if exp else 60)

    meta = capture(router, label, duration=duration, mode=args.mode, clear=not args.no_clear,
                   out_dir=args.out_dir, reconnect_timeout=args.reconnect_timeout)
    if exp and exp.network and meta["capture_file"]:
        peer = f" --silent-peer {exp.silent_peer}" if exp.silent_peer else ""
        print(f"\nMeasure it:\n  python scripts/parse_rip_debug.py {meta['capture_file']} "
              f"--network {exp.network} --metric {exp.metric}{peer}")
    if meta["debug_enabled"] and not meta["undebug_ok"]:
        return 2
    return 0 if meta["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
