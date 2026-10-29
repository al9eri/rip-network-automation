"""
connect_test.py - check that Python can SSH into every router.

Run this first, before any other script or test. For each router it logs
in, reads the prompt and prints the hostname, so a wrong IP, a missing SSH
config or a bad password shows up here instead of in the middle of a test.

    python scripts/connect_test.py

Exit code 0 means all routers answered, 1 means at least one failed.
"""

import sys
import time

from inventory import ROUTERS, connect


def check(name):
    """Log into one router; return its hostname, or raise if it fails."""
    with connect(name) as conn:
        # The prompt is 'LIB-R1#': hostname plus '#' for privileged mode.
        # Getting it back proves login, auth and enable mode all worked.
        return conn.find_prompt().rstrip("#>")


def main():
    failed = 0
    for name, host in ROUTERS.items():
        start = time.monotonic()
        try:
            hostname = check(name)
        except Exception as exc:
            # Common causes: timeout = no route to the router or SSH not
            # enabled; authentication error = wrong user/password.
            print(f"FAIL  {name:7} {host:13} {exc.__class__.__name__}: {exc}".splitlines()[0])
            failed += 1
            continue
        took = time.monotonic() - start
        # The hostname must match the name we used; if not, the IP in
        # inventory.py points at the wrong router.
        note = "" if hostname == name else f"  (expected {name}!)"
        print(f"OK    {name:7} {host:13} prompt {hostname}  {took:.1f} s{note}")

    print(f"\n{len(ROUTERS) - failed}/{len(ROUTERS)} routers reachable over SSH")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
