"""
sim_router.py - a pretend Cisco router for offline tests and SIMULATED sample data.

Nothing here talks to real equipment. It renders 'show ip route rip' and
'debug ip rip' / 'debug ip routing' text in IOS 15 style from a scenario,
and offers a fake netmiko session (SimConnection) driven by a virtual
clock, so monitor_convergence.py and capture_rip_debug.py can be run end
to end without routers and without waiting in real time.

Every value it produces is made up. Anything generated with it must be
labelled SIMULATED (simulate_data.py does this).
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

LEGEND = """Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2
       E1 - OSPF external type 1, E2 - OSPF external type 2
       i - IS-IS, su - IS-IS summary, L1 - IS-IS level-1, L2 - IS-IS level-2
       ia - IS-IS inter area, * - candidate default, U - per-user static route
       o - ODR, P - periodic downloaded static route, H - NHRP, l - LISP
       + - replicated route, % - next hop override

Gateway of last resort is not set

"""

G01, S000, S001 = "GigabitEthernet0/1", "Serial0/0/0", "Serial0/0/1"


def R(network, metric, next_hop, interface, status="up"):
    return {"network": network, "metric": metric, "next_hop": next_hop, "interface": interface, "status": status}


# Healthy tables, from Saif's Part 1 doc (same as tests/test_parse_routes.py).
BASELINE = {
    "LIB-R1": [R("10.0.23.0/30", 1, "10.0.12.2", S000), R("10.0.23.0/30", 1, "10.0.13.2", G01),
               R("192.168.20.0/24", 1, "10.0.12.2", S000), R("192.168.30.0/24", 1, "10.0.13.2", G01)],
    "TCH-R2": [R("10.0.13.0/30", 1, "10.0.12.1", S000), R("10.0.13.0/30", 1, "10.0.23.2", S001),
               R("192.168.10.0/24", 1, "10.0.12.1", S000), R("192.168.30.0/24", 1, "10.0.23.2", S001)],
    "LAB-R3": [R("10.0.12.0/30", 1, "10.0.13.1", G01), R("10.0.12.0/30", 1, "10.0.23.1", S000),
               R("192.168.10.0/24", 1, "10.0.13.1", G01), R("192.168.20.0/24", 1, "10.0.23.1", S000)],
}


def render_route_table(routes, legend=True, age="00:00:12"):
    """IOS 15 'show ip route rip' text for a list of route dicts (get_routes format)."""
    lines = [LEGEND.rstrip("\n"), ""] if legend else []
    tens = [r for r in routes if r["network"].startswith("10.")]
    others = [r for r in routes if not r["network"].startswith("10.")]
    if tens:
        lines.append(f"      10.0.0.0/8 is variably subnetted, {len({r['network'] for r in tens}) + 2} subnets, 2 masks")
    for group, indent in ((tens, "R        "), (others, "R     ")):
        last = None
        for r in group:
            if r["status"] == "possibly_down":
                lines.append(f"{indent}{r['network']} is possibly down,")
                lines.append(f"          routing via {r['next_hop']}, {r['interface']}")
                last = None
                continue
            path = f"[120/{r['metric']}] via {r['next_hop']}, {age}, {r['interface']}"
            if r["network"] == last:
                lines.append(" " * (len(indent) + len(r["network"]) + 1) + path)
            else:
                lines.append(f"{indent}{r['network']} {path}")
            last = r["network"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- scenarios

@dataclass
class Scenario:
    """How the measured router's table changes over (virtual) time.

    fail_at   seconds after the start when the operator applies the failure
    phases    list of (seconds after fail_at, routes) - the table from that time on
    unreachable  list of (start, end) seconds after fail_at when SSH to this router is cut
    """
    test: str
    router: str
    fail_at: float
    phases: list
    unreachable: list = field(default_factory=list)
    latency: float = 0.35          # seconds per 'show ip route rip'
    router_offset: float = -2.731  # router clock minus laptop clock

    def routes_at(self, t):
        current = BASELINE[self.router]
        for start, routes in self.phases:
            if t >= self.fail_at + start:
                current = routes
        return current

    def reachable_at(self, t):
        return not any(self.fail_at + a <= t < self.fail_at + b for a, b in self.unreachable)


def _drop(routes, network=None, next_hop=None):
    return [r for r in routes if not ((network is None or r["network"] == network)
                                      and (next_hop is None or r["next_hop"] == next_hop))]


def make_scenario(test, delay, fail_at=10.0, rng=None):
    """Build a plausible scenario. `delay` = seconds from failure to the new route being installed.

    For T5 `delay` is measured from the last update heard from LAB-R3.
    """
    rng = rng or random.Random(0)
    lib, tch = BASELINE["LIB-R1"], BASELINE["TCH-R2"]
    if test == "T1":
        gap = _drop(_drop(lib, "192.168.20.0/24"), "10.0.23.0/30", "10.0.12.2")
        after = gap + [R("192.168.20.0/24", 2, "10.0.13.2", G01)]
        return Scenario("T1", "LIB-R1", fail_at, [(0.05, gap), (delay, after)])
    if test == "T2":
        gap = _drop(_drop(lib, "192.168.30.0/24"), "10.0.23.0/30", "10.0.13.2")
        after = gap + [R("192.168.30.0/24", 2, "10.0.12.2", S000)]
        # The laptop on the LAB LAN loses LIB-R1 until LAB-R3 learns 192.168.10.0 via TCH-R2
        lost_for = max(delay, rng.uniform(3, 25)) + rng.uniform(1, 6)
        return Scenario("T2", "LIB-R1", fail_at, [(0.05, gap), (delay, after)], unreachable=[(0.0, lost_for)])
    if test == "T3":
        gap = _drop(_drop(tch, "192.168.30.0/24"), "10.0.13.0/30", "10.0.23.2")
        after = gap + [R("192.168.30.0/24", 2, "10.0.12.1", S000)]
        lost_for = max(delay, rng.uniform(3, 25)) + rng.uniform(1, 6)
        return Scenario("T3", "TCH-R2", fail_at, [(0.05, gap), (delay, after)], unreachable=[(0.0, lost_for)],
                        latency=0.9)
    if test == "T4":
        before = _drop(_drop(lib, "192.168.20.0/24"), "10.0.23.0/30", "10.0.12.2") + \
            [R("192.168.20.0/24", 2, "10.0.13.2", G01)]
        sc = Scenario("T4", "LIB-R1", fail_at, [(delay, lib)])
        sc.phases.insert(0, (-fail_at - 1, before))  # T1 state from the start
        return sc
    if test == "T5":
        # LAB-R3 goes silent; its last update reached LIB-R1 `last` s before the push.
        last = -rng.uniform(1, 29)
        invalid, flush = last + 180, last + 240
        down = [r for r in lib if r["network"] != "192.168.30.0/24"] + \
            [R("192.168.30.0/24", 16, "10.0.13.2", G01, "possibly_down")]
        down = _drop(down, "10.0.23.0/30", "10.0.13.2")
        gone = _drop(down, "192.168.30.0/24")
        after = gone + [R("192.168.30.0/24", 2, "10.0.12.2", S000)]
        install = last + delay
        sc = Scenario("T5", "LIB-R1", fail_at, [(invalid, down), (flush, gone), (install, after)])
        sc.last_update = last
        return sc
    raise ValueError(f"no scenario for {test}")


# ---------------------------------------------------------------- fake netmiko

class SimClock:
    """Virtual time: sleep() returns at once and moves the clock forward."""

    def __init__(self, base=datetime(2026, 10, 28, 10, 0, 0)):
        self.t = 0.0
        self.base = base

    def monotonic(self):
        return self.t

    def now(self):
        return self.base + timedelta(seconds=self.t)

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))


class SimReadTimeout(Exception):
    """Stands in for netmiko.exceptions.ReadTimeout."""


class SimConnectTimeout(Exception):
    """Stands in for netmiko.exceptions.NetmikoTimeoutException."""


class SimConnection:
    """Enough of a netmiko session for the scripts in this repo."""

    def __init__(self, scenario, clock, debug_text=""):
        self.sc, self.clock = scenario, clock
        self.debug_text = debug_text
        self.commands = []
        self.alive = True
        self.debug_on = False
        self.monitor_on = False

    def _check(self, read_timeout=10):
        if not self.alive:
            raise OSError("Socket is closed")
        if not self.sc.reachable_at(self.clock.t):
            self.clock.sleep(read_timeout)
            self.alive = False
            raise SimReadTimeout(f"Pattern not detected within read_timeout of {read_timeout}")

    def send_command(self, command, read_timeout=10, **kwargs):
        self.commands.append(command)
        self._check(read_timeout)
        self.clock.sleep(self.sc.latency)
        if command.startswith("show ip route"):
            return render_route_table(self.sc.routes_at(self.clock.t))
        if command == "show clock":
            rt = self.clock.now() + timedelta(seconds=self.sc.router_offset)
            return f"*{rt:%H:%M:%S}.{rt.microsecond // 1000:03d} UTC {rt:%a %b %d %Y}"
        if command == "show logging":
            return ("Syslog logging: enabled (0 messages dropped, 0 flushes, 0 overruns)\n"
                    "    Buffer logging:  level debugging, 120 messages logged, xml disabled,\n\n"
                    "Log Buffer (64000 bytes):\n" + self.debug_text)
        if "Buffer logging" in command:
            return "    Buffer logging:  level debugging, 120 messages logged, xml disabled,"
        if "service timestamps" in command:
            return "service timestamps debug datetime msec\nservice timestamps log datetime msec"
        return ""

    def send_command_timing(self, command, read_timeout=10, **kwargs):
        self.commands.append(command)
        self._check(read_timeout)
        self.clock.sleep(0.2)
        if command == "clear logging":
            return "Clear logging buffer [confirm]"
        if command.startswith("debug ip"):
            self.debug_on = True
            return f"{command.split()[-1].upper()} protocol debugging is on"
        if command == "undebug all":
            self.debug_on = False
            return "All possible debugging has been turned off"
        if command == "terminal monitor":
            self.monitor_on = True
        if command == "terminal no monitor":
            self.monitor_on = False
        return ""

    def read_channel(self):
        self._check(1)
        return ""

    def is_alive(self):
        return self.alive and self.sc.reachable_at(self.clock.t)

    def disconnect(self):
        self.alive = False


class SimLab:
    """connect_fn replacement: one SimConnection per login; refuses while unreachable."""

    def __init__(self, scenario, clock, debug_text=""):
        self.sc, self.clock, self.debug_text = scenario, clock, debug_text
        self.sessions = []
        self.connect_attempts = 0

    def connect(self, name):
        self.connect_attempts += 1
        if name != self.sc.router:
            raise ValueError(f"simulated lab only has {self.sc.router}")
        if not self.sc.reachable_at(self.clock.t):
            self.clock.sleep(10)  # inventory.py conn_timeout
            raise SimConnectTimeout(f"TCP connection to device failed ({name})")
        self.clock.sleep(1.5)  # SSH login
        conn = SimConnection(self.sc, self.clock, self.debug_text)
        self.sessions.append(conn)
        return conn

    @property
    def debug_left_on(self):
        return any(s.debug_on for s in self.sessions) and not any(
            "undebug all" in s.commands for s in self.sessions)


# ---------------------------------------------------------------- debug logs

def _rstamp(dt):
    return f"*{dt:%b} {dt.day:2d} {dt:%H:%M:%S}.{dt.microsecond // 1000:03d}: "


def render_debug_log(sc, clock_base, delay, duration, rng=None):
    """'show logging' body (debug ip rip + debug ip routing) for a scenario, router clock.

    Times are relative to the capture start (t=0 = laptop clock_base).
    """
    rng = rng or random.Random(1)
    rbase = clock_base + timedelta(seconds=sc.router_offset)
    lines = []

    def at(t, text):
        lines.append((t, _rstamp(rbase + timedelta(seconds=t)) + text))

    neighbours = {"LIB-R1": [("10.0.12.2", S000), ("10.0.13.2", G01)],
                  "TCH-R2": [("10.0.12.1", S000), ("10.0.23.2", S001)]}[sc.router]
    fail = sc.fail_at
    install = fail + delay if sc.test != "T5" else fail + sc.last_update + delay
    lost_peer = {"T1": "10.0.12.2", "T2": "10.0.13.2", "T3": "10.0.23.2", "T5": "10.0.13.2"}.get(sc.test)
    iface = {"T1": S000, "T2": G01, "T3": S001, "T4": S000}.get(sc.test)
    net, new_hop, new_metric = {
        "T1": ("192.168.20.0/24", "10.0.13.2", 2), "T2": ("192.168.30.0/24", "10.0.12.2", 2),
        "T3": ("192.168.30.0/24", "10.0.12.1", 2), "T4": ("192.168.20.0/24", "10.0.12.2", 1),
        "T5": ("192.168.30.0/24", "10.0.12.2", 2)}[sc.test]

    for peer, piface in neighbours:
        if sc.test == "T5" and peer == lost_peer:
            # Regular updates every ~30 s up to the last one (added below), then silence.
            t = fail + sc.last_update - 30
            while t >= 0:
                at(t, f"RIP: received v2 update from {peer} on {piface}")
                at(t, f"     {net} via 0.0.0.0 in 1 hops")
                t -= 30
            continue
        t = rng.uniform(0, 30) - 30
        while t < duration:
            t += 30 + rng.uniform(-1.5, 1.5)
            if t < 0 or t >= duration:
                continue
            down = sc.test in ("T1", "T2", "T3") and peer == lost_peer and t >= fail
            down = down or (sc.test == "T4" and peer == "10.0.12.2" and t < fail + delay - 0.01)
            if down:
                continue
            if abs(t - install) < 0.01:
                continue
            at(t, f"RIP: received v2 update from {peer} on {piface}")
            at(t, f"     {net} via 0.0.0.0 in {new_metric if peer == new_hop else 1} hops")
    if sc.test == "T5":
        t_last = fail + sc.last_update
        at(t_last, f"RIP: received v2 update from {lost_peer} on {G01}")
        at(t_last, f"     {net} via 0.0.0.0 in 1 hops")
        at(t_last + 180.0, f"RIP: sending v2 flash update to 224.0.0.9 via {S000} (10.0.12.1)")
        at(t_last + 180.0, f"\t{net} via 0.0.0.0, metric 16, tag 0")
        at(t_last + 240.0, f"RT: delete route to {net.split('/')[0]} via {lost_peer}, rip metric [120/1]")
    elif sc.test == "T4":
        at(fail, f"%LINK-3-UPDOWN: Interface {iface}, changed state to up")
        at(fail + 1.0, f"%LINEPROTO-5-UPDOWN: Line protocol on Interface {iface}, changed state to up")
    else:
        at(fail, f"%LINK-5-CHANGED: Interface {iface}, changed state to administratively down")
        at(fail + 0.009, f"RT: delete route to {net.split('/')[0]} via {lost_peer}, rip metric [120/1]")
        at(fail + 0.012, "RIP: sending v2 flash update to 224.0.0.9 via "
                         f"{[i for p, i in neighbours if p != lost_peer][0]}")
        at(fail + 0.012, f"\t{net} via 0.0.0.0, metric 16, tag 0")
        at(fail + 1.0, f"%LINEPROTO-5-UPDOWN: Line protocol on Interface {iface}, changed state to down")
    at(install, f"RIP: received v2 update from {new_hop} on {[i for p, i in neighbours if p == new_hop][0]}")
    at(install, f"     {net} via 0.0.0.0 in {new_metric} hops")
    at(install + 0.005, f"RT: add {net} via {new_hop}, rip metric [120/{new_metric}]")
    lines.sort(key=lambda x: x[0])
    return "\n".join(text for _, text in lines) + "\n"
