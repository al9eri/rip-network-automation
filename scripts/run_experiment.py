"""
run_experiment.py - safe, operator-guided runner for one test run (T0..T5).

Automates the repetitive bookkeeping around a test run so lab time is spent
on the routers, not typing file names:
  1. Checks that the measured router has the expected before-state route
     (e.g. metric 1 before T1, metric 2 before T4) and warns if not.
  2. Saves the before-snapshot across all three routers (get_routes.py).
  3. Connects to the measured router, runs 'clear logging', and turns on
     'debug ip rip' + 'debug ip routing' into the 64 KB router log buffer.
  4. Starts monitor_convergence.py and prints the exact CLI commands for the
     operator (or teammate) to run on the router console / SSH session.
  5. Waits for the operator to press Enter at the moment the failure is
     applied (records a mark), then polls until convergence or duration.
  6. Reconnects to the measured router, sends 'undebug all', saves
     'show logging' to data/debug_logs/<test>-run<n>_<router>.txt, and runs
     parse_rip_debug.py on it.
  7. Saves the after-snapshot and prints compare_routes.py diff + updated
     results.csv row.

SAFETY: This script NEVER shuts down interfaces or modifies router routing
configuration on its own. Every link failure or recovery action is performed
explicitly by the human operator when prompted.

    python scripts/run_experiment.py T0
    python scripts/run_experiment.py T1 --run 1
    python scripts/run_experiment.py T4 --run 1
    python scripts/run_experiment.py T5 --run 1 --pings-lost 0
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from capture_rip_debug import (
    DEBUG_DIR, check_router_setup, read_buffer, reconnect, router_clock, start_debug, stop_debug,
)
from collect_results import collect, format_markdown_table, write_results_csv
from compare_routes import describe as describe_paths_short, diff_snapshots
from experiments import get_experiment, run_label
from get_routes import DATA_DIR, save_snapshot, take_snapshot
from monitor_convergence import MONITOR_DIR, RouteMonitor, print_summary, start_enter_marker
from parse_rip_debug import convergence, describe as describe_debug, parse_debug, write_csv

SNAPSHOT_DIR = DATA_DIR / "snapshots"


def _default_connect(name):
    from inventory import connect
    return connect(name)


def check_precondition(snapshot, exp):
    """Check whether `exp.router` has `exp.network` at `exp.before_metric` before the run."""
    if not exp.network or not exp.before_metric:
        return True, "no precondition"
    routes = snapshot.get("routers", {}).get(exp.router, [])
    matches = [r for r in routes if r["network"] == exp.network and r["status"] == "up"]
    if not matches:
        return False, f"{exp.network} is missing on {exp.router} (expected metric {exp.before_metric})"
    metrics = {r["metric"] for r in matches}
    if exp.before_metric not in metrics:
        return False, (f"{exp.network} on {exp.router} is at metric {sorted(metrics)}, "
                       f"expected {exp.before_metric} before {exp.test}")
    return True, f"{exp.network} on {exp.router} is at metric {exp.before_metric} (ready)"


def run_one(test, run=1, duration=None, stable_for=35.0, pings_lost=None,
            connect_fn=_default_connect, snapshot_fn=take_snapshot, save_snap_fn=save_snapshot,
            interactive=True, out_base=DATA_DIR):
    """Execute the full workflow for one test run. Returns a summary dict."""
    exp = get_experiment(test)
    out_base = Path(out_base)
    debug_dir = out_base / "debug_logs"
    monitor_dir = out_base / "monitor_logs"

    if exp.test == "T0":
        print("=== T0 Baseline: taking snapshot of all routers ===")
        snap = snapshot_fn("T0-baseline")
        path = save_snap_fn(snap)
        print(f"Saved baseline snapshot: {path}")
        return {"test": "T0", "snapshot": str(path)}

    label = run_label(exp.test, run)
    dur = duration if duration is not None else exp.duration
    print(f"\n============================================================")
    print(f"  {label}: {exp.title}  (measured router: {exp.router})")
    print(f"============================================================")
    for note in exp.notes:
        print(f"  Note: {note}")

    # 1. Before snapshot + precondition check
    print(f"\n[1/6] Taking before-snapshot ({label}-before)...")
    before = snapshot_fn(f"{label}-before")
    before_path = save_snap_fn(before)
    ok_pre, msg_pre = check_precondition(before, exp)
    print(f"  Precondition: {msg_pre}")
    if not ok_pre and interactive:
        ans = input("  Precondition not met. Continue anyway? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Aborted before enabling debugs.")
            return {"test": exp.test, "run": run, "aborted": True}

    # 2. Clear logging and turn on debugs in the router buffer
    print(f"\n[2/6] Enabling 'debug ip rip' + 'debug ip routing' on {exp.router}...")
    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{label}_{exp.router}"
    log_path = debug_dir / f"{stem}.txt"
    meta_path = debug_dir / f"{stem}.meta.json"
    meta = {
        "router": exp.router, "label": label, "mode": "buffer", "duration_s": dur,
        "capture_file": str(log_path), "laptop_start": datetime.now().isoformat(timespec="milliseconds"),
        "warnings": [], "undebug_ok": False, "source": "measured",
    }
    conn = connect_fn(exp.router)
    try:
        meta["warnings"] += check_router_setup(conn)
        for w in meta["warnings"]:
            print(f"  WARNING: {w}")
        meta["clock_start"] = router_clock(conn)
        start_debug(conn, clear=True, monitor=False)
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass

    # 3. Print operator action instructions & start monitor
    print(f"\n[3/6] Operator action required during the run:")
    for line in exp.action:
        print(f"    {line}")
    if exp.pre_roll:
        print(f"\n  Wait ~{exp.pre_roll} s after monitoring starts, apply the action above,")
        print("  and press Enter in this window right as you apply it.")

    print(f"\n[4/6] Starting route monitor on {exp.router} for {dur:g} s...")
    monitor = RouteMonitor(
        exp.router, label, target=exp.network, out_dir=monitor_dir,
        interval=1.0, duration=dur, expect_metric=exp.metric, expect_next_hop=exp.next_hop,
        stable_for=stable_for, connect_fn=connect_fn,
    )
    if interactive:
        start_enter_marker(monitor)
    mon_summary = monitor.run()
    print_summary(mon_summary)

    # 5. Reconnect, undebug all, and save 'show logging'
    print(f"\n[5/6] Reading debug buffer from {exp.router} and running 'undebug all'...")
    conn = reconnect(exp.router, connect_fn, timeout=120)
    dbg_result = None
    if conn is not None:
        try:
            stop_debug(conn)
            meta["undebug_ok"] = True
            text = read_buffer(conn)
            meta["clock_end"] = router_clock(conn)
            log_path.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
            events = parse_debug(text)
            write_csv(events, log_path.with_suffix(".csv"))
            dbg_result = convergence(events, exp.network, exp.metric, exp.silent_peer or None)
            if dbg_result:
                print(f"  Debug start: {describe_debug(dbg_result['start'])}")
                print(f"  Debug end:   {describe_debug(dbg_result['end'])}")
                print(f"  Debug convergence: {dbg_result['seconds']:.3f} s")
            else:
                print("  No convergence event pair found in debug buffer (check 'debug ip routing').")
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass
    else:
        print(f"  WARNING: Could not reconnect to {exp.router}! Run 'undebug all' on its console.")
    meta["laptop_end"] = datetime.now().isoformat(timespec="milliseconds")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # 6. After snapshot & diff
    print(f"\n[6/6] Taking after-snapshot ({label}-after) and comparing routes...")
    after = snapshot_fn(f"{label}-after")
    after_path = save_snap_fn(after)
    for c in diff_snapshots(before, after):
        if c["change"] == "not captured":
            print(f"  {c['router']}: not captured")
        else:
            print(f"  {c['router']} {c['change'].upper():7} {c['network']}: "
                  f"{describe_paths_short(c['before'])} -> {describe_paths_short(c['after'])}")

    if exp.restore:
        print("\nBefore the next test, restore the network:")
        for line in exp.restore:
            print(f"    {line}")

    pings = {label: pings_lost} if pings_lost is not None else {}
    rows = collect(debug_dir=debug_dir, monitor_dir=monitor_dir, pings=pings)
    write_results_csv(rows, out_base / "results.csv")
    return {
        "test": exp.test, "run": run, "label": label,
        "before_snapshot": str(before_path), "after_snapshot": str(after_path),
        "debug_log": str(log_path),
        "debug_convergence_s": dbg_result["seconds"] if dbg_result else None,
        "monitor_summary": mon_summary,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Safe, operator-guided runner for T0..T5.")
    parser.add_argument("test", help="T0, T1, T2, T3, T4 or T5")
    parser.add_argument("--run", type=int, default=1, help="run number (1..3)")
    parser.add_argument("--duration", type=float, help="override monitor/capture duration (seconds)")
    parser.add_argument("--stable-for", type=float, default=35.0)
    parser.add_argument("--pings-lost", type=int, help="lost ping count from 'ping -t -w 1000'")
    args = parser.parse_args(argv)

    run_one(args.test, run=args.run, duration=args.duration,
            stable_for=args.stable_for, pings_lost=args.pings_lost)
    return 0


if __name__ == "__main__":
    sys.exit(main())
