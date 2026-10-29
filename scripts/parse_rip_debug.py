"""
parse_rip_debug.py - turn a raw debug capture into events and a convergence time.

Reads a log saved by capture_rip_debug.py (output of 'debug ip rip' and
'debug ip routing' on one router) and finds four kinds of lines:
  - RIP updates the router sent or received, with the routes and metrics in them
  - interface up/down log messages (%LINK, %LINEPROTO)
  - routing-table changes (RT: add / RT: delete)
  - config changes (%SYS-5-CONFIG_I)
Every event is written to a CSV next to the log, one row per route.

    python scripts/parse_rip_debug.py data/debug_logs/T1-run1_LIB-R1.txt
    python scripts/parse_rip_debug.py data/debug_logs/T1-run1_LIB-R1.txt --network 192.168.20.0/24 --metric 2
    python scripts/parse_rip_debug.py data/debug_logs/T5-run1_LIB-R1.txt --network 192.168.30.0/24 --metric 2 --silent-peer 10.0.13.2

With --network and --metric it also measures convergence on this router:
  start = the first interface up/down line (T1 to T4), or, with --silent-peer,
          the last update heard from that neighbour (T5: nothing goes down, and
          RIP's invalid timer counts from that last update)
  end   = the moment the router installs the new route ('RT: add', from
          debug ip routing)
Both times come from the same router's clock, so unsynchronised clocks
between routers and the laptop do not matter.
"""

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

# '*Oct 21 10:15:02.123: ' from 'service timestamps debug datetime msec'.
# '*' means the router clock was never set; the year is optional.
TIMESTAMP_RE = re.compile(r"^[*.]?(?P<ts>[A-Z][a-z]{2}\s+\d+\s+(?:\d{4}\s+)?\d+:\d+:\d+(?:\.\d+)?):\s*")

# Update headers. 'flash' = triggered update, sent at once after a change
# instead of waiting for the 30 s periodic timer.
SENT_RE = re.compile(r"RIP: sending v2 (?P<flash>flash )?update to (?P<dest>\S+) via (?P<interface>\S+)")
RECEIVED_RE = re.compile(r"RIP: received v2 update from (?P<peer>\S+) on (?P<interface>\S+)")

# Route lines under a header:
#   sent:      10.0.13.0/30 via 0.0.0.0, metric 1, tag 0
#   received:  10.0.23.0/30 via 0.0.0.0 in 1 hops
# Metric 16 means unreachable (a poisoned route).
ENTRY_RE = re.compile(r"^\s*(?P<network>\d+\.\d+\.\d+\.\d+/\d+) via [\d.]+(?:, metric | in )(?P<metric>\d+)")

LINK_RE = re.compile(r"%(?:LINK|LINEPROTO)-\d-\w+: (?:Line protocol on )?Interface (?P<interface>[^,]+), "
                     r"changed state to (?P<state>.+)")
CONFIG_RE = re.compile(r"%SYS-5-CONFIG_I: (?P<detail>.+)")

# From 'debug ip routing': the exact moment the routing table changes.
ROUTE_ADD_RE = re.compile(r"RT: add (?P<network>[\d.]+/\d+) via (?P<next_hop>[\d.]+), rip metric \[\d+/(?P<metric>\d+)\]")
ROUTE_DEL_RE = re.compile(r"RT: del(?:ete)?\b.*?(?P<network>\d+\.\d+\.\d+\.\d+(?:/\d+)?)(?: via (?P<next_hop>[\d.]+))?")

CSV_FIELDS = ["time", "kind", "interface", "peer", "update", "network", "metric", "detail"]


def parse_time(ts):
    """'Oct 21 10:15:02.123' -> datetime. Routers often omit the year; assume this year."""
    ts = " ".join(ts.split())
    if not re.search(r"\b\d{4}\b", ts):
        ts = f"{datetime.now().year} {ts}"
        formats = ("%Y %b %d %H:%M:%S.%f", "%Y %b %d %H:%M:%S")
    else:
        formats = ("%b %d %Y %H:%M:%S.%f", "%b %d %Y %H:%M:%S")
    for fmt in formats:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            pass
    return None


def _event(time, kind, interface="", peer="", update="", routes=None, detail=""):
    return {"time": time, "kind": kind, "interface": interface, "peer": peer,
            "update": update, "routes": routes or [], "detail": detail}


