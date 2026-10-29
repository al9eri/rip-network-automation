"""
Tests for monitor_convergence.py using simulated router sessions and virtual clocks.

Covers:
  - Normal baseline RIP operation (no spurious changes)
  - T1 link failure: route disappears, reappears on backup path with metric 1 -> 2
  - T4 route recovery: metric 2 -> 1
  - T5 silent failure: metric 1 -> possibly_down (metric 16) -> flushed -> metric 2
  - Equal-cost multi-path (ECMP): losing one of two paths is logged as paths/next_hop change
  - Missing routes at start and later appearance
  - Invalid / unexpected CLI output (empty string, '% Invalid input') ignored without false route_lost
  - Session drop and reconnect during T2/T3 with gap logged
  - Slow command execution (> 1 s) recorded accurately in poll intervals and overrun counter
"""

import csv
from datetime import timedelta

from monitor_convergence import (
    ADDED, CHANGED, LOST, POSSIBLY_DOWN, RESTORED, RouteMonitor,
    classify, diff_states, group_paths, output_looks_valid,
)
from sim_router import (
    BASELINE, LEGEND, R, SimClock, SimLab, make_scenario, render_route_table,
)


def test_output_validation_rejects_empty_and_ios_error_lines():
    assert not output_looks_valid("")
    assert not output_looks_valid("   \n  ")
    assert not output_looks_valid("% Invalid input detected at '^' marker.")
    assert output_looks_valid(LEGEND)  # valid 'show ip route rip' when no RIP routes exist
    assert output_looks_valid(render_route_table(BASELINE["LIB-R1"]))


def test_equal_cost_path_loss_is_classified_as_paths_and_next_hop_change():
    full = group_paths(BASELINE["LIB-R1"])
    one_left = group_paths([r for r in BASELINE["LIB-R1"]
                            if not (r["network"] == "10.0.23.0/30" and r["next_hop"] == "10.0.12.2")])
    changes = list(diff_states(full, one_left, seen=set(full)))
    assert len(changes) == 1
    net, event, what, old, new = changes[0]
    assert net == "10.0.23.0/30"
    assert event == CHANGED
    assert "next_hop" in what and "paths" in what
    assert len(old) == 2 and len(new) == 1


def test_route_disappearance_and_reappearance_classification():
    p1 = frozenset({("10.0.12.2", "Serial0/0/0", 1, "up")})
    p2 = frozenset({("10.0.13.2", "GigabitEthernet0/1", 2, "up")})
    assert classify(frozenset(), p1, seen_before=False) == (ADDED, "appeared")
    assert classify(p1, frozenset(), seen_before=True) == (LOST, "disappeared")
    assert classify(frozenset(), p2, seen_before=True) == (RESTORED, "appeared")


def test_normal_operation_logs_baseline_and_no_changes(tmp_path):
    clock = SimClock()
    sc = make_scenario("T1", delay=999, fail_at=999)  # nothing fails during window
    lab = SimLab(sc, clock)
    m = RouteMonitor(
        "LIB-R1", "T0-check", out_dir=tmp_path, duration=6,
        connect_fn=lab.connect, clock=clock.monotonic, now=clock.now,
        sleep=clock.sleep, log=lambda *a: None,
    )
    summary = m.run()
    assert summary["polls_ok"] >= 5
    assert summary["first_change"] is None


def test_t1_failure_detects_disappearance_metric_1_to_2_and_convergence(tmp_path):
    clock = SimClock()
    sc = make_scenario("T1", delay=8.0, fail_at=5.0)
    lab = SimLab(sc, clock)
    m = RouteMonitor(
        "LIB-R1", "T1-run1", target="192.168.20.0/24", out_dir=tmp_path,
        duration=55, expect_metric=2, expect_next_hop="10.0.13.2", stable_for=25.0,
        connect_fn=lab.connect, clock=clock.monotonic, now=clock.now,
        sleep=clock.sleep, log=lambda *a: None,
    )
    m.mark("shut S0/0/0", when=clock.base + timedelta(seconds=5.0))
    summary = m.run()

    assert summary["first_change"] is not None
    assert summary["converged"] is not None
    assert 7.5 <= summary["converged"]["since_mark_s"] <= 9.5

    with (tmp_path / "T1-run1_LIB-R1.csv").open(encoding="utf-8") as f:
        events = [r for r in csv.DictReader(f) if r["network"] == "192.168.20.0/24"]
    kinds = [r["event"] for r in events]
    assert "baseline" in kinds
    assert LOST in kinds
    assert RESTORED in kinds
    assert "converged" in kinds


