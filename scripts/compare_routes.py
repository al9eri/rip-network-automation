"""
compare_routes.py - show what changed in the routing tables between two snapshots.

Takes two snapshots saved by get_routes.py (file paths, or just their labels,
which picks the newest file with that label) and prints, router by router,
every network that was added, removed or changed.

    python scripts/compare_routes.py T1-before T1-after
    python scripts/compare_routes.py data/snapshots/20261021T101502_T1-before.json data/snapshots/20261021T101610_T1-after.json

Routes are compared per network as a set of paths (next hop + metric +
status). Equal-cost routes have two paths, so losing one of them shows up
as a change instead of looking like the whole network vanished.
No routers needed: it only reads the JSON files.
"""

import argparse
import json
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "snapshots"


def load_snapshot(arg):
    """Load a snapshot from a file path, or the newest snapshot with that label."""
    path = Path(arg)
    if not path.is_file():
        # Snapshot names start with the time, so sorting by name = by time.
        matches = sorted(SNAPSHOT_DIR.glob(f"*_{arg}.json"))
        if not matches:
            raise SystemExit(f"no snapshot file or label {arg!r} in {SNAPSHOT_DIR}")
        path = matches[-1]
    return json.loads(path.read_text()), path


def paths_by_network(routes):
    """Group one router's routes: network -> set of (next_hop, interface, metric, status)."""
    grouped = {}
    for r in routes:
        grouped.setdefault(r["network"], set()).add(
            (r["next_hop"], r["interface"], r["metric"], r["status"]))
    return grouped


def diff_snapshots(before, after):
    """Return a list of changes: {router, network, change, before, after}.

    change is 'added', 'removed' or 'changed'; before/after are the sorted
    path tuples on each side (empty when the network is missing).
    """
    changes = []
    for router in sorted(set(before["routers"]) | set(after["routers"])):
        if router not in before["routers"] or router not in after["routers"]:
            # One side could not reach the router (see get_routes.py), so
            # there is nothing fair to compare.
            changes.append({"router": router, "network": None, "change": "not captured",
                            "before": [], "after": []})
            continue
        old = paths_by_network(before["routers"][router])
        new = paths_by_network(after["routers"][router])
        for network in sorted(set(old) | set(new)):
            if old.get(network) == new.get(network):
                continue  # identical paths: nothing happened to this network
            if network not in old:
                change = "added"
            elif network not in new:
                change = "removed"
            else:
                change = "changed"
            changes.append({"router": router, "network": network, "change": change,
                            "before": sorted(old.get(network, ())), "after": sorted(new.get(network, ()))})
    return changes


def describe(paths):
    """Readable form of a path set, e.g. 'via 10.0.13.2 (Gi0/1) metric 2'."""
    if not paths:
        return "-"
    parts = []
    for next_hop, interface, metric, status in paths:
        short = interface.replace("GigabitEthernet", "Gi").replace("Serial", "Se")
        flag = " POSSIBLY DOWN" if status == "possibly_down" else ""
        parts.append(f"via {next_hop} ({short}) metric {metric}{flag}")
    return " + ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Compare two routing-table snapshots.")
    parser.add_argument("before", help="snapshot file or label (e.g. T1-before)")
    parser.add_argument("after", help="snapshot file or label (e.g. T1-after)")
    args = parser.parse_args()

    before, before_path = load_snapshot(args.before)
    after, after_path = load_snapshot(args.after)
    print(f"before: {before_path.name}  ({before['time']})")
    print(f"after:  {after_path.name}  ({after['time']})\n")

    changes = diff_snapshots(before, after)
    if not changes:
        print("No route changes.")
        return

    current = None
    for c in changes:
        if c["router"] != current:
            current = c["router"]
            print(f"=== {current} ===")
        if c["change"] == "not captured":
            print("  not in both snapshots, skipped")
            continue
        print(f"  {c['change'].upper():8} {c['network']}")
        print(f"           before: {describe(c['before'])}")
        print(f"           after:  {describe(c['after'])}")


if __name__ == "__main__":
    main()
