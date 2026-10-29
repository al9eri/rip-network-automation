"""
Tests for get_routes.parse_routes, run with:  pytest

The first three inputs are the healthy-state tables from Saif's Part 1 doc
(what every router should show with all links up). The rest are shaped like
real router output: legend, headers, route ages, and a route in hold-down.
"""

import pytest

from get_routes import parse_routes

# --- Saif's expected tables (no route ages) -------------------------------

LIB_R1 = """
R    10.0.23.0/30 [120/1] via 10.0.12.2, Serial0/0/0
                  [120/1] via 10.0.13.2, GigabitEthernet0/1
R    192.168.20.0/24 [120/1] via 10.0.12.2, Serial0/0/0
R    192.168.30.0/24 [120/1] via 10.0.13.2, GigabitEthernet0/1
"""

TCH_R2 = """
R    10.0.13.0/30 [120/1] via 10.0.12.1, Serial0/0/0
                  [120/1] via 10.0.23.2, Serial0/0/1
R    192.168.10.0/24 [120/1] via 10.0.12.1, Serial0/0/0
R    192.168.30.0/24 [120/1] via 10.0.23.2, Serial0/0/1
"""

LAB_R3 = """
R    10.0.12.0/30 [120/1] via 10.0.13.1, GigabitEthernet0/1
                  [120/1] via 10.0.23.1, Serial0/0/0
R    192.168.10.0/24 [120/1] via 10.0.13.1, GigabitEthernet0/1
R    192.168.20.0/24 [120/1] via 10.0.23.1, Serial0/0/0
"""


@pytest.mark.parametrize("text, own_lan", [
    (LIB_R1, "192.168.10.0/24"),
    (TCH_R2, "192.168.20.0/24"),
    (LAB_R3, "192.168.30.0/24"),
])
def test_baseline_tables(text, own_lan):
    routes = parse_routes(text)
    # 3 networks, the far WAN link has 2 equal-cost paths -> 4 entries
    assert len(routes) == 4
    # T0 expectation: every route is 1 hop and up
    assert all(r["metric"] == 1 and r["status"] == "up" for r in routes)
    # A router never learns its own LAN through RIP
    assert own_lan not in {r["network"] for r in routes}


def test_equal_cost_path_attached_to_network_above():
    routes = parse_routes(LIB_R1)
    wan = [r for r in routes if r["network"] == "10.0.23.0/30"]
    assert [(r["next_hop"], r["interface"]) for r in wan] == [
        ("10.0.12.2", "Serial0/0/0"),
        ("10.0.13.2", "GigabitEthernet0/1"),
    ]


# --- Real-style output ----------------------------------------------------

LEGEND = """Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area
       + - replicated route, % - next hop override

Gateway of last resort is not set

"""


def test_real_output_with_legend_headers_and_ages():
    text = LEGEND + """      10.0.0.0/8 is variably subnetted, 5 subnets, 2 masks
R        10.0.23.0/30 [120/1] via 10.0.12.2, 00:00:12, Serial0/0/0
                      [120/1] via 10.0.13.2, 00:00:05, GigabitEthernet0/1
R     192.168.20.0/24 [120/1] via 10.0.12.2, 00:00:12, Serial0/0/0
R     192.168.30.0/24 [120/1] via 10.0.13.2, 00:00:05, GigabitEthernet0/1
"""
    assert parse_routes(text) == parse_routes(LIB_R1)


def test_after_t1_route_moves_to_lab_r3_with_metric_2():
    # LIB-R1 after its serial link to TCH-R2 is shut
    text = LEGEND + """      10.0.0.0/8 is variably subnetted, 3 subnets, 2 masks
R        10.0.23.0/30 [120/1] via 10.0.13.2, 00:00:03, GigabitEthernet0/1
R     192.168.20.0/24 [120/2] via 10.0.13.2, 00:00:03, GigabitEthernet0/1
R     192.168.30.0/24 [120/1] via 10.0.13.2, 00:00:03, GigabitEthernet0/1
"""
    teachers = [r for r in parse_routes(text) if r["network"] == "192.168.20.0/24"]
    assert teachers == [{"network": "192.168.20.0/24", "metric": 2, "next_hop": "10.0.13.2",
                         "interface": "GigabitEthernet0/1", "status": "up"}]


@pytest.mark.parametrize("down_line", [
    # IOS 15 wraps it over two lines
    "R        192.168.30.0/24 is possibly down,\n          routing via 10.0.13.2, GigabitEthernet0/1",
    # older IOS keeps it on one line
    "R        192.168.30.0/24 is possibly down, routing via 10.0.13.2, GigabitEthernet0/1",
])
def test_possibly_down_route_is_recorded(down_line):
    # LIB-R1 during T5, after the invalid timer expired
    text = LEGEND + down_line + "\nR     192.168.20.0/24 [120/1] via 10.0.12.2, 00:00:21, Serial0/0/0\n"
    routes = parse_routes(text)
    assert routes[0] == {"network": "192.168.30.0/24", "metric": 16, "next_hop": "10.0.13.2",
                         "interface": "GigabitEthernet0/1", "status": "possibly_down"}
    assert routes[1]["network"] == "192.168.20.0/24"


def test_old_ios_subnets_without_prefix_length():
    text = """     10.0.0.0/30 is subnetted, 3 subnets
R       10.0.23.0 [120/1] via 10.0.12.2, 00:00:12, Serial0/0/0
"""
    assert parse_routes(text)[0]["network"] == "10.0.23.0/30"


def test_no_rip_routes():
    assert parse_routes(LEGEND) == []
    assert parse_routes("") == []
