"""
collect_results.py - build data/results.csv and a Markdown table from the logs.

Reads every run of T1..T5 in data/debug_logs/ and data/monitor_logs/, calls
parse_rip_debug.convergence() on each debug capture, pulls the monitor's
first-change and converged times out of its summary/CSV, and writes one row
per run to data/results.csv.

    python scripts/collect_results.py
    python scripts/collect_results.py --ping T1-run1=3 T1-run2=2
    python scripts/collect_results.py --debug-dir data/simulated/debug_logs \\
        --monitor-dir data/simulated/monitor_logs --out data/simulated/results.csv --source simulated

Also prints the exact results table from the team plan (Run 1, Run 2, Run 3,
Average, Min, Max, Pings lost) so it can be pasted straight into Section 6a.
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

from experiments import EXPERIMENTS, FAILURE_TESTS, get_experiment, run_label
from parse_rip_debug import convergence, parse_debug, write_csv

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

RESULT_FIELDS = [
    "test", "run", "router", "network", "expected_metric",
    "debug_convergence_s", "debug_start", "debug_end",
    "monitor_first_change_s", "monitor_converged_s", "monitor_poll_interval_s",
    "session_gap_s", "pings_lost", "source", "notes",
]


def _float(val):
    if val in (None, ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def measure_debug_file(log_path, exp):
    """Parse one debug log, write its .csv if missing, and return convergence info."""
    if not log_path.is_file():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    events = parse_debug(text)
    csv_path = log_path.with_suffix(".csv")
    if not csv_path.exists():
        write_csv(events, csv_path)
    result = convergence(events, exp.network, exp.metric, exp.silent_peer or None)
    if result is None:
        return {"seconds": None, "start": "", "end": "", "note": "no convergence match in debug log"}
    return {
        "seconds": round(result["seconds"], 3),
        "start": result["start"]["time"].strftime("%H:%M:%S.%f")[:-3],
        "end": result["end"]["time"].strftime("%H:%M:%S.%f")[:-3],
        "note": "",
    }


def read_monitor_run(monitor_dir, label, router):
    """Read <label>_<router>_summary.json (or fall back to the event CSV)."""
    stem = f"{label}_{router}"
    summary_path = Path(monitor_dir) / f"{stem}_summary.json"
    csv_path = Path(monitor_dir) / f"{stem}.csv"
    if summary_path.is_file():
        try:
            s = json.loads(summary_path.read_text(encoding="utf-8"))
            first = _float(s.get("first_change") and s["first_change"].get("since_mark_s"))
            conv = _float(s.get("converged") and s["converged"].get("since_mark_s"))
            gaps = sum(_float(g.get("gap_s")) or 0.0 for g in s.get("session_gaps", []))
            return {
                "first_change_s": first,
                "converged_s": conv,
                "poll_interval_s": _float(s.get("poll_interval_mean_s")),
                "gap_s": round(gaps, 3) if gaps else None,
                "source": s.get("source") or "",
            }
        except Exception:
            pass
    if not csv_path.is_file():
        return None
    try:
        with csv_path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None
    mark_time = None
    first = conv = None
    for r in rows:
        if r.get("event") == "mark" and mark_time is None and r.get("obs_time"):
            try:
                mark_time = datetime.fromisoformat(r["obs_time"])
            except ValueError:
                pass
        if r.get("event") in ("route_lost", "route_changed", "route_restored", "route_added", "possibly_down"):
            if first is None:
                first = _float(r.get("since_mark_s"))
                if first is None and mark_time and r.get("obs_time"):
                    try:
                        first = round((datetime.fromisoformat(r["obs_time"]) - mark_time).total_seconds(), 3)
                    except ValueError:
                        pass
        if r.get("event") == "converged" and conv is None:
            conv = _float(r.get("since_mark_s"))
    return {"first_change_s": first, "converged_s": conv, "poll_interval_s": None, "gap_s": None, "source": ""}


def collect(debug_dir=DATA_DIR / "debug_logs", monitor_dir=DATA_DIR / "monitor_logs",
            tests=FAILURE_TESTS, runs=(1, 2, 3), pings=None, source=""):
    """Return a list of result dicts (one per test/run that has any data or is requested)."""
    pings = pings or {}
    rows = []
    for test in tests:
        exp = get_experiment(test)
        for run in runs:
            label = run_label(test, run)
            log_path = Path(debug_dir) / f"{label}_{exp.router}.txt"
            meta_path = Path(debug_dir) / f"{label}_{exp.router}.meta.json"
            dbg = measure_debug_file(log_path, exp)
            mon = read_monitor_run(monitor_dir, label, exp.router)
            meta_source = ""
            if meta_path.is_file():
                try:
                    meta_source = json.loads(meta_path.read_text(encoding="utf-8")).get("source", "")
                except Exception:
                    pass
            row_source = source or (mon and mon["source"]) or meta_source or ("measured" if (dbg or mon) else "")
            notes = []
            if dbg and dbg["note"]:
                notes.append(dbg["note"])
            if mon and mon["gap_s"]:
                notes.append(f"SSH gap {mon['gap_s']:g}s")
            ping_val = pings.get(label, pings.get(f"{test}-run{run}", ""))
            rows.append({
                "test": test,
                "run": run,
                "router": exp.router,
                "network": exp.network,
                "expected_metric": exp.metric,
                "debug_convergence_s": dbg["seconds"] if dbg and dbg["seconds"] is not None else "",
                "debug_start": dbg["start"] if dbg else "",
                "debug_end": dbg["end"] if dbg else "",
                "monitor_first_change_s": mon["first_change_s"] if mon and mon["first_change_s"] is not None else "",
                "monitor_converged_s": mon["converged_s"] if mon and mon["converged_s"] is not None else "",
                "monitor_poll_interval_s": mon["poll_interval_s"] if mon and mon["poll_interval_s"] is not None else "",
                "session_gap_s": mon["gap_s"] if mon and mon["gap_s"] is not None else "",
                "pings_lost": ping_val,
                "source": row_source,
                "notes": "; ".join(notes),
            })
    return rows


def write_results_csv(rows, path=DATA_DIR / "results.csv"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def load_results_csv(path):
    """Read a results.csv file, tolerating missing/malformed cells and extra whitespace."""
    path = Path(path)
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        rows = []
        for raw in reader:
            if not raw or not any((v or "").strip() for v in raw.values()):
                continue
            test = (raw.get("test") or "").strip().upper()
            if test not in EXPERIMENTS:
                continue
            rows.append({k: (raw.get(k) or "").strip() if isinstance(raw.get(k), str) else raw.get(k)
                         for k in RESULT_FIELDS})
        return rows


def summarize_by_test(rows, field="debug_convergence_s", fallback="monitor_converged_s"):
    """Compute per-test run values, average, min, max and average pings lost."""
    summary = {}
    for test in FAILURE_TESTS:
        t_rows = [r for r in rows if (r.get("test") or "").upper() == test]
        by_run = {}
        used_fallback = False
        for r in t_rows:
            run_no = _float(r.get("run"))
            if run_no is None:
                continue
            val = _float(r.get(field))
            if val is None and fallback:
                val = _float(r.get(fallback))
                if val is not None:
                    used_fallback = True
            if val is not None and val >= 0:
                by_run[int(run_no)] = val
        vals = list(by_run.values())
        pings = [_float(r.get("pings_lost")) for r in t_rows]
        pings = [p for p in pings if p is not None and p >= 0]
        sources = {(r.get("source") or "").lower() for r in t_rows if r.get("source")}
        summary[test] = {
            "test": test,
            "title": EXPERIMENTS[test].title,
            "runs": by_run,
            "values": vals,
            "avg": round(mean(vals), 2) if vals else None,
            "min": round(min(vals), 2) if vals else None,
            "max": round(max(vals), 2) if vals else None,
            "pings_avg": round(mean(pings), 1) if pings else None,
            "simulated": any("sim" in s for s in sources),
            "used_fallback": used_fallback,
        }
    return summary


def format_markdown_table(rows):
    """Return the Markdown table matching the team plan's Results table."""
    summary = summarize_by_test(rows)
    sim = any(v["simulated"] for v in summary.values())
    banner = "> **SIMULATED SAMPLE DATA — NOT FROM PHYSICAL ROUTERS**\n\n" if sim else ""
    lines = [
        banner + "| Test | Run 1 (s) | Run 2 (s) | Run 3 (s) | Average (s) | Min–Max (s) | Pings lost (avg) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for test in FAILURE_TESTS:
        s = summary[test]
        r1, r2, r3 = (f"{s['runs'][i]:.2f}" if i in s["runs"] else "—" for i in (1, 2, 3))
        avg = f"{s['avg']:.2f}" if s["avg"] is not None else "—"
        mm = f"{s['min']:.2f}–{s['max']:.2f}" if s["min"] is not None else "—"
        pl = f"{s['pings_avg']:.1f}" if s["pings_avg"] is not None else "—"
        lines.append(f"| {test} ({s['title']}) | {r1} | {r2} | {r3} | {avg} | {mm} | {pl} |")
    return "\n".join(lines)


def _parse_pings(pairs):
    out = {}
    for item in pairs or ():
        if "=" not in item:
            raise ValueError(f"expected LABEL=COUNT (e.g. T1-run1=2), got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = int(v.strip())
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build data/results.csv from debug and monitor logs.")
    parser.add_argument("--debug-dir", type=Path, default=DATA_DIR / "debug_logs")
    parser.add_argument("--monitor-dir", type=Path, default=DATA_DIR / "monitor_logs")
    parser.add_argument("--out", type=Path, default=DATA_DIR / "results.csv")
    parser.add_argument("--ping", nargs="*", help="lost ping counts, e.g. T1-run1=2 T1-run2=3")
    parser.add_argument("--source", default="", help="'measured' or 'simulated'")
    args = parser.parse_args(argv)

    rows = collect(args.debug_dir, args.monitor_dir, pings=_parse_pings(args.ping), source=args.source)
    write_results_csv(rows, args.out)
    print(f"Wrote {args.out}\n")
    print(format_markdown_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
