"""
simulate_data.py - generate SIMULATED sample experiment data and sample charts.

Runs the real measurement pipeline (capture_rip_debug, monitor_convergence,
parse_rip_debug, get_routes, compare_routes, collect_results, visualize)
against sim_router.py's virtual clock and writes everything under
data/simulated/ plus SIMULATED_*.png sample charts in data/charts/.

    python scripts/simulate_data.py

Every generated file is marked SIMULATED in its header, metadata or filename.
Real data directories (data/snapshots/, data/debug_logs/, data/monitor_logs/)
are NEVER touched by this script.
"""

import json
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from collect_results import collect, format_markdown_table, write_results_csv
from experiments import FAILURE_TESTS, get_experiment, run_label
from monitor_convergence import RouteMonitor
from parse_rip_debug import parse_debug, write_csv
from sim_router import BASELINE, R, SimClock, SimLab, _drop, make_scenario, render_debug_log
from visualize import generate_all_charts

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SIM_DIR = DATA_DIR / "simulated"

SIM_BANNER = (
    "! =========================================================================\n"
    "! SIMULATED SAMPLE CAPTURE — GENERATED OFFLINE BY scripts/simulate_data.py\n"
    "! NOT RECORDED FROM PHYSICAL CISCO ROUTERS. DO NOT SUBMIT AS LAB EVIDENCE.\n"
    "! =========================================================================\n"
)

# Representative convergence delays (seconds) for 3 runs of each test:
#   T1..T4: 0..30 s depending on where in the 30 s periodic RIP update cycle
#           the link change falls.
#   T5:     240 s flush timer + wait (0..30 s) for TCH-R2's next update.
SIM_RUNS = {
    "T1": [7.42, 18.15, 11.68],
    "T2": [14.20, 6.85, 22.40],
    "T3": [9.60, 21.10, 13.75],
    "T4": [4.30, 12.90, 8.15],
    "T5": [252.40, 263.80, 246.90],
}


def _after_all_routers(test):
    """Expected 3-router routing tables after `test` converges (for simulated snapshots)."""
    lib = [dict(r) for r in BASELINE["LIB-R1"]]
    tch = [dict(r) for r in BASELINE["TCH-R2"]]
    lab = [dict(r) for r in BASELINE["LAB-R3"]]
    if test == "T1":
        lib = _drop(_drop(lib, "192.168.20.0/24"), "10.0.23.0/30", "10.0.12.2") + \
            [R("192.168.20.0/24", 2, "10.0.13.2", "GigabitEthernet0/1")]
        tch = _drop(_drop(tch, "192.168.10.0/24"), "10.0.13.0/30", "10.0.12.1") + \
            [R("192.168.10.0/24", 2, "10.0.23.2", "Serial0/0/1")]
        lab = _drop(lab, "10.0.12.0/30")
    elif test == "T2":
        lib = _drop(_drop(lib, "192.168.30.0/24"), "10.0.23.0/30", "10.0.13.2") + \
            [R("192.168.30.0/24", 2, "10.0.12.2", "Serial0/0/0")]
        lab = _drop(_drop(lab, "192.168.10.0/24"), "10.0.12.0/30", "10.0.13.1") + \
            [R("192.168.10.0/24", 2, "10.0.23.1", "Serial0/0/0")]
        tch = _drop(tch, "10.0.13.0/30")
    elif test == "T3":
        tch = _drop(_drop(tch, "192.168.30.0/24"), "10.0.13.0/30", "10.0.23.2") + \
            [R("192.168.30.0/24", 2, "10.0.12.1", "Serial0/0/0")]
        lab = _drop(_drop(lab, "192.168.20.0/24"), "10.0.12.0/30", "10.0.23.1") + \
            [R("192.168.20.0/24", 2, "10.0.13.1", "GigabitEthernet0/1")]
        lib = _drop(lib, "10.0.23.0/30")
    elif test == "T5":
        # Asymmetric routing: LIB-R1 goes via TCH-R2 (m2), LAB-R3 keeps direct (m1)
        lib = _drop(_drop(lib, "192.168.30.0/24"), "10.0.23.0/30", "10.0.13.2") + \
            [R("192.168.30.0/24", 2, "10.0.12.2", "Serial0/0/0")]
    return {"LIB-R1": lib, "TCH-R2": tch, "LAB-R3": lab}


def _write_sim_snapshot(snap_dir, stamp, label, routers_dict):
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = {
        "simulated": True,
        "note": "SIMULATED SAMPLE SNAPSHOT — NOT FROM PHYSICAL ROUTERS",
        "time": stamp.isoformat(timespec="seconds"),
        "label": label,
        "routers": routers_dict,
    }
    fname = f"SIMULATED_{stamp.strftime('%Y%m%dT%H%M%S')}_{label}.json"
    path = snap_dir / fname
    path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    return path


