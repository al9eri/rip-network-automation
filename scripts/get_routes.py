"""
get_routes.py - take a snapshot of the RIP routes on every router.

Logs into each router, runs 'show ip route rip', turns the text into a list
of routes, then saves:
  - data/snapshots/<time>_<label>.json  one file per snapshot (all routers)
  - data/routes.csv                     every snapshot, one row per route

    python scripts/get_routes.py T1-before
    python scripts/get_routes.py T1-after --routers LIB-R1 TCH-R2

parse_routes() is also imported by Zayed's monitor_convergence.py, which
calls it once a second to spot the moment a route changes.
"""

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

from inventory import ROUTERS, connect

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_FIELDS = ["time", "label", "router", "network", "metric", "next_hop", "interface", "status"]

# A RIP route line, e.g.
#   R     192.168.20.0/24 [120/1] via 10.0.12.2, 00:00:12, Serial0/0/0
# [120/1] is administrative distance / metric (hop count). The age
# (00:00:12, time since the last update) is optional: Saif's expected tables
# leave it out, real output has it.
ROUTE_RE = re.compile(
    r"^R\s+(?P<network>\d+\.\d+\.\d+\.\d+(?:/\d+)?)\s+"
    r"\[\d+/(?P<metric>\d+)\]\s+via\s+(?P<next_hop>[\d.]+),"
    r"(?:\s*[^,]+,)?\s*(?P<interface>\S+)"
)

# Second equal-cost path: same format with no 'R' and no network in front.
# It belongs to the network on the line above.
EXTRA_PATH_RE = re.compile(
    r"^\s+\[\d+/(?P<metric>\d+)\]\s+via\s+(?P<next_hop>[\d.]+),"
    r"(?:\s*[^,]+,)?\s*(?P<interface>\S+)"
)

# Route whose updates stopped: the invalid timer (180 s) expired, so the
# router advertises it as unreachable (metric 16) and holds it down until
# the flush timer (240 s) deletes it. T5 measures exactly this state.
POSSIBLY_DOWN_RE = re.compile(
    r"^R\s+(?P<network>\d+\.\d+\.\d+\.\d+(?:/\d+)?)\s+is possibly down,\s*"
    r"routing via\s+(?P<next_hop>[\d.]+),\s*(?P<interface>\S+)"
)

# Header line above a group of subnets, e.g. '10.0.0.0/30 is subnetted, 3 subnets'.
# Older IOS prints the subnets under it without their /30, so we keep the length.
SUBNETTED_RE = re.compile(r"^\s+\d+\.\d+\.\d+\.\d+/(?P<length>\d+) is subnetted")


def parse_routes(text):
    """Turn 'show ip route rip' output into a list of route dicts.

    Each route: {network, metric, next_hop, interface, status}, where status
    is 'up' or 'possibly_down'. A network with two equal-cost paths gives
    two entries with the same network and different next hops.
    """
    # IOS 15 wraps a possibly-down route over two lines; join them so one
    # regex handles both the wrapped and the single-line form.
    text = re.sub(r"is possibly down,\s*\n\s*", "is possibly down, ", text)

    routes = []
    last_network = None   # for attaching equal-cost second paths
    subnet_length = None  # from the last 'is subnetted' header

    for line in text.splitlines():
        header = SUBNETTED_RE.match(line)
        if header:
            subnet_length = header["length"]
            continue

        down = POSSIBLY_DOWN_RE.match(line)
        if down:
            network = _with_length(down["network"], subnet_length)
            routes.append(_route(network, 16, down["next_hop"], down["interface"], "possibly_down"))
            last_network = network
            continue

        route = ROUTE_RE.match(line)
        if route:
            network = _with_length(route["network"], subnet_length)
            routes.append(_route(network, route["metric"], route["next_hop"], route["interface"]))
            last_network = network
            continue

        extra = EXTRA_PATH_RE.match(line)
        if extra and last_network:
            routes.append(_route(last_network, extra["metric"], extra["next_hop"], extra["interface"]))
            continue

        # Anything else (code legend, 'Gateway of last resort', 'variably
        # subnetted' headers, blank lines) is not a route. Reset so a stray
        # indented line can never be attached to the wrong network.
        last_network = None

    return routes


def _with_length(network, subnet_length):
    """Add the prefix length when IOS left it off ('10.0.23.0' -> '10.0.23.0/30')."""
    if "/" in network or subnet_length is None:
        return network
    return f"{network}/{subnet_length}"


def _route(network, metric, next_hop, interface, status="up"):
    return {
        "network": network,
        "metric": int(metric),
        "next_hop": next_hop,
        "interface": interface,
        "status": status,
    }


def take_snapshot(label, names=None):
    """Read the RIP routes from each router; return the snapshot dict."""
    snapshot = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "routers": {},
    }
    for name in names or ROUTERS:
        try:
            with connect(name) as conn:
                output = conn.send_command("show ip route rip")
        except Exception as exc:
            # A router can be unreachable for a moment during a test (T2 cuts
            # the SSH path to LIB-R1). Skip it rather than lose the others.
            print(f"  {name}: not captured ({exc.__class__.__name__})")
            continue
        snapshot["routers"][name] = parse_routes(output)
        print(f"  {name}: {len(snapshot['routers'][name])} routes")
    return snapshot


def save_snapshot(snapshot):
    """Write the snapshot as JSON and append its routes to routes.csv."""
    stamp = snapshot["time"].replace(":", "").replace("-", "")  # ':' is not allowed in Windows file names
    json_path = DATA_DIR / "snapshots" / f"{stamp}_{snapshot['label']}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(snapshot, indent=2))

    csv_path = DATA_DIR / "routes.csv"
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        for router, routes in snapshot["routers"].items():
            for r in routes:
                writer.writerow({"time": snapshot["time"], "label": snapshot["label"], "router": router, **r})
    return json_path


def main():
    parser = argparse.ArgumentParser(description="Snapshot the RIP routes on every router.")
    parser.add_argument("label", nargs="?", default="manual", help="e.g. T0-baseline, T1-before, T1-after")
    parser.add_argument("--routers", nargs="+", choices=list(ROUTERS), help="only these routers")
    args = parser.parse_args()

    print(f"Snapshot '{args.label}':")
    path = save_snapshot(take_snapshot(args.label, args.routers))
    print(f"Saved {path.relative_to(DATA_DIR.parent)} and appended to data/routes.csv")


if __name__ == "__main__":
    main()
