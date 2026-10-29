"""
monitor_convergence.py - watch one router's RIP routes and log every change.

Keeps one SSH session open and runs 'show ip route rip' about once a
second. Each answer is parsed with get_routes.parse_routes() and compared
with the previous one; any route that appears, disappears, changes metric,
next hop or status (hold-down, 'possibly down') is written to a CSV.

    python scripts/monitor_convergence.py --test T1 --run 1 --mark-on-enter
    python scripts/monitor_convergence.py LIB-R1 --network 192.168.30.0/24 --expect-metric 2 --duration 330
    python scripts/monitor_convergence.py TCH-R2 --label practice --duration 60

Outputs, in data/monitor_logs/:
    <label>_<router>.csv          one row per event (changes, marks, session drops)
    <label>_<router>_polls.csv    one row per poll: real send/receive times and intervals
    <label>_<router>_summary.json first change, converged time, poll statistics

About the times (please read before using them in the report):
  * Every time here is the LAPTOP clock at the moment the answer came back
    ('obs_time'). It is when the change was SEEN, not when it happened.
    The change happened somewhere inside [window_start, obs_time], i.e.
    after the previous poll was sent; 'window_s' is the width of that window.
  * One poll is not one second: on a 64 kbps serial path 'show ip route rip'
    can take longer than that. The real interval of every poll is logged.
  * A 'mark' is the operator pressing Enter when the failure is applied
    (--mark-on-enter). It is a human reaction time, accurate to about 1 s.
    The exact failure time is the %LINK/%LINEPROTO line in the router's own
    debug log (capture_rip_debug.py + parse_rip_debug.py).
  * 'converged' = the watched route reached the expected metric/next hop and
    then stayed unchanged for --stable-for seconds (default 35 s, just over
    one 30 s RIP update period). Its time is when that final state was first
    seen, not when the stability window ended.

If the session drops (T2 and T3 cut the SSH path) it logs 'session_lost',
keeps trying to reconnect, and logs 'session_restored' with the gap.
"""

import argparse
import csv
import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import mean

from get_routes import parse_routes

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MONITOR_DIR = DATA_DIR / "monitor_logs"
COMMAND = "show ip route rip"

EVENT_FIELDS = [
    "obs_time", "t_rel_s", "since_mark_s", "router", "network", "event", "changes",
    "metric_before", "metric_after", "next_hop_before", "next_hop_after",
    "status_before", "status_after", "before", "after",
    "window_start", "window_s", "after_gap", "poll", "detail",
]
POLL_FIELDS = [
    "poll", "sent_time", "received_time", "t_rel_s", "duration_s", "interval_s",
    "ok", "routes", "watched", "error",
]

# Event names written to the CSV
BASELINE = "baseline"
ADDED, RESTORED, LOST = "route_added", "route_restored", "route_lost"
POSSIBLY_DOWN, CHANGED = "possibly_down", "route_changed"
CHANGE_EVENTS = (ADDED, RESTORED, LOST, POSSIBLY_DOWN, CHANGED)


def _default_connect(name):
    from inventory import connect
    return connect(name)


# ---------------------------------------------------------------- pure logic

def output_looks_valid(text):
    """True if `text` is a real 'show ip route' answer (even with no RIP routes).

    IOS always prints the code legend or 'Gateway of last resort'. An empty
    answer, '% Invalid input' or a half-read buffer is NOT 'no routes': if we
    believed it, every route would look lost for one poll.
    """
    if not text or not text.strip():
        return False
    if any(line.strip().startswith("% ") for line in text.splitlines()):
        return False
    return "Codes:" in text or "Gateway of last resort" in text or bool(parse_routes(text))


def group_paths(routes):
    """network -> frozenset of (next_hop, interface, metric, status)."""
    grouped = {}
    for r in routes:
        grouped.setdefault(r["network"], set()).add((r["next_hop"], r["interface"], r["metric"], r["status"]))
    return {net: frozenset(paths) for net, paths in grouped.items()}


def best_metric(paths):
    return min((p[2] for p in paths), default=None)


def next_hops(paths):
    return "+".join(sorted({p[0] for p in paths}))