def parse_debug(text):
    """Return the list of events found in a debug capture, in log order."""
    events = []
    current = None  # the last update header; route lines below it belong to it

    for raw in text.splitlines():
        stamp = TIMESTAMP_RE.match(raw)
        time = parse_time(stamp["ts"]) if stamp else None
        msg = raw[stamp.end():] if stamp else raw

        entry = ENTRY_RE.match(msg)
        if entry and current is not None:
            current["routes"].append((entry["network"], int(entry["metric"])))
            continue
        if "RIP: build" in msg:
            continue  # 'build update entries' sits between a header and its routes

        if m := SENT_RE.search(msg):
            current = _event(time, "sent", m["interface"], m["dest"], "flash" if m["flash"] else "periodic")
        elif m := RECEIVED_RE.search(msg):
            current = _event(time, "received", m["interface"], m["peer"])
        elif m := LINK_RE.search(msg):
            current = None
            events.append(_event(time, "link", m["interface"], detail=m["state"].strip()))
            continue
        elif m := CONFIG_RE.search(msg):
            current = None
            events.append(_event(time, "config", detail=m["detail"].strip()))
            continue
        elif m := ROUTE_ADD_RE.search(msg):
            current = None
            events.append(_event(time, "route_add", peer=m["next_hop"],
                                 routes=[(m["network"], int(m["metric"]))]))
            continue
        elif m := ROUTE_DEL_RE.search(msg):
            current = None
            events.append(_event(time, "route_delete", peer=m["next_hop"] or "", routes=[(m["network"], None)]))
            continue
        else:
            continue  # other debug noise; keep collecting routes for the current header
        events.append(current)

    return events


def convergence(events, network, metric, silent_peer=None):
    """Measure how long this router took to install `network` at `metric`.

    Returns {start, end, seconds} (start/end are events), or None with no match.
    """
    timed = [e for e in events if e["time"]]
    adds = [e for e in timed if e["kind"] == "route_add" and (network, metric) in e["routes"]]

    if silent_peer:
        # T5: the end is the first install of the new route; the start is
        # the last update heard from the neighbour that went silent.
        if not adds:
            return None
        end = adds[0]
        heard = [e for e in timed if e["kind"] == "received" and e["peer"] == silent_peer
                 and e["time"] <= end["time"]]
        if not heard:
            return None
        start = heard[-1]
    else:
        # T1 to T4: the start is the first interface change in the log.
        links = [e for e in timed if e["kind"] == "link"]
        if not links:
            return None
        start = links[0]
        after = [e for e in adds if e["time"] >= start["time"]]
        if not after:
            return None
        end = after[0]

    return {"start": start, "end": end, "seconds": (end["time"] - start["time"]).total_seconds()}


def write_csv(events, path):
    """One row per route (updates list several), one row for other events."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for e in events:
            base = {k: e[k] for k in ("kind", "interface", "peer", "update", "detail")}
            base["time"] = e["time"].isoformat(timespec="milliseconds") if e["time"] else ""
            for network, metric in e["routes"] or [("", "")]:
                writer.writerow({**base, "network": network, "metric": "" if metric is None else metric})


def describe(e):
    """One-line summary of an event for the printed timeline."""
    t = e["time"].strftime("%H:%M:%S.%f")[:-3] if e["time"] else "--:--:--.---"
    if e["kind"] in ("sent", "received"):
        arrow = "->" if e["kind"] == "sent" else "<-"
        routes = ", ".join(f"{n} m{m}" for n, m in e["routes"])
        return f"{t}  {e['kind']:12} {arrow} {e['peer']} on {e['interface']} {e['update']}: {routes}"
    if e["kind"] in ("route_add", "route_delete"):
        network, metric = e["routes"][0]
        via = f" via {e['peer']}" if e["peer"] else ""
        return f"{t}  {e['kind']:12} {network}{via}" + (f" metric {metric}" if metric is not None else "")
    return f"{t}  {e['kind']:12} {e['interface']} {e['detail']}".rstrip()


def main():
    parser = argparse.ArgumentParser(description="Parse a debug ip rip / debug ip routing capture.")
    parser.add_argument("log", type=Path, help="capture file from capture_rip_debug.py")
    parser.add_argument("--network", help="route to measure, e.g. 192.168.20.0/24")
    parser.add_argument("--metric", type=int, help="its metric once converged, e.g. 2")
    parser.add_argument("--silent-peer", help="T5: neighbour that stopped sending, e.g. 10.0.13.2")
    parser.add_argument("--all", action="store_true", help="print every update, not just the changes")
    args = parser.parse_args()

    events = parse_debug(args.log.read_text(errors="replace"))
    csv_path = args.log.with_suffix(".csv")
    write_csv(events, csv_path)

    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    print(f"{args.log.name}: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    print(f"Wrote {csv_path}\n")

    # Periodic updates repeat every 30 s; by default show only what changed:
    # interface events, config, routing-table changes and triggered updates.
    for e in events:
        if args.all or e["kind"] not in ("sent", "received") or e["update"] == "flash":
            print(describe(e))

    if args.network and args.metric is not None:
        result = convergence(events, args.network, args.metric, args.silent_peer)
        print()
        if result is None:
            print(f"No convergence found for {args.network} metric {args.metric}. "
                  "Is 'debug ip routing' in the capture, and the start event in the log?")
        else:
            print(f"start:  {describe(result['start'])}")
            print(f"end:    {describe(result['end'])}")
            print(f"Convergence for {args.network} at metric {args.metric}: {result['seconds']:.3f} s")


if __name__ == "__main__":
    main()
