"""Keeps a reverse SSH tunnel open so h3.umaya.tw's Caddy can reach this
PC's H3 Studio backend on port 8790. Connects to the VPS's public IP
directly (not the twstock.umaya.tw hostname) - this machine's LAN has a
USB-Ethernet bridge to the VPS that can silently break *.umaya.tw routing
while the public IP keeps working (see the vps-lan-usb-network memory).
Auto-reconnects if the tunnel drops.
"""
import subprocess
import sys
import time
from pathlib import Path

# pythonw.exe has no console of its own, so a plain subprocess.run(ssh_args)
# leaves the child ssh.exe (a console app) with no inherited console handle -
# Windows then allocates a brand-new, visible console window for it. Every
# reconnect (frequent on this flaky line) popped up a fresh window. Passing
# CREATE_NO_WINDOW plus explicit stdout/stderr file handles stops that.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LOG_PATH = Path(__file__).parent / "ssh_tunnel_boot.log"

VPS_HOST = "118.150.141.193"
VPS_PORT = "2222"
VPS_USER = "aj"
TUNNEL_PORT = 8790

SSH_ARGS = [
    "ssh",
    "-N",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=2",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-R", f"127.0.0.1:{TUNNEL_PORT}:127.0.0.1:{TUNNEL_PORT}",
    "-p", VPS_PORT,
    f"{VPS_USER}@{VPS_HOST}",
]

# This link (this PC <-> VPS) is known to be flaky: the SSH *control*
# channel's keepalives can keep responding even after the *forwarded*
# data channel has silently died, so ServerAlive alone never notices and
# the tunnel sits up-but-useless indefinitely. There's no cheap way to
# probe the forward's health from this side without another (slow, ~1min)
# SSH round-trip, so instead we just force a full reconnect on a fixed
# cadence - it bounds how long a silently-dead tunnel can stay undetected,
# at the cost of a short gap every cycle while it re-handshakes.
MAX_SESSION_SECONDS = 240

if __name__ == "__main__":
    # Launched via pythonw.exe (no console window) under Task Scheduler, so
    # there's no inherited stdout to redirect from the caller side - log to
    # a file directly instead.
    log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log

    while True:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] connecting tunnel...", flush=True)
        try:
            subprocess.run(SSH_ARGS, timeout=MAX_SESSION_SECONDS,
                            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] forcing reconnect after {MAX_SESSION_SECONDS}s "
                  f"(guards against a silently-dead forward that ServerAlive won't catch)", flush=True)
        except Exception as e:
            print(f"ssh process error: {e}", flush=True)
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] tunnel dropped, reconnecting in 5s", flush=True)
        time.sleep(5)