def statuses(paths):
    return "+".join(sorted({p[3] for p in paths}))


def describe_paths(paths):
    if not paths:
        return "-"
    return " + ".join(f"via {nh} ({iface}) m{metric}{' POSSIBLY DOWN' if st == 'possibly_down' else ''}"
                      for nh, iface, metric, st in sorted(paths))


def classify(old, new, seen_before):
    """Name the change between two path sets for one network.

    Returns (event, changes) where changes lists what differs:
    metric, next_hop, status, paths (an equal-cost path came or went).
    """
    if not old and new:
        return (RESTORED if seen_before else ADDED), "appeared"
    if old and not new:
        return LOST, "disappeared"
    changes = []
    if best_metric(old) != best_metric(new):
        changes.append("metric")
    if next_hops(old) != next_hops(new):
        changes.append("next_hop")
    if statuses(old) != statuses(new):
        changes.append("status")
    if len(old) != len(new):
        changes.append("paths")
    if not changes:
        changes.append("other")  # e.g. same next hop, different interface
    went_down = any(p[3] == "possibly_down" for p in new) and not any(p[3] == "possibly_down" for p in old)
    return (POSSIBLY_DOWN if went_down else CHANGED), ",".join(changes)


def diff_states(old, new, seen):
    """Yield (network, event, changes, old_paths, new_paths) for every network that changed."""
    for network in sorted(set(old) | set(new)):
        a, b = old.get(network, frozenset()), new.get(network, frozenset())
        if a != b:
            event, changes = classify(a, b, network in seen)
            yield network, event, changes, a, b


def target_met(paths, metric, next_hop=None):
    """True if the network has an 'up' path with this metric (and next hop)."""
    return any(p[3] == "up" and p[2] == metric and (not next_hop or p[0] == next_hop) for p in paths)


# ---------------------------------------------------------------- monitor

