"""Tests for compare_routes.diff_snapshots, using parsed versions of real-style tables."""

from compare_routes import diff_snapshots
from get_routes import parse_routes
from test_parse_routes import LIB_R1


def snap(label, **routers):
    return {"time": "2026-10-21T10:00:00", "label": label,
            "routers": {name.replace("_", "-"): routes for name, routes in routers.items()}}


# LIB-R1 after T1 (serial to TCH-R2 shut): Teachers' LAN now 2 hops via LAB-R3,
# and the far WAN link lost its path through the dead serial.
LIB_R1_AFTER_T1 = parse_routes("""
R    10.0.23.0/30 [120/1] via 10.0.13.2, GigabitEthernet0/1
R    192.168.20.0/24 [120/2] via 10.0.13.2, GigabitEthernet0/1
R    192.168.30.0/24 [120/1] via 10.0.13.2, GigabitEthernet0/1
""")


def test_identical_snapshots_have_no_changes():
    routes = parse_routes(LIB_R1)
    assert diff_snapshots(snap("a", LIB_R1=routes), snap("b", LIB_R1=routes)) == []


def test_t1_shows_next_hop_and_metric_change():
    changes = diff_snapshots(snap("T1-before", LIB_R1=parse_routes(LIB_R1)),
                             snap("T1-after", LIB_R1=LIB_R1_AFTER_T1))
    by_net = {c["network"]: c for c in changes}
    # The Lab LAN did not move, so it is not reported
    assert set(by_net) == {"10.0.23.0/30", "192.168.20.0/24"}

    teachers = by_net["192.168.20.0/24"]
    assert teachers["change"] == "changed"
    assert teachers["before"] == [("10.0.12.2", "Serial0/0/0", 1, "up")]
    assert teachers["after"] == [("10.0.13.2", "GigabitEthernet0/1", 2, "up")]

    # Losing one of two equal-cost paths is a change, not a removal
    assert by_net["10.0.23.0/30"]["change"] == "changed"
    assert len(by_net["10.0.23.0/30"]["after"]) == 1


def test_added_removed_and_possibly_down():
    before = parse_routes("R    192.168.30.0/24 [120/1] via 10.0.13.2, GigabitEthernet0/1\n")
    during_t5 = parse_routes(
        "R    192.168.30.0/24 is possibly down, routing via 10.0.13.2, GigabitEthernet0/1\n"
        "R    192.168.99.0/24 [120/3] via 10.0.12.2, Serial0/0/0\n")
    changes = {c["network"]: c["change"] for c in diff_snapshots(snap("a", LIB_R1=before), snap("b", LIB_R1=during_t5))}
    assert changes == {"192.168.30.0/24": "changed", "192.168.99.0/24": "added"}

    changes = {c["network"]: c["change"] for c in diff_snapshots(snap("b", LIB_R1=during_t5), snap("c", LIB_R1=[]))}
    assert changes == {"192.168.30.0/24": "removed", "192.168.99.0/24": "removed"}


def test_router_missing_from_one_snapshot():
    changes = diff_snapshots(snap("a", LIB_R1=[], TCH_R2=[]), snap("b", TCH_R2=[]))
    assert changes == [{"router": "LIB-R1", "network": None, "change": "not captured", "before": [], "after": []}]
