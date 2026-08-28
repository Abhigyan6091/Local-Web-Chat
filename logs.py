#!/usr/bin/env python3
"""
logs.py - Live tail logs from all 4 remote lab machines simultaneously.

Usage:
    python logs.py          # Stream all machines
    python logs.py Sys1     # Stream only Load Balancer
    python logs.py Sys2     # Stream only Backend 1
"""

import sys
import threading
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko not installed. Run: pip install paramiko")
    sys.exit(1)

SSH_HOST = "10.1.75.79"
SSH_USER = "student"
SSH_PASS = "REDACTED"

MACHINES = {
    "Sys1 (LB) ": {"ssh_port": 2237, "color": "\033[93m", "log": "/home/student/loadbalancer/lb.log"},
    "Sys2 (BE1)": {"ssh_port": 2238, "color": "\033[94m", "log": "/home/student/chat-backend/backend.log"},
    "Sys3 (BE2)": {"ssh_port": 2239, "color": "\033[96m", "log": "/home/student/chat-backend/backend.log"},
    "Sys4 (BE3)": {"ssh_port": 2240, "color": "\033[92m", "log": "/home/student/chat-backend/backend.log"},
}
RESET = "\033[0m"


def tail_machine(name, cfg):
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=SSH_HOST, port=cfg["ssh_port"],
            username=SSH_USER, password=SSH_PASS,
            timeout=15, banner_timeout=15,
        )
        cmd = f"tail -f -n 10 {cfg['log']}"
        stdin, stdout, stderr = client.exec_command(cmd)
        for line in iter(stdout.readline, ""):
            if line:
                print(f"{cfg['color']}[{name}]{RESET} {line.strip()}", flush=True)
    except Exception as e:
        print(f"{cfg['color']}[{name}]{RESET} Connection closed: {e}", flush=True)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    threads = []

    print("\n" + "=" * 60)
    print("  Streaming Live Remote Cluster Logs (Ctrl+C to stop)")
    print("=" * 60 + "\n")

    for name, cfg in MACHINES.items():
        if target and target.lower() not in name.lower():
            continue
        t = threading.Thread(target=tail_machine, args=(name, cfg), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped streaming logs.")


if __name__ == "__main__":
    main()
