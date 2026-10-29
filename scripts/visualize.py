"""
visualize.py - generate convergence comparison and route-change timeline charts.

Reads data/results.csv (or builds it from data/debug_logs/ + data/monitor_logs/)
and plots:
  1. convergence_comparison.png    average convergence per test (T1..T5) with
                                   min-max error bars and individual run dots
  2. timeline_<test>_<router>.png  step timeline of route metric, next hop and
                                   status (up / possibly_down / missing) from a
                                   monitor_convergence.py log

    python scripts/visualize.py
    python scripts/visualize.py --simulated
    python scripts/visualize.py --results data/results.csv --timeline data/monitor_logs/T1-run1_LIB-R1.csv

T5 (~240-270 s) is an order of magnitude longer than T1..T4 (~1-30 s), so the
comparison figure shows a two-panel layout: all tests T1..T5 on the left, and a
zoomed T1..T4 panel on the right so the fast tests remain easy to read.

Whenever any input is marked 'simulated' (or --simulated is passed), the chart
title, subtitle, watermark and file name clearly mark it as SIMULATED SAMPLE
DATA so it can never be mistaken for real lab measurements.
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless-safe for tests and SSH terminals
import matplotlib.pyplot as plt

from collect_results import collect, load_results_csv, summarize_by_test, write_results_csv
from experiments import EXPERIMENTS, FAILURE_TESTS

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHARTS_DIR = DATA_DIR / "charts"

SIM_WATERMARK = "SIMULATED SAMPLE DATA — NOT FROM PHYSICAL ROUTERS"


def _float(val):
    if val in (None, ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def is_simulated_rows(rows, force=False):
    if force:
        return True
    for r in rows:
        src = str(r.get("source") or r.get("detail") or "").lower()
        if "sim" in src:
            return True
    return False


def _stamp_simulated(fig):
    """Add an unmistakable banner and diagonal watermark to a simulated chart."""
    fig.text(
        0.5, 0.985, SIM_WATERMARK,
        ha="center", va="top", fontsize=10, fontweight="bold", color="#8b0000",
        bbox={"boxstyle": "round,pad=0.3", "facecolor": "#ffe8e8", "edgecolor": "#cc0000", " lw": 1}
        if False else {"boxstyle": "round,pad=0.3", "facecolor": "#ffe8e8", "edgecolor": "#cc0000"},
    )
    fig.text(
        0.5, 0.5, "SIMULATED SAMPLE",
        ha="center", va="center", fontsize=34, fontweight="bold",
        color="#cc0000", alpha=0.10, rotation=22,
    )


def plot_convergence_comparison(rows, out_path, simulated=False, method_field="debug_convergence_s"):
    """Plot average convergence per test with min-max error bars and individual runs.

    Returns the saved Path. Handles missing tests or partial runs gracefully.
    """
    sim = is_simulated_rows(rows, force=simulated)
    summary = summarize_by_test(rows, field=method_field)

    fig, (ax_all, ax_fast) = plt.subplots(
        1, 2, figsize=(13, 5.6), gridspec_kw={"width_ratios": [1.25, 1.0]}
    )

    colors = {"T1": "#2b5c8f", "T2": "#3884b6", "T3": "#4aa3a2", "T4": "#5aa469", "T5": "#d95f02"}

    def draw_panel(ax, tests, title):
        xs = list(range(len(tests)))
        avgs, low_err, high_err, bar_colors = [], [], [], []
        for idx, t in enumerate(tests):
            s = summary[t]
            if s["avg"] is not None:
                avgs.append(s["avg"])
                low_err.append(max(0.0, s["avg"] - s["min"]))
                high_err.append(max(0.0, s["max"] - s["avg"]))
                bar_colors.append(colors.get(t, "#4c72b0"))
            else:
                avgs.append(0.0)
                low_err.append(0.0)
                high_err.append(0.0)
                bar_colors.append("#cccccc")

        bars = ax.bar(
            xs, avgs, width=0.58, color=bar_colors, edgecolor="#1f2937", linewidth=0.9,
            yerr=[low_err, high_err], capsize=5,
            error_kw={"elinewidth": 1.4, "ecolor": "#111827", "capthick": 1.4},
            label="Mean convergence time (min–max error bar)",
        )

        # Plot individual runs as scatter dots
        for idx, t in enumerate(tests):
            s = summary[t]
            run_items = sorted(s["runs"].items())
            for k, (run_no, val) in enumerate(run_items):
                jitter = (k - (len(run_items) - 1) / 2) * 0.11
                ax.scatter(
                    idx + jitter, val, color="#111827", facecolor="#ffffff",
                    edgecolor="#111827", s=38, zorder=4, linewidth=1.2,
                    label="Individual run (n=1..3)" if (idx == 0 and k == 0) else None,
                )
            if s["avg"] is not None:
                top = s["max"] if s["max"] is not None else s["avg"]
                ax.text(
                    idx, top + max(top * 0.04, 0.4),
                    f"{s['avg']:.1f}s\n(n={len(s['values'])})",
                    ha="center", va="bottom", fontsize=8.5, fontweight="bold", color="#111827",
                )
            else:
                ax.text(idx, 0.5, "no data", ha="center", va="bottom", fontsize=8.5,
                        style="italic", color="#6b7280")

        ax.set_xticks(xs)
        ax.set_xticklabels([f"{t}\n{_short_title(t)}" for t in tests], fontsize=8.5)
        ax.set_ylabel("Convergence time (seconds)", fontsize=10)
        ax.set_title(title, fontsize=10.5, fontweight="bold", pad=8)
        ax.grid(axis="y", linestyle="--", alpha=0.45)
        ax.set_axisbelow(True)
        ymax = max([summary[t]["max"] or 0.0 for t in tests] + [5.0])
        ax.set_ylim(0, ymax * 1.38)
        return bars

    draw_panel(ax_all, list(FAILURE_TESTS), "All Link-Failure Tests (T1–T5)")
    draw_panel(ax_fast, ["T1", "T2", "T3", "T4"], "Zoomed View: Direct Interface Changes (T1–T4)")

    # Reference timer lines on the full panel
    t5_max = summary["T5"]["max"] or 0
    if t5_max > 150:
        ax_all.axhline(180, color="#b91c1c", linestyle=":", linewidth=1.0, label="RIP invalid timer (180 s)")
        ax_all.axhline(240, color="#7c2d12", linestyle="--", linewidth=1.0, label="RIP flush timer (240 s)")
    ax_fast.axhline(30, color="#4b5563", linestyle=":", linewidth=1.0, label="RIP periodic update (30 s)")

    ax_all.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax_fast.legend(loc="upper right", fontsize=8, framealpha=0.9)

    tag = " [SIMULATED SAMPLE DATA]" if sim else ""
    fig.suptitle(f"RIPv2 Convergence Time by Test Scenario (Mean ± Min/Max Across Runs){tag}",
                 fontsize=12, fontweight="bold", y=0.94 if sim else 0.97)
    if sim:
        _stamp_simulated(fig)

    fig.tight_layout(rect=(0, 0, 1, 0.90 if sim else 0.93))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return out_path


def _short_title(test):
    return {
        "T1": "LIB–TCH\nserial down",
        "T2": "LIB–LAB\nGi0/1 down",
        "T3": "TCH–LAB\nserial down",
        "T4": "T1 link\nrecovery",
        "T5": "Silent peer\n(passive Gi0/1)",
    }.get(test, test)


def load_monitor_csv(path):
    """Read a monitor_convergence.py event CSV, skipping malformed lines."""
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "event" not in reader.fieldnames:
            return []
        for raw in reader:
            if not raw:
                continue
            t = _float(raw.get("t_rel_s"))
            if t is None and raw.get("obs_time"):
                try:
                    t = datetime.fromisoformat(raw["obs_time"]).timestamp()
                except ValueError:
                    t = None
            if t is None:
                continue
            rows.append({**raw, "_t": t})
    if rows and rows[0]["_t"] > 1e6:
        t0 = rows[0]["_t"]
        for r in rows:
            r["_t"] = round(r["_t"] - t0, 3)
    return rows


def load_polls_csv(event_csv_path):
    """Load the matching _polls.csv if present next to the event CSV."""
    p = Path(event_csv_path)
    polls_path = p.with_name(p.stem + "_polls.csv")
    if not polls_path.is_file():
        return []
    out = []
    with polls_path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "t_rel_s" not in reader.fieldnames:
            return []
        for raw in reader:
            t = _float(raw.get("t_rel_s"))
            if t is not None:
                out.append({**raw, "_t": t})
    return out


def _choose_target_network(rows, network=None):
    if network:
        return network
    # 1. Explicit target recorded in monitor_start detail or converged row
    for r in rows:
        if r.get("event") == "converged" and r.get("network"):
            return r["network"]
        if r.get("event") == "monitor_start":
            for part in (r.get("detail") or "").split(";"):
                if "target=" in part:
                    val = part.split("target=", 1)[1].strip()
                    if val and val != "all":
                        return val
    # 2. Prefer a 192.168.x.0/24 room LAN that changed over a 10.0.x.0/30 transit link
    change_evs = ("route_changed", "route_lost", "route_restored", "possibly_down")
    for r in rows:
        if r.get("event") in change_evs and (r.get("network") or "").startswith("192.168."):
            return r["network"]
    for r in rows:
        if r.get("event") in change_evs and r.get("network"):
            return r["network"]
    for r in rows:
        if (r.get("network") or "").startswith("192.168."):
            return r["network"]
    for r in rows:
        if r.get("network"):
            return r["network"]
    return "unknown"


def build_route_series(rows, network, polls=None):
    """Build step points (t, metric_display, status, next_hop) for `network`.

    metric_display is 1..15 for 'up', 16 for 'possibly_down', and 0 when missing
    from the routing table so disappearance is clearly visible on the chart.
    """
    points = []
    last_metric, last_status, last_nh = None, "missing", "-"

    for r in rows:
        ev = r.get("event", "")
        t = r["_t"]
        if r.get("network") == network and ev in (
            "baseline", "route_added", "route_restored", "route_changed", "possibly_down", "route_lost"
        ):
            if ev == "route_lost" or "not in routing table" in (r.get("detail") or ""):
                last_metric, last_status, last_nh = 0, "missing", "-"
            else:
                m = _float(r.get("metric_after"))
                st = (r.get("status_after") or "up").strip()
                nh = (r.get("next_hop_after") or "-").strip()
                if st == "possibly_down" or m == 16:
                    last_metric, last_status, last_nh = 16, "possibly_down", nh
                elif m is not None:
                    last_metric, last_status, last_nh = int(m), "up", nh
            points.append((t, last_metric, last_status, last_nh, ev))
        elif ev == "monitor_stop" and points:
            points.append((t, last_metric, last_status, last_nh, "end"))

    if not points and polls:
        for p in polls:
            points.append((p["_t"], 0, "missing", "-", "poll"))
    return points


def plot_route_timeline(event_csv_path, out_path=None, network=None, simulated=False):
    """Plot a route-change timeline (metric + state + key events) from one monitor CSV."""
    event_csv_path = Path(event_csv_path)
    rows = load_monitor_csv(event_csv_path)
    polls = load_polls_csv(event_csv_path)
    sim = is_simulated_rows(rows, force=simulated)
    router = next((r.get("router") for r in rows if r.get("router")), event_csv_path.stem.split("_")[-1])
    label = event_csv_path.stem.replace(f"_{router}", "")
    target = _choose_target_network(rows, network)
    points = build_route_series(rows, target, polls)

    fig, (ax_metric, ax_poll) = plt.subplots(
        2, 1, figsize=(11.5, 5.8), sharex=True, gridspec_kw={"height_ratios": [2.4, 1.0]}
    )

    if not points:
        ax_metric.text(0.5, 0.5, f"No valid route events in {event_csv_path.name}",
                       ha="center", va="center", transform=ax_metric.transAxes, fontsize=11)
    else:
        ts = [p[0] for p in points]
        # Map metric to a readable y scale: 0 = missing, 1 = 1 hop, 2 = 2 hops, 3.2 = possibly_down (m16)
        def y_map(m, st):
            if st == "missing" or m == 0:
                return 0.0
            if st == "possibly_down" or m >= 16:
                return 3.2
            return float(min(m, 3))

        ys = [y_map(p[1], p[2]) for p in points]
        ax_metric.step(ts, ys, where="post", color="#1d4ed8", linewidth=2.2, label=f"{target} state / metric")
        ax_metric.scatter(ts[:-1] if len(ts) > 1 else ts, ys[:-1] if len(ys) > 1 else ys,
                          color="#1d4ed8", s=34, zorder=4)

        # Annotate next-hop transitions
        last_label = None
        for t, m, st, nh, ev in points:
            if ev == "end":
                continue
            desc = "missing" if st == "missing" else (f"m16 hold-down\nvia {nh}" if st == "possibly_down"
                                                     else f"metric {m}\nvia {nh}")
            if desc != last_label:
                ax_metric.annotate(
                    desc, xy=(t, y_map(m, st)), xytext=(6, 10 if y_map(m, st) < 2.5 else -26),
                    textcoords="offset points", fontsize=8, fontweight="bold",
                    bbox={"boxstyle": "round,pad=0.22", "facecolor": "#f8fafc", "edgecolor": "#94a3b8"},
                )
                last_label = desc

    # Shade SSH session gaps and vertical lines for mark & converged
    down_t = None
    for r in rows:
        ev, t = r.get("event"), r["_t"]
        if ev == "mark":
            ax_metric.axvline(t, color="#dc2626", linestyle="--", linewidth=1.5, label=f"Operator mark ({t:.1f}s)")
            ax_poll.axvline(t, color="#dc2626", linestyle="--", linewidth=1.2)
        elif ev == "converged":
            ax_metric.axvline(t, color="#15803d", linestyle="-.", linewidth=1.6, label=f"Converged ({t:.1f}s)")
            ax_poll.axvline(t, color="#15803d", linestyle="-.", linewidth=1.2)
        elif ev == "session_lost":
            down_t = t
        elif ev == "session_restored" and down_t is not None:
            ax_metric.axvspan(down_t, t, color="#fecaca", alpha=0.45, label=f"SSH session gap ({t - down_t:.1f}s)")
            ax_poll.axvspan(down_t, t, color="#fecaca", alpha=0.45)
            down_t = None

    ax_metric.set_yticks([0.0, 1.0, 2.0, 3.2])
    ax_metric.set_yticklabels(["Missing\n(flushed)", "Metric 1\n(direct)", "Metric 2\n(alternate)", "Metric 16\n(possibly down)"])
    ax_metric.set_ylim(-0.45, 3.85)
    ax_metric.set_ylabel("Observed Route State", fontsize=10)
    ax_metric.grid(True, linestyle="--", alpha=0.4)
    handles, _ = ax_metric.get_legend_handles_labels()
    if handles:
        ax_metric.legend(loc="upper right", fontsize=8, framealpha=0.92)

    # Bottom panel: actual polling interval / command duration
    if polls:
        pt = [p["_t"] for p in polls]
        iv = [_float(p.get("interval_s")) or _float(p.get("duration_s")) or 0.0 for p in polls]
        dur = [_float(p.get("duration_s")) or 0.0 for p in polls]
        ax_poll.plot(pt, iv, color="#475569", marker=".", linewidth=1.1, label="Actual poll interval (s)")
        ax_poll.plot(pt, dur, color="#0f766e", linewidth=1.0, alpha=0.85, label="SSH command duration (s)")
        ax_poll.axhline(1.0, color="#94a3b8", linestyle=":", linewidth=1.0, label="Target interval (1.0 s)")
        ax_poll.legend(loc="upper right", fontsize=7.5)
    else:
        et = [r["_t"] for r in rows]
        ax_poll.scatter(et, [1.0] * len(et), color="#475569", s=18, label="Logged events")
        ax_poll.legend(loc="upper right", fontsize=7.5)

    ax_poll.set_xlabel("Elapsed observation time on laptop clock (seconds)", fontsize=10)
    ax_poll.set_ylabel("Poll timing (s)", fontsize=9.5)
    ax_poll.grid(True, linestyle="--", alpha=0.4)

    tag = " [SIMULATED SAMPLE DATA]" if sim else ""
    fig.suptitle(f"Route-Change Timeline: {label} on {router} (watching {target}){tag}",
                 fontsize=11.5, fontweight="bold", y=0.94 if sim else 0.97)
    if sim:
        _stamp_simulated(fig)

    fig.tight_layout(rect=(0, 0, 1, 0.90 if sim else 0.93))
    if out_path is None:
        prefix = "SIMULATED_" if sim else ""
        out_path = CHARTS_DIR / f"{prefix}timeline_{label}_{router}.png"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return out_path


def generate_all_charts(results_path=DATA_DIR / "results.csv",
                        debug_dir=DATA_DIR / "debug_logs",
                        monitor_dir=DATA_DIR / "monitor_logs",
                        charts_dir=CHARTS_DIR,
                        timeline_files=None,
                        simulated=False):
    """Build the comparison chart + timeline charts and return the list of saved PNG paths."""
    results_path = Path(results_path)
    charts_dir = Path(charts_dir)
    rows = load_results_csv(results_path)
    if not rows:
        rows = collect(debug_dir=debug_dir, monitor_dir=monitor_dir,
                       source="simulated" if simulated else "")
        if any(r.get("debug_convergence_s") != "" or r.get("monitor_converged_s") != "" for r in rows):
            write_results_csv(rows, results_path)

    sim = is_simulated_rows(rows, force=simulated)
    prefix = "SIMULATED_" if sim else ""
    saved = []

    comp_path = charts_dir / f"{prefix}convergence_comparison.png"
    saved.append(plot_convergence_comparison(rows, comp_path, simulated=sim))

    if timeline_files is None:
        # Pick one representative run per test if available (preferring T1-run1 and T5-run1)
        candidates = sorted(Path(monitor_dir).glob("*-run1_*.csv"))
        timeline_files = [p for p in candidates if not p.name.endswith("_polls.csv")]
    for csv_path in timeline_files:
        csv_path = Path(csv_path)
        if not csv_path.is_file():
            continue
        c_rows = load_monitor_csv(csv_path)
        c_sim = is_simulated_rows(c_rows, force=sim)
        c_prefix = "SIMULATED_" if c_sim else ""
        out = charts_dir / f"{c_prefix}timeline_{csv_path.stem}.png"
        saved.append(plot_route_timeline(csv_path, out, simulated=c_sim))
    return saved


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plot RIP convergence charts and route-change timelines.")
    parser.add_argument("--results", type=Path, help="path to results.csv")
    parser.add_argument("--debug-dir", type=Path, help="debug_logs directory")
    parser.add_argument("--monitor-dir", type=Path, help="monitor_logs directory")
    parser.add_argument("--charts-dir", type=Path, help="where to save PNG charts")
    parser.add_argument("--timeline", type=Path, nargs="*", help="specific monitor CSV file(s) to plot")
    parser.add_argument("--simulated", action="store_true",
                        help="use data/simulated/ and stamp every chart as SIMULATED SAMPLE DATA")
    args = parser.parse_args(argv)

    base = DATA_DIR / "simulated" if args.simulated else DATA_DIR
    results_path = args.results or (base / "results.csv")
    debug_dir = args.debug_dir or (base / "debug_logs")
    monitor_dir = args.monitor_dir or (base / "monitor_logs")
    charts_dir = args.charts_dir or (base / "charts" if args.simulated else CHARTS_DIR)

    saved = generate_all_charts(
        results_path=results_path, debug_dir=debug_dir, monitor_dir=monitor_dir,
        charts_dir=charts_dir, timeline_files=args.timeline, simulated=args.simulated,
    )
    for p in saved:
        print(f"Saved chart: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
