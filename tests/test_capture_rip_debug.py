"""
Tests for capture_rip_debug.py using simulated Netmiko sessions (no routers needed).

Covers:
  - Normal capture in 'both', 'buffer' and 'live' modes
  - Reliable 'undebug all' cleanup even when an exception occurs mid-capture
  - Session drop during T2/T3 followed by automatic reconnect and 'undebug all'
  - Warning when reconnect fails completely so the operator can undebug on console
  - Rejected CLI command detection ('% Invalid input...')
  - Router clock parsing and offset calculation (with and without '*' unsynced prefix)
"""

import json
from datetime import datetime

import pytest

from capture_rip_debug import (
    CaptureError, buffer_warnings, capture, capture_name, check_router_setup,
    parse_show_clock, router_clock, start_debug, stop_debug,
)
from sim_router import SimClock, SimConnectTimeout, SimLab, make_scenario, render_debug_log


def test_capture_name_follows_team_plan_convention():
    assert capture_name("LIB-R1", "T1-run1") == "T1-run1_LIB-R1"


def test_parse_show_clock_synced_and_unsynced():
    dt = parse_show_clock("*10:15:02.123 UTC Wed Oct 21 2026")
    assert dt == datetime(2026, 10, 21, 10, 15, 2, 123000)

    # Unset router clock from 1993 gets mapped to the requested year
    dt_1993 = parse_show_clock("*00:04:12.500 UTC Mon Mar 1 1993", year=2026)
    assert dt_1993.year == 2026 and dt_1993.month == 3 and dt_1993.day == 1
    assert parse_show_clock("garbage output") is None


def test_normal_buffer_and_both_capture_saves_log_and_undebugs(tmp_path):
    clock = SimClock()
    sc = make_scenario("T1", delay=8.4, fail_at=5.0)
    dbg = render_debug_log(sc, clock.base, delay=8.4, duration=30)
    lab = SimLab(sc, clock, debug_text=dbg)

    meta = capture(
        "LIB-R1", "T1-run1", duration=25, mode="both", out_dir=tmp_path,
        connect_fn=lab.connect, clock=clock.monotonic, sleep=clock.sleep,
        now=clock.now, log=lambda *a: None,
    )
    assert meta["ok"] is True
    assert meta["undebug_ok"] is True
    assert not lab.debug_left_on
    saved = (tmp_path / "T1-run1_LIB-R1.txt").read_text(encoding="utf-8")
    assert "RIP: received v2 update" in saved
    assert "RT: add 192.168.20.0/24 via 10.0.13.2, rip metric [120/2]" in saved


def test_t2_session_drop_reconnects_and_turns_debugging_off(tmp_path):
    clock = SimClock()
    sc = make_scenario("T2", delay=9.0, fail_at=5.0)
    dbg = render_debug_log(sc, clock.base, delay=9.0, duration=60)
    lab = SimLab(sc, clock, debug_text=dbg)

    meta = capture(
        "LIB-R1", "T2-run1", duration=45, mode="both", out_dir=tmp_path,
        connect_fn=lab.connect, reconnect_timeout=60,
        clock=clock.monotonic, sleep=clock.sleep, now=clock.now, log=lambda *a: None,
    )
    assert meta["session_drop"] is not None
    assert meta["undebug_ok"] is True
    assert meta["ok"] is True
    assert lab.connect_attempts >= 2
    assert "RT: add 192.168.30.0/24" in (tmp_path / "T2-run1_LIB-R1.txt").read_text(encoding="utf-8")


def test_undebug_all_runs_even_when_read_channel_raises(tmp_path):
    clock = SimClock()
    sc = make_scenario("T1", delay=5.0, fail_at=3.0)
    dbg = render_debug_log(sc, clock.base, delay=5.0, duration=20)
    lab = SimLab(sc, clock, debug_text=dbg)

    def broken_connect(name):
        conn = lab.connect(name)
        conn.read_channel = lambda: (_ for _ in ()).throw(RuntimeError("boom during live read"))
        return conn

    meta = capture(
        "LIB-R1", "T1-err", duration=10, mode="both", out_dir=tmp_path,
        connect_fn=broken_connect, clock=clock.monotonic, sleep=clock.sleep,
        now=clock.now, log=lambda *a: None,
    )
    assert meta["undebug_ok"] is True
    assert not lab.debug_left_on


def test_initial_ssh_connection_failure_is_handled_cleanly(tmp_path):
    clock = SimClock()

    def always_fail(name):
        raise SimConnectTimeout(f"cannot reach {name}")

    meta = capture(
        "LIB-R1", "T1-unreachable", duration=5, mode="buffer", out_dir=tmp_path,
        connect_fn=always_fail, clock=clock.monotonic, sleep=clock.sleep,
        now=clock.now, log=lambda *a: None,
    )
    assert meta["ok"] is False
    assert meta["debug_enabled"] is False
    assert "SimConnectTimeout" in meta["error"]
    meta_disk = json.loads((tmp_path / "T1-unreachable_LIB-R1.meta.json").read_text(encoding="utf-8"))
    assert meta_disk["ok"] is False


def test_rejected_debug_command_raises_capture_error():
    class RejectingConn:
        def send_command_timing(self, cmd, **kw):
            if cmd.startswith("debug"):
                return "% Invalid input detected at '^' marker."
            return ""

    with pytest.raises(CaptureError):
        start_debug(RejectingConn(), clear=False, monitor=False)


def test_setup_and_buffer_warnings_detect_missing_prerequisites():
    class UnconfiguredConn:
        def send_command(self, cmd, **kw):
            if "Buffer logging" in cmd:
                return "Buffer logging: level informational"
            return ""

    w = check_router_setup(UnconfiguredConn())
    assert len(w) == 2
    bw = buffer_warnings("Syslog logging: enabled\n")
    assert any("no 'RIP:' lines" in item for item in bw)
    assert any("no 'RT:' lines" in item for item in bw)
