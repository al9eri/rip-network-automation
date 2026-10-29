"""
experiments.py - the T0 to T5 test definitions in one place.

Every value here comes from the team plan (v3), "Test plan" and "Handoff
from Mouhamed": which router is measured, which route is watched, the
metric it should end up with, and what the operator does to cause the
failure. capture_rip_debug.py, monitor_convergence.py, run_experiment.py
and collect_results.py all read this file, so a change is made once.

    from experiments import EXPERIMENTS, get_experiment
    t1 = get_experiment("T1")
    t1.router, t1.network, t1.metric   # 'LIB-R1', '192.168.20.0/24', 2

Nothing in this file talks to a router.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Experiment:
    test: str                 # 'T1'
    title: str                # short name for tables and charts
    router: str               # router whose clock/log/routing table is measured
    network: str = ""         # route that changes ('' for T0)
    metric: int = 0           # its metric once converged
    next_hop: str = ""        # its next hop once converged
    before_metric: int = 0    # its metric before the action (precondition check)
    silent_peer: str = ""     # T5 only: neighbour that stops sending updates
    duration: int = 60        # seconds to record (plan: 60 s, about 300 s for T5)
    pre_roll: int = 5         # seconds to record before the action
    action: tuple = ()        # what the operator does, line by line
    restore: tuple = ()       # how to undo it before the next run
    notes: tuple = field(default_factory=tuple)


EXPERIMENTS = {
    "T0": Experiment(
        test="T0", title="Baseline", router="LIB-R1",
        duration=35, pre_roll=0,
        action=("Nothing to do: all links up. Snapshot every router.",),
        notes=("Every remote LAN should be at metric 1 on every router.",),
    ),
    "T1": Experiment(
        test="T1", title="Library-Teachers serial down", router="LIB-R1",
        network="192.168.20.0/24", metric=2, next_hop="10.0.13.2", before_metric=1,
        action=("On LIB-R1:", "  configure terminal", "  interface Serial0/0/0", "  shutdown", "  end"),
        restore=("On LIB-R1:", "  configure terminal", "  interface Serial0/0/0", "  no shutdown", "  end",
                 "(or run T4 now: T4 IS this restore, measured)"),
        notes=("SSH from the LAB LAN to LIB-R1 uses G0/1, so the session stays up.",),
    ),
    "T2": Experiment(
        test="T2", title="Library-Lab Ethernet down", router="LIB-R1",
        network="192.168.30.0/24", metric=2, next_hop="10.0.12.2", before_metric=1,
        action=("From the LIB-R1 CONSOLE (not SSH):", "  configure terminal", "  interface GigabitEthernet0/1",
                "  shutdown", "  end"),
        restore=("From the LIB-R1 console:", "  configure terminal", "  interface GigabitEthernet0/1",
                 "  no shutdown", "  end"),
        notes=("A laptop on the LAB LAN reaches LIB-R1 over this link: the SSH session drops "
               "until RIP finds the path via TCH-R2. The monitor reconnects and logs the gap; "
               "the debug capture is read back from the log buffer afterwards.",),
    ),
    "T3": Experiment(
        test="T3", title="Teachers-Lab serial down", router="TCH-R2",
        network="192.168.30.0/24", metric=2, next_hop="10.0.12.1", before_metric=1,
        action=("From the TCH-R2 CONSOLE (recommended):", "  configure terminal", "  interface Serial0/0/1",
                "  shutdown", "  end"),
        restore=("On TCH-R2:", "  configure terminal", "  interface Serial0/0/1", "  no shutdown", "  end"),
        notes=("Not in the plan: a laptop on the LAB LAN reaches TCH-R2 over LAB-R3 S0/0/0 -> "
               "TCH-R2 S0/0/1, which is the link being shut. Expect the SSH session to TCH-R2 "
               "to drop like T2; the scripts handle it the same way.",),
    ),
    "T4": Experiment(
        test="T4", title="Recovery (T1 link back up)", router="LIB-R1",
        network="192.168.20.0/24", metric=1, next_hop="10.0.12.2", before_metric=2,
        action=("Precondition: the T1 link (LIB-R1 S0/0/0) is DOWN and has converged.",
                "On LIB-R1:", "  configure terminal", "  interface Serial0/0/0", "  no shutdown", "  end"),
        restore=("Nothing: the network is back to normal.",),
        notes=("Run it straight after each T1 run: T1-run1, T4-run1, T1-run2, T4-run2, ...",),
    ),
    "T5": Experiment(
        test="T5", title="Silent failure (passive G0/1)", router="LIB-R1",
        network="192.168.30.0/24", metric=2, next_hop="10.0.12.2", before_metric=1,
        silent_peer="10.0.13.2", duration=330, pre_roll=35,
        action=("Push the T5 change to LAB-R3 (does NOT save it):",
                "  python scripts/push_config.py configs/push/t5-start.txt LAB-R3 --no-save"),
        restore=("  python scripts/push_config.py configs/push/t5-stop.txt LAB-R3 --no-save",
                 "Then wait ~60 s and check LIB-R1 is back to metric 1 via 10.0.13.2."),
        notes=("Pre-roll is 35 s so at least one update from LAB-R3 (10.0.13.2) is in the log: "
               "parse_rip_debug.py measures T5 from the last update heard from it.",
               "Plan expects about 240-270 s (flush timer + TCH-R2's next update). Some IOS "
               "versions may install the metric-2 route earlier, after the 180 s invalid timer; "
               "record what actually happens.",),
    ),
}

FAILURE_TESTS = ("T1", "T2", "T3", "T4", "T5")


def get_experiment(test):
    """Return the Experiment for 'T1' (case-insensitive), or raise ValueError."""
    key = str(test).strip().upper()
    if key not in EXPERIMENTS:
        raise ValueError(f"unknown test {test!r}; expected one of {', '.join(EXPERIMENTS)}")
    return EXPERIMENTS[key]


def run_label(test, run):
    """'T1', 1 -> 'T1-run1', the naming the plan uses for captures."""
    return f"{get_experiment(test).test}-run{int(run)}"
