"""
Tests for parse_rip_debug, using captures shaped like real IOS output from
LIB-R1 with 'debug ip rip' + 'debug ip routing' and msec timestamps.
"""

from datetime import datetime

import pytest

from parse_rip_debug import convergence, parse_debug, parse_time, write_csv

# T1 on LIB-R1: S0/0/0 (to TCH-R2) is shut at 10:15:30. LAB-R3 has been
# advertising 192.168.20.0/24 at metric 2 all along (first update below),
# but LIB-R1 only installs it after the 1-hop route is gone.
T1_LOG = """\
*Oct 21 10:15:04.567: RIP: received v2 update from 10.0.13.2 on GigabitEthernet0/1
*Oct 21 10:15:04.567:      10.0.23.0/30 via 0.0.0.0 in 1 hops
*Oct 21 10:15:04.567:      192.168.20.0/24 via 0.0.0.0 in 2 hops
*Oct 21 10:15:04.567:      192.168.30.0/24 via 0.0.0.0 in 1 hops
*Oct 21 10:15:12.100: RIP: sending v2 update to 224.0.0.9 via Serial0/0/0 (10.0.12.1)
*Oct 21 10:15:12.100: RIP: build update entries
*Oct 21 10:15:12.100: \t10.0.13.0/30 via 0.0.0.0, metric 1, tag 0
*Oct 21 10:15:12.100: \t192.168.10.0/24 via 0.0.0.0, metric 1, tag 0
*Oct 21 10:15:30.001: %LINK-5-CHANGED: Interface Serial0/0/0, changed state to administratively down
*Oct 21 10:15:30.010: RT: delete route to 192.168.20.0 via 10.0.12.2, rip metric [120/1]
*Oct 21 10:15:30.012: RIP: sending v2 flash update to 224.0.0.9 via GigabitEthernet0/1 (10.0.13.1)
*Oct 21 10:15:30.012: RIP: build flash update entries
*Oct 21 10:15:30.012: \t192.168.20.0/24 via 0.0.0.0, metric 16, tag 0
*Oct 21 10:15:31.002: %LINEPROTO-5-UPDOWN: Line protocol on Interface Serial0/0/0, changed state to down
*Oct 21 10:15:34.890: RIP: received v2 update from 10.0.13.2 on GigabitEthernet0/1
*Oct 21 10:15:34.890:      10.0.23.0/30 via 0.0.0.0 in 1 hops
*Oct 21 10:15:34.890:      192.168.20.0/24 via 0.0.0.0 in 2 hops
*Oct 21 10:15:34.895: RT: add 192.168.20.0/24 via 10.0.13.2, rip metric [120/2]
"""

# T5 on LIB-R1: LAB-R3 (10.0.13.2) goes silent after its 10:00:05 update.
# 240 s later the flush timer removes the old route, and the metric-2 route
# from TCH-R2 is installed from TCH-R2's next update.
T5_LOG = """\
*Oct 21 10:00:05.000: RIP: received v2 update from 10.0.13.2 on GigabitEthernet0/1
*Oct 21 10:00:05.000:      192.168.30.0/24 via 0.0.0.0 in 1 hops
*Oct 21 10:00:20.000: RIP: received v2 update from 10.0.12.2 on Serial0/0/0
*Oct 21 10:00:20.000:      192.168.30.0/24 via 0.0.0.0 in 2 hops
*Oct 21 10:04:05.100: RT: delete route to 192.168.30.0 via 10.0.13.2, rip metric [120/1]
*Oct 21 10:04:20.300: RIP: received v2 update from 10.0.12.2 on Serial0/0/0
*Oct 21 10:04:20.300:      192.168.30.0/24 via 0.0.0.0 in 2 hops
*Oct 21 10:04:20.305: RT: add 192.168.30.0/24 via 10.0.12.2, rip metric [120/2]
"""


def kinds(events):
    return [e["kind"] for e in events]


def test_events_and_their_routes():
    events = parse_debug(T1_LOG)
    assert kinds(events) == ["received", "sent", "link", "route_delete", "sent", "link", "received", "route_add"]

    first = events[0]
    assert first["peer"] == "10.0.13.2" and first["interface"] == "GigabitEthernet0/1"
    assert first["routes"] == [("10.0.23.0/30", 1), ("192.168.20.0/24", 2), ("192.168.30.0/24", 1)]

    # 'build update entries' between header and routes does not break the grouping
    assert events[1]["routes"] == [("10.0.13.0/30", 1), ("192.168.10.0/24", 1)]
    assert events[1]["update"] == "periodic"


def test_triggered_update_poisons_the_lost_route():
    flash = [e for e in parse_debug(T1_LOG) if e["update"] == "flash"]
    # Right after the link goes down LIB-R1 tells LAB-R3 the route is now unreachable
    assert flash[0]["routes"] == [("192.168.20.0/24", 16)]


def test_t1_convergence_measured_from_link_down_to_route_install():
    result = convergence(parse_debug(T1_LOG), "192.168.20.0/24", 2)
    assert result["start"]["detail"] == "administratively down"
    assert result["end"]["kind"] == "route_add"
    # Not 10:15:04 (the update before the failure that already carried metric 2)
    assert result["seconds"] == pytest.approx(4.894)


def test_t5_convergence_measured_from_last_update_heard():
    result = convergence(parse_debug(T5_LOG), "192.168.30.0/24", 2, silent_peer="10.0.13.2")
    assert result["start"]["peer"] == "10.0.13.2"
    # flush at 240 s plus the wait for TCH-R2's next update
    assert result["seconds"] == pytest.approx(255.305)


def test_no_result_without_routing_table_lines():
    no_rt = "\n".join(line for line in T1_LOG.splitlines() if "RT:" not in line)
    assert convergence(parse_debug(no_rt), "192.168.20.0/24", 2) is None


def test_timestamps_with_and_without_year():
    assert parse_time("Oct 21 10:15:02.123") == datetime(datetime.now().year, 10, 21, 10, 15, 2, 123000)
    assert parse_time("Oct  1 2026 09:00:00.5") == datetime(2026, 10, 1, 9, 0, 0, 500000)


def test_csv_has_one_row_per_route(tmp_path):
    path = tmp_path / "t1.csv"
    write_csv(parse_debug(T1_LOG), path)
    rows = path.read_text().splitlines()
    assert rows[0] == "time,kind,interface,peer,update,network,metric,detail"
    # 3 + 2 + 1 + 1 + 1 + 1 + 2 + 1 routes/events
    assert len(rows) - 1 == 12