def test_t4_recovery_switches_from_metric_2_back_to_1(tmp_path):
    clock = SimClock()
    sc = make_scenario("T4", delay=4.5, fail_at=5.0)
    lab = SimLab(sc, clock)
    m = RouteMonitor(
        "LIB-R1", "T4-run1", target="192.168.20.0/24", out_dir=tmp_path,
        duration=45, expect_metric=1, expect_next_hop="10.0.12.2", stable_for=20.0,
        connect_fn=lab.connect, clock=clock.monotonic, now=clock.now,
        sleep=clock.sleep, log=lambda *a: None,
    )
    m.mark("no shut S0/0/0", when=clock.base + timedelta(seconds=5.0))
    summary = m.run()
    assert summary["converged"] is not None
    assert 4.0 <= summary["converged"]["since_mark_s"] <= 6.0


def test_t5_silent_failure_captures_possibly_down_flush_and_metric_2(tmp_path):
    clock = SimClock()
    sc = make_scenario("T5", delay=252.0, fail_at=10.0)
    lab = SimLab(sc, clock)
    m = RouteMonitor(
        "LIB-R1", "T5-run1", target="192.168.30.0/24", out_dir=tmp_path,
        duration=310, expect_metric=2, expect_next_hop="10.0.12.2", stable_for=25.0,
        connect_fn=lab.connect, clock=clock.monotonic, now=clock.now,
        sleep=clock.sleep, log=lambda *a: None,
    )
    m.mark("passive G0/1", when=clock.base + timedelta(seconds=10.0))
    summary = m.run()

    with (tmp_path / "T5-run1_LIB-R1.csv").open(encoding="utf-8") as f:
        events = [r["event"] for r in csv.DictReader(f) if r["network"] == "192.168.30.0/24"]
    assert POSSIBLY_DOWN in events
    assert LOST in events
    assert RESTORED in events
    assert summary["converged"] is not None


def test_t2_session_drop_logs_gap_and_reconnects(tmp_path):
    clock = SimClock()
    sc = make_scenario("T2", delay=10.0, fail_at=5.0)
    lab = SimLab(sc, clock)
    m = RouteMonitor(
        "LIB-R1", "T2-run1", target="192.168.30.0/24", out_dir=tmp_path,
        duration=75, expect_metric=2, expect_next_hop="10.0.12.2", stable_for=20.0,
        connect_fn=lab.connect, clock=clock.monotonic, now=clock.now,
        sleep=clock.sleep, log=lambda *a: None,
    )
    m.mark("shut G0/1", when=clock.base + timedelta(seconds=5.0))
    summary = m.run()
    assert len(summary["session_gaps"]) >= 1
    assert summary["converged"] is not None


def test_invalid_command_output_does_not_cause_false_route_loss(tmp_path):
    clock = SimClock()
    sc = make_scenario("T1", delay=999, fail_at=999)
    lab = SimLab(sc, clock)
    calls = {"n": 0}

    def flaky_connect(name):
        conn = lab.connect(name)
        orig = conn.send_command

        def flaky_send(cmd, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                return ""  # empty glitch
            if calls["n"] == 4:
                return "% Invalid input detected at '^' marker."
            return orig(cmd, **kw)

        conn.send_command = flaky_send
        return conn

    m = RouteMonitor(
        "LIB-R1", "flaky", out_dir=tmp_path, duration=7,
        connect_fn=flaky_connect, clock=clock.monotonic, now=clock.now,
        sleep=clock.sleep, log=lambda *a: None,
    )
    summary = m.run()
    assert summary["polls_failed"] == 2
    assert summary["first_change"] is None  # no false route_lost!


def test_slow_command_execution_over_one_second_is_recorded(tmp_path):
    clock = SimClock()
    sc = make_scenario("T1", delay=999, fail_at=999)
    sc.latency = 1.65  # each 'show ip route rip' takes 1.65 s (> 1.0 s target)
    lab = SimLab(sc, clock)
    m = RouteMonitor(
        "LIB-R1", "slow-serial", out_dir=tmp_path, interval=1.0, duration=8,
        connect_fn=lab.connect, clock=clock.monotonic, now=clock.now,
        sleep=clock.sleep, log=lambda *a: None,
    )
    summary = m.run()
    assert summary["polls_slower_than_interval"] >= 3
    assert summary["poll_interval_mean_s"] >= 1.5
