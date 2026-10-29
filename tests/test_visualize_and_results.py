"""
Tests for collect_results.py, visualize.py, experiments.py and run_experiment.py.

Covers:
  - Average, min and max convergence time calculations across 3 runs
  - Fallback from debug_convergence_s to monitor_converged_s when debug is missing
  - Handling of missing, empty, incomplete and malformed CSV files
  - PNG chart generation for convergence comparison and route-change timelines
  - Simulated data tagging ('SIMULATED_' prefix and watermark detection)
  - Precondition check in run_experiment.py (catching wrong before-state metric)
"""

import pytest

from collect_results import (
    format_markdown_table, load_results_csv, summarize_by_test, write_results_csv,
)
from experiments import EXPERIMENTS, get_experiment, run_label
from run_experiment import check_precondition
from sim_router import BASELINE
from visualize import (
    is_simulated_rows, load_monitor_csv, plot_convergence_comparison, plot_route_timeline,
)


def test_experiment_definitions_match_team_plan_v3():
    assert get_experiment("t1").router == "LIB-R1"
    assert get_experiment("T1").network == "192.168.20.0/24" and get_experiment("T1").metric == 2
    assert get_experiment("T2").network == "192.168.30.0/24" and get_experiment("T2").metric == 2
    assert get_experiment("T3").router == "TCH-R2" and get_experiment("T3").metric == 2
    assert get_experiment("T4").metric == 1 and get_experiment("T4").before_metric == 2
    assert get_experiment("T5").silent_peer == "10.0.13.2" and get_experiment("T5").duration >= 300
    assert run_label("T1", 2) == "T1-run2"
    with pytest.raises(ValueError):
        get_experiment("T9")


def test_summarize_by_test_computes_avg_min_max_and_handles_missing_runs():
    rows = [
        {"test": "T1", "run": "1", "debug_convergence_s": "10.0", "pings_lost": "2", "source": "simulated"},
        {"test": "T1", "run": "2", "debug_convergence_s": "20.0", "pings_lost": "4", "source": "simulated"},
        {"test": "T1", "run": "3", "debug_convergence_s": "15.0", "pings_lost": "3", "source": "simulated"},
        # T2 only has monitor fallback and 1 run, plus a malformed row
        {"test": "T2", "run": "1", "debug_convergence_s": "", "monitor_converged_s": "12.5", "pings_lost": "bad"},
        {"test": "T2", "run": "not_a_number", "debug_convergence_s": "abc"},
    ]
    s = summarize_by_test(rows)
    assert s["T1"]["avg"] == 15.0
    assert s["T1"]["min"] == 10.0
    assert s["T1"]["max"] == 20.0
    assert s["T1"]["pings_avg"] == 3.0
    assert s["T1"]["simulated"] is True

    assert s["T2"]["avg"] == 12.5
    assert s["T2"]["used_fallback"] is True
    assert s["T3"]["avg"] is None


def test_load_results_csv_handles_missing_and_corrupted_files(tmp_path):
    assert load_results_csv(tmp_path / "does_not_exist.csv") == []

    bad_csv = tmp_path / "corrupt.csv"
    bad_csv.write_text("not,a,valid,header\n1,2,3,4\n\n", encoding="utf-8")
    assert load_results_csv(bad_csv) == []

    mixed_csv = tmp_path / "mixed.csv"
    write_results_csv([
        {"test": "T1", "run": 1, "router": "LIB-R1", "network": "192.168.20.0/24",
         "expected_metric": 2, "debug_convergence_s": 8.5, "debug_start": "", "debug_end": "",
         "monitor_first_change_s": 1.0, "monitor_converged_s": 9.0, "monitor_poll_interval_s": 1.0,
         "session_gap_s": "", "pings_lost": 2, "source": "simulated", "notes": ""},
    ], mixed_csv)
    loaded = load_results_csv(mixed_csv)
    assert len(loaded) == 1 and float(loaded[0]["debug_convergence_s"]) == 8.5
    md = format_markdown_table(loaded)
    assert "SIMULATED SAMPLE DATA" in md
    assert "8.50" in md


def test_visualize_creates_png_charts_with_incomplete_and_malformed_data(tmp_path):
    rows = [
        {"test": "T1", "run": "1", "debug_convergence_s": "7.4", "source": "simulated"},
        {"test": "T1", "run": "2", "debug_convergence_s": "18.1", "source": "simulated"},
        {"test": "T5", "run": "1", "debug_convergence_s": "252.4", "source": "simulated"},
    ]
    comp = plot_convergence_comparison(rows, tmp_path / "SIMULATED_comp.png")
    assert comp.is_file() and comp.stat().st_size > 5000
    assert comp.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # Empty / malformed monitor CSV still produces a valid PNG without crashing
    empty_mon = tmp_path / "empty_LIB-R1.csv"
    empty_mon.write_text("obs_time,t_rel_s,event,network\nbad_time,not_float,baseline,192.168.20.0/24\n",
                         encoding="utf-8")
    assert load_monitor_csv(empty_mon) == []
    tl_empty = plot_route_timeline(empty_mon, tmp_path / "SIMULATED_empty_tl.png", simulated=True)
    assert tl_empty.is_file() and tl_empty.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_precondition_checker_detects_wrong_initial_state():
    healthy = {"routers": BASELINE}
    ok_t1, _ = check_precondition(healthy, get_experiment("T1"))
    assert ok_t1 is True

    # T4 requires T1 link to be down first (metric 2 on 192.168.20.0/24), so healthy fails
    ok_t4, msg = check_precondition(healthy, get_experiment("T4"))
    assert ok_t4 is False and "expected 2" in msg
