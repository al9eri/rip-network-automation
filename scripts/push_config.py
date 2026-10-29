"""
push_config.py - apply configuration commands from a text file to routers.

The file holds the lines you would type after 'configure terminal', one per
line; blank lines and '!' comments are ignored. netmiko enters config mode,
sends the lines, leaves config mode, and then this script saves the running
config so the change survives a reload.

    python scripts/push_config.py configs/push/timestamps.txt all
    python scripts/push_config.py configs/push/rip-LAB-R3.txt LAB-R3
    python scripts/push_config.py configs/push/t5-start.txt LAB-R3 --no-save

Ready-made files are in configs/push/. Use --no-save for test changes
(like T5) so a router reload brings back the normal config.
"""

import argparse
from pathlib import Path

from inventory import ROUTERS, connect


def load_commands(path):
    """Read config lines from a file, dropping blank lines and '!' comments."""
    commands = []
    for line in Path(path).read_text().splitlines():
        line = line.rstrip()
        if line.strip() and not line.strip().startswith("!"):
            commands.append(line)
    return commands


def push(name, commands, save=True):
    """Send the commands to one router and return what the router echoed back."""
    with connect(name) as conn:
        # send_config_set wraps the list in 'configure terminal' ... 'end',
        # so the file must not contain those two lines itself.
        output = conn.send_config_set(commands)
        if save:
            # Same as 'write memory': copy running-config to startup-config.
            output += "\n" + conn.save_config()
    return output


def main():
    parser = argparse.ArgumentParser(description="Apply config lines from a file to routers.")
    parser.add_argument("file", help="text file with config-mode commands")
    parser.add_argument("routers", nargs="+", choices=[*ROUTERS, "all"], help="router names, or 'all'")
    parser.add_argument("--no-save", action="store_true", help="leave the change in running-config only")
    args = parser.parse_args()

    commands = load_commands(args.file)
    names = list(ROUTERS) if "all" in args.routers else args.routers
    print(f"{len(commands)} commands from {args.file}:")
    for c in commands:
        print(f"    {c}")

    for name in names:
        print(f"\n=== {name} ===")
        output = push(name, commands, save=not args.no_save)
        print(output)
        # IOS rejects a bad line with '% Invalid input', '% Incomplete
        # command' etc. but the session carries on, so look for it.
        if any(line.startswith("% ") for line in output.splitlines()):
            print(f"WARNING: {name} rejected at least one line, see the '%' messages above")


if __name__ == "__main__":
    main()