def generate_simulated_dataset(out_dir=SIM_DIR, charts_dir=DATA_DIR / "charts"):
    """Generate the full simulated dataset (T0..T5, 3 runs each) and sample charts."""
    out_dir = Path(out_dir)
    debug_dir = out_dir / "debug_logs"
    monitor_dir = out_dir / "monitor_logs"
    snap_dir = out_dir / "snapshots"
    sim_charts_dir = out_dir / "charts"
    for d in (debug_dir, monitor_dir, snap_dir, sim_charts_dir):
        d.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 10, 28, 10, 0, 0)
    _write_sim_snapshot(snap_dir, base_time, "T0-baseline", BASELINE)

    pings = {}
    seed = 100
    for test in FAILURE_TESTS:
        exp = get_experiment(test)
        for run_idx, delay in enumerate(SIM_RUNS[test], start=1):
            seed += 1
            rng = random.Random(seed)
            label = run_label(test, run_idx)
            run_clock_start = base_time + timedelta(minutes=12 * seed)
            clock = SimClock(base=run_clock_start)

            fail_at = float(exp.pre_roll or 5) + 5.0
            sc = make_scenario(test, delay, fail_at=fail_at, rng=rng)
            total_dur = exp.duration + 35

            debug_body = render_debug_log(sc, run_clock_start, delay, total_dur, rng=rng)
            lab = SimLab(sc, clock, debug_text=debug_body)

            before_routers = _after_all_routers("T1") if test == "T4" else BASELINE
            _write_sim_snapshot(snap_dir, run_clock_start, f"{label}-before", before_routers)

            monitor = RouteMonitor(
                sc.router, label, target=exp.network, out_dir=monitor_dir,
                interval=1.0, duration=total_dur, expect_metric=exp.metric,
                expect_next_hop=exp.next_hop, stable_for=35.0, connect_fn=lab.connect,
                clock=clock.monotonic, now=clock.now, sleep=clock.sleep,
                log=lambda *a: None, source="simulated",
            )
            orig_drain = monitor._drain_marks

            def drain_with_mark(m=monitor, sc=sc, clock=clock, orig=orig_drain):
                if not m.mark_times and m.marks.empty() and clock.t >= sc.fail_at:
                    m.mark("SIMULATED failure applied",
                           when=clock.base + timedelta(seconds=sc.fail_at))
                orig()

            monitor._drain_marks = drain_with_mark
            monitor.run()

            # Write simulated debug capture + parsed CSV + metadata
            stem = f"{label}_{exp.router}"
            log_path = debug_dir / f"{stem}.txt"
            full_log = SIM_BANNER + debug_body
            log_path.write_text(full_log, encoding="utf-8")
            write_csv(parse_debug(full_log), log_path.with_suffix(".csv"))
            (debug_dir / f"{stem}.meta.json").write_text(json.dumps({
                "simulated": True,
                "source": "simulated",
                "router": exp.router,
                "label": label,
                "duration_s": total_dur,
                "note": "SIMULATED SAMPLE DATA — NOT FROM PHYSICAL ROUTERS",
            }, indent=2), encoding="utf-8")

            _write_sim_snapshot(
                snap_dir, run_clock_start + timedelta(seconds=total_dur),
                f"{label}-after", _after_all_routers(test),
            )

            # Simulated ping loss (continuous ping -t -w 1000: 1 reply/s during outage)
            if test == "T5":
                # Traffic flows while route is in hold-down; brief gap between flush (240s) and new route
                pings[label] = max(0, int(round(delay - 240.0)))
            elif test == "T4":
                pings[label] = 0
            else:
                pings[label] = max(1, int(math.ceil(delay)))

    rows = collect(debug_dir=debug_dir, monitor_dir=monitor_dir, pings=pings, source="simulated")
    results_csv = write_results_csv(rows, out_dir / "results.csv")
    table_md = format_markdown_table(rows)
    (out_dir / "SIMULATED_RESULTS_TABLE.md").write_text(table_md + "\n", encoding="utf-8")

    # Generate charts in both data/simulated/charts/ and data/charts/ (prefixed SIMULATED_)
    timeline_picks = [
        monitor_dir / "T1-run1_LIB-R1.csv",
        monitor_dir / "T2-run1_LIB-R1.csv",
        monitor_dir / "T5-run1_LIB-R1.csv",
    ]
    sim_charts = generate_all_charts(
        results_path=results_csv, debug_dir=debug_dir, monitor_dir=monitor_dir,
        charts_dir=sim_charts_dir, timeline_files=timeline_picks, simulated=True,
    )
    main_charts = generate_all_charts(
        results_path=results_csv, debug_dir=debug_dir, monitor_dir=monitor_dir,
        charts_dir=charts_dir, timeline_files=timeline_picks, simulated=True,
    )
    return {"results_csv": results_csv, "sim_charts": sim_charts, "main_charts": main_charts, "table": table_md}


def main():
    info = generate_simulated_dataset()
    print(f"Wrote simulated results: {info['results_csv']}\n")
    print(info["table"])
    print("\nGenerated simulated charts:")
    for p in info["main_charts"]:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