class RouteMonitor:
    """Polls one router and writes the event and poll CSVs.

    Everything that touches the outside world (connect, clocks, sleep) is a
    parameter, so the tests can drive it with a pretend router.
    """

    def __init__(self, router, label, networks=None, target=None, out_dir=MONITOR_DIR, interval=1.0, duration=None,
                 expect_metric=None, expect_next_hop=None, stable_for=35.0, stop_when_converged=False,
                 read_timeout=10, reconnect_delay=2.0, connect_fn=_default_connect, clock=time.monotonic,
                 now=datetime.now, sleep=time.sleep, marks=None, log=print, source="live"):
        self.router, self.label = router, label
        self.networks = set(networks) if networks else None
        # The route whose convergence is measured. Other networks are still logged.
        self.target = target or (sorted(self.networks)[0] if self.networks and len(self.networks) == 1 else None)
        self.out_dir = Path(out_dir)
        self.interval, self.duration = float(interval), duration
        self.expect_metric, self.expect_next_hop = expect_metric, expect_next_hop
        self.stable_for, self.stop_when_converged = float(stable_for), stop_when_converged
        self.read_timeout, self.reconnect_delay = read_timeout, reconnect_delay
        self.connect_fn, self.clock, self.now, self.sleep = connect_fn, clock, now, sleep
        self.marks = marks if marks is not None else queue.Queue()
        self.log, self.source = log, source
        self.stop_event = threading.Event()

        stem = f"{label}_{router}"
        self.events_path = self.out_dir / f"{stem}.csv"
        self.polls_path = self.out_dir / f"{stem}_polls.csv"
        self.summary_path = self.out_dir / f"{stem}_summary.json"

        # run state
        self.conn = None
        self.state = None            # last good network -> paths
        self.seen = set()
        self.poll_no = 0
        self.t0 = None
        self.last_ok_sent = None     # (datetime, t_rel) of the last good poll's send
        self.last_poll_start = None
        self.after_gap = False
        self.session_down_since = None
        self.error_streak = False
        self.mark_times = []         # (datetime, text)
        self.change_rows = []        # (obs datetime, row) of every change event
        self.converged = None        # event row dict
        self.candidate = None        # (obs_time, t_rel, poll) when the current target state was first seen
        self.target_at_start = False
        self.gaps = []
        self.durations, self.intervals = [], []
        self.polls_ok = self.polls_failed = self.overruns = 0
        self.rows = 0

    # -- helpers
    def _t(self):
        return self.clock() - self.t0

    def _since_mark(self, dt):
        if not self.mark_times:
            return ""
        return round((dt - self.mark_times[0][0]).total_seconds(), 3)

    def _filter(self, state):
        if self.networks is None:
            return state
        return {n: p for n, p in state.items() if n in self.networks}

    def _write_event(self, obs_time, event, network="", changes="", old=frozenset(), new=frozenset(),
                     window_start=None, detail="", t_rel=None):
        row = {
            "obs_time": obs_time.isoformat(timespec="milliseconds"),
            "t_rel_s": round(self._t() if t_rel is None else t_rel, 3),
            "since_mark_s": self._since_mark(obs_time),
            "router": self.router, "network": network, "event": event, "changes": changes,
            "metric_before": best_metric(old) if old else "", "metric_after": best_metric(new) if new else "",
            "next_hop_before": next_hops(old), "next_hop_after": next_hops(new),
            "status_before": statuses(old), "status_after": statuses(new),
            "before": describe_paths(old) if old else "", "after": describe_paths(new) if new else "",
            "window_start": window_start.isoformat(timespec="milliseconds") if window_start else "",
            "window_s": round((obs_time - window_start).total_seconds(), 3) if window_start else "",
            "after_gap": int(self.after_gap) if event in CHANGE_EVENTS else "",
            "poll": self.poll_no, "detail": detail,
        }
        self._events.writerow(row)
        self._events_file.flush()
        self.rows += 1
        return row

    def _say(self, row):
        what = f"{row['network']} {row['event']}" if row["network"] else row["event"]
        extra = f"  {row['before'] or '-'} -> {row['after'] or '-'}" if row["event"] in CHANGE_EVENTS else ""
        mark = f" (+{row['since_mark_s']} s after mark)" if row["since_mark_s"] != "" else ""
        self.log(f"[{row['obs_time'][11:23]}] {what}{extra}{mark} {row['detail']}".rstrip())

    # -- connection
    def _close(self):
        if self.conn is not None:
            try:
                self.conn.disconnect()
            except Exception:
                pass
        self.conn = None

    def _connect(self):
        try:
            self.conn = self.connect_fn(self.router)
            return True
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self.conn = None
            self._last_connect_error = f"{exc.__class__.__name__}: {exc}".splitlines()[0]
            return False

    # -- marks
    def mark(self, text="failure applied", when=None):
        """Record an operator mark (thread-safe: goes through the queue)."""
        self.marks.put((when or self.now(), text))

    def _drain_marks(self):
        while True:
            try:
                when, text = self.marks.get_nowait()
            except queue.Empty:
                return
            self.mark_times.append((when, text))
            self._say(self._write_event(when, "mark", detail=text,
                                        t_rel=self._t() - (self.now() - when).total_seconds()))

    # -- one poll
    def _poll(self):
        self.poll_no += 1
        start_mono = self.clock()
        sent = self.now()
        interval = round(start_mono - self.last_poll_start, 3) if self.last_poll_start is not None else ""
        self.last_poll_start = start_mono
        if interval != "":
            self.intervals.append(interval)
        poll_row = {"poll": self.poll_no, "sent_time": sent.isoformat(timespec="milliseconds"),
                    "t_rel_s": round(start_mono - self.t0, 3), "interval_s": interval}
        try:
            text = self.conn.send_command(COMMAND, read_timeout=self.read_timeout)
            error = None if output_looks_valid(text) else "unexpected output: " + (text or "<empty>").strip()[:80]
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            text, error = None, f"{exc.__class__.__name__}: {exc}".splitlines()[0]
        received = self.now()
        duration = round(self.clock() - start_mono, 3)
        self.durations.append(duration)
        if self.interval and duration > self.interval:
            self.overruns += 1
        poll_row.update(received_time=received.isoformat(timespec="milliseconds"), duration_s=duration)

        if text is None:  # the session itself failed
            self.polls_failed += 1
            poll_row.update(ok=0, routes="", watched="", error=error)
            self._polls.writerow(poll_row)
            self._polls_file.flush()
            self.session_down_since = (received, self._t())
            self._say(self._write_event(received, "session_lost", detail=error))
            self._close()
            return

        if error:  # session fine, answer unusable: keep the old state
            self.polls_failed += 1
            poll_row.update(ok=0, routes="", watched="", error=error)
            self._polls.writerow(poll_row)
            self._polls_file.flush()
            if not self.error_streak:
                self._say(self._write_event(received, "unexpected_output", detail=error))
            self.error_streak = True
            return

        self.error_streak = False
        self.polls_ok += 1
        routes = parse_routes(text)
        new = self._filter(group_paths(routes))
        watched = "; ".join(f"{n}={describe_paths(p)}" for n, p in sorted(new.items()))
        poll_row.update(ok=1, routes=len(routes), watched=watched, error="")
        self._polls.writerow(poll_row)
        self._polls_file.flush()

        first = self.state is None
        target_changed = False
        if first:
            for network, paths in sorted(new.items()):
                self._write_event(received, BASELINE, network, "", frozenset(), paths)
            self.log(f"[{received.isoformat()[11:23]}] baseline: " + (watched or "no RIP routes"))
            if self.networks:
                for missing in sorted(self.networks - set(new)):
                    self._write_event(received, BASELINE, missing, "", detail="not in routing table")
        else:
            window = self.last_ok_sent[0] if self.last_ok_sent else None
            for network, event, changes, a, b in diff_states(self.state, new, self.seen):
                row = self._write_event(received, event, network, changes, a, b, window_start=window,
                                        detail="after session gap" if self.after_gap else "")
                self._say(row)
                self.change_rows.append((received, row))
                if network == self.target:
                    target_changed = True
        self.seen |= set(new)
        self.state = new
        self.last_ok_sent = (sent, poll_row["t_rel_s"])
        self.after_gap = False
        self._track_convergence(received, target_changed, first=first)

    def _track_convergence(self, received, target_changed, first):
        """Declare 'converged' once the target route is in the expected state and stays there."""
        if not (self.target and self.expect_metric is not None):
            return
        met = target_met(self.state.get(self.target, frozenset()), self.expect_metric, self.expect_next_hop)
        if first:
            # Already in the end state before anything happened: not a measurement.
            self.target_at_start = met
            if met:
                self.log("  note: the target state is already met at the start (nothing to measure yet)")
            return
        if target_changed:
            if self.converged is not None:
                self._say(self._write_event(received, "left_converged_state", self.target,
                                            detail="route changed again after convergence"))
                self.converged = None
            self.target_at_start = False
            self.candidate = (received, self._t(), self.poll_no) if met else None
        elif not met:
            self.candidate = None
        if met and self.candidate and self.converged is None:
            if (received - self.candidate[0]).total_seconds() >= self.stable_for:
                row = self._write_event(self.candidate[0], "converged", self.target,
                                        old=frozenset(), new=self.state.get(self.target, frozenset()),
                                        detail=f"stable for {self.stable_for:g} s (confirmed at "
                                               f"{received.isoformat(timespec='milliseconds')[11:]})",
                                        t_rel=self.candidate[1])
                self.converged = row
                self._say(row)
                if self.stop_when_converged:
                    self.stop_event.set()

    # -- main loop
    def run(self):
        """Poll until --duration, stop_event, or Ctrl+C. Returns the summary dict."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.t0 = self.clock()
        started = self.now()
        self._last_connect_error = None
        interrupted = False
        with self.events_path.open("w", newline="", encoding="utf-8") as ef, \
                self.polls_path.open("w", newline="", encoding="utf-8") as pf:
            self._events_file, self._polls_file = ef, pf
            self._events = csv.DictWriter(ef, fieldnames=EVENT_FIELDS)
            self._polls = csv.DictWriter(pf, fieldnames=POLL_FIELDS)
            self._events.writeheader()
            self._polls.writeheader()
            self._write_event(started, "monitor_start",
                              detail=f"source={self.source}; interval={self.interval:g}s; "
                                     f"target={self.target or 'all'}; expect_metric={self.expect_metric}")
            try:
                if not self._connect():
                    self._say(self._write_event(self.now(), "connect_failed", detail=self._last_connect_error))
                    self.session_down_since = (self.now(), self._t())
                next_due = self.clock()
                while not self.stop_event.is_set():
                    if self.duration is not None and self._t() >= self.duration:
                        break
                    self._drain_marks()
                    if self.conn is None:
                        if self._connect():
                            down_dt, down_t = self.session_down_since or (self.now(), self._t())
                            gap = round((self.now() - down_dt).total_seconds(), 3)
                            self.gaps.append({"lost": down_dt.isoformat(timespec="milliseconds"),
                                              "restored": self.now().isoformat(timespec="milliseconds"),
                                              "gap_s": gap})
                            if self.state is not None:
                                self.after_gap = True
                            self._say(self._write_event(self.now(), "session_restored", detail=f"gap {gap} s"))
                            self.session_down_since = None
                        else:
                            self.sleep(self.reconnect_delay)
                            continue
                    self._poll()
                    next_due += self.interval
                    wait = next_due - self.clock()
                    if wait > 0:
                        self.sleep(wait)
                    else:
                        next_due = self.clock()  # running late: poll again at once, do not burst
                self._drain_marks()
            except KeyboardInterrupt:
                interrupted = True
                self.log("Stopped by Ctrl+C.")
            finally:
                self._close()
                self._write_event(self.now(), "monitor_stop", detail="interrupted" if interrupted else "")
        summary = self.summary(started, interrupted)
        self.summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    def first_change(self):
        """First change on the target (any network if no target), after the first mark if there is one."""
        rows = [(t, r) for t, r in self.change_rows if not self.target or r["network"] == self.target]
        if self.mark_times:
            rows = [(t, r) for t, r in rows if t >= self.mark_times[0][0]]
        return rows[0][1] if rows else None

    def summary(self, started, interrupted=False):
        def stamp(row):
            if not row:
                return None
            # Recomputed here: a change seen just before the mark was taken off
            # the queue has an empty since_mark_s in its CSV row.
            since = self._since_mark(datetime.fromisoformat(row["obs_time"]))
            return {"obs_time": row["obs_time"], "since_mark_s": since if since != "" else None,
                    "window_s": row["window_s"] if row.get("window_s", "") != "" else None,
                    "network": row["network"], "event": row["event"], "after": row["after"]}
        return {
            "label": self.label, "router": self.router, "source": self.source,
            "networks": sorted(self.networks) if self.networks else "all",
            "target": self.target, "expect_metric": self.expect_metric, "expect_next_hop": self.expect_next_hop,
            "stable_for_s": self.stable_for, "interval_target_s": self.interval,
            "started": started.isoformat(timespec="milliseconds"),
            "ended": self.now().isoformat(timespec="milliseconds"), "interrupted": interrupted,
            "marks": [{"time": t.isoformat(timespec="milliseconds"), "text": x} for t, x in self.mark_times],
            "first_change": stamp(self.first_change()),
            "converged": stamp(self.converged),
            "target_met_at_start": self.target_at_start,
            "final_state": {n: describe_paths(p) for n, p in sorted((self.state or {}).items())},
            "polls": self.poll_no, "polls_ok": self.polls_ok, "polls_failed": self.polls_failed,
            "poll_interval_mean_s": round(mean(self.intervals), 3) if self.intervals else None,
            "poll_interval_max_s": max(self.intervals) if self.intervals else None,
            "poll_duration_mean_s": round(mean(self.durations), 3) if self.durations else None,
            "poll_duration_max_s": max(self.durations) if self.durations else None,
            "polls_slower_than_interval": self.overruns,
            "session_gaps": self.gaps,
            "files": {"events": str(self.events_path), "polls": str(self.polls_path)},
            "time_note": "obs_time = laptop clock when the change was SEEN; the change happened inside "
                         "[window_start, obs_time]. Marks are operator key presses, not router events.",
        }


def start_enter_marker(monitor):
    """Background thread: every Enter on the keyboard adds a mark."""
    def reader():
        while not monitor.stop_event.is_set():
            try:
                text = input()
            except (EOFError, OSError):
                return
            monitor.mark(text.strip() or "failure applied")
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    return thread


def print_summary(s, log=print):
    log(f"\nPolls: {s['polls']} ({s['polls_failed']} failed), real interval mean "
        f"{s['poll_interval_mean_s']} s / max {s['poll_interval_max_s']} s, "
        f"{s['polls_slower_than_interval']} polls took longer than {s['interval_target_s']:g} s")
    for g in s["session_gaps"]:
        log(f"Session gap: {g['gap_s']} s ({g['lost'][11:]} -> {g['restored'][11:]})")
    if s["marks"]:
        log(f"Mark: {s['marks'][0]['time']}")
    for key in ("first_change", "converged"):
        if s[key]:
            rel = f", {s[key]['since_mark_s']} s after mark" if s[key]["since_mark_s"] is not None else ""
            log(f"{key.replace('_', ' ').capitalize()}: {s[key]['obs_time']}{rel}  ({s[key]['network']})")
    if s["target"] and s["expect_metric"] is not None and not s["converged"]:
        log("Not converged to the expected state within the run" +
            (" (it was already met at the start)" if s["target_met_at_start"] else ""))
    log(f"Saved {s['files']['events']}")


def main(argv=None):
    from experiments import get_experiment, run_label

    parser = argparse.ArgumentParser(description="Poll a router's RIP routes and log every change.")
    parser.add_argument("router", nargs="?", help="LIB-R1, TCH-R2 or LAB-R3 (default: the test's router)")
    parser.add_argument("--test", help="T1..T5: fills router, network, expected metric/next hop, duration")
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--label", help="file label (default <test>-run<n>)")
    parser.add_argument("--network", action="append", help="watch only this network (repeatable)")
    parser.add_argument("--expect-metric", type=int, help="metric of the converged route")
    parser.add_argument("--expect-next-hop", help="next hop of the converged route")
    parser.add_argument("--interval", type=float, default=1.0, help="target seconds between polls (default 1)")
    parser.add_argument("--duration", type=float, help="stop after this many seconds (default: test duration + 30, "
                                                       "or until Ctrl+C)")
    parser.add_argument("--stable-for", type=float, default=35.0)
    parser.add_argument("--stop-when-converged", action="store_true")
    parser.add_argument("--mark-on-enter", action="store_true", help="press Enter to mark the failure moment")
    parser.add_argument("--read-timeout", type=float, default=10)
    parser.add_argument("--out-dir", type=Path, default=MONITOR_DIR)
    args = parser.parse_args(argv)

    exp = get_experiment(args.test) if args.test else None
    router = args.router or (exp.router if exp else None)
    if not router:
        parser.error("give a router name or --test")
    networks = args.network or ([exp.network] if exp and exp.network else None)
    expect_metric = args.expect_metric if args.expect_metric is not None else (exp.metric if exp and exp.network else None)
    expect_nh = args.expect_next_hop or (exp.next_hop if exp else None)
    duration = args.duration if args.duration is not None else (exp.duration + 30 if exp else None)
    label = args.label or (run_label(args.test, args.run) if exp else f"monitor-{datetime.now():%Y%m%dT%H%M%S}")

    monitor = RouteMonitor(router, label, networks=networks, out_dir=args.out_dir, interval=args.interval,
                           duration=duration, expect_metric=expect_metric, expect_next_hop=expect_nh,
                           stable_for=args.stable_for, stop_when_converged=args.stop_when_converged,
                           read_timeout=args.read_timeout)
    print(f"Monitoring {router} ({', '.join(networks) if networks else 'all RIP routes'}) every "
          f"{args.interval:g} s{f' for {duration:g} s' if duration else ''}. Ctrl+C to stop.")
    if args.mark_on_enter:
        print(">>> Press Enter at the moment the failure is applied (each Enter adds a mark).")
        start_enter_marker(monitor)
    summary = monitor.run()
    print_summary(summary)
    return 0 if summary["polls_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
