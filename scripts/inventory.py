"""
inventory.py - the router list and one helper to log into any of them.

Every other script imports this file instead of repeating IP addresses and
passwords, so a change (new password, rebuilt lab) is made in one place.

    from inventory import connect, ROUTERS
    with connect("LIB-R1") as conn:
        print(conn.send_command("show ip route rip"))

Each router is reached on its LAN gateway address (G0/0). The link-failure
tests never touch G0/0, so every router stays reachable from AUTO-PC
(192.168.30.20) as long as RIP still has some path to its LAN.
"""

import os

from netmiko import ConnectHandler

# Defaults match Saif's configs. Environment variables override them, so
# different lab passwords never have to be typed into the code.
USERNAME = os.environ.get("RIP_USER", "admin")
PASSWORD = os.environ.get("RIP_PASS", "Admin@123")
SECRET = os.environ.get("RIP_SECRET", "class123")

# Name -> SSH address. Names match each router's hostname.
ROUTERS = {
    "LIB-R1": "192.168.10.1",  # Library
    "TCH-R2": "192.168.20.1",  # Teachers' room
    "LAB-R3": "192.168.30.1",  # Laboratory (same LAN as AUTO-PC)
}


def device_params(name):
    """Return the settings netmiko's ConnectHandler needs for one router."""
    if name not in ROUTERS:
        raise ValueError(f"unknown router {name!r}; expected one of {', '.join(ROUTERS)}")
    return {
        # Tells netmiko the CLI is Cisco IOS: which prompts to wait for,
        # how to turn off paging, how to enter config mode.
        "device_type": "cisco_ios",
        "host": ROUTERS[name],
        "username": USERNAME,
        "password": PASSWORD,
        "secret": SECRET,
        # Give up after 10 s instead of hanging, e.g. when a test has just
        # cut the path to this router.
        "conn_timeout": 10,
    }


def connect(name):
    """Open an SSH session to a router and return it in privileged (#) mode."""
    conn = ConnectHandler(**device_params(name))
    # 'admin' is privilege 15, so the login already lands at '#' and this
    # does nothing. It only matters if someone logs in with a lower-privilege
    # user, in which case it sends 'enable' and the secret.
    conn.enable()
    return conn
