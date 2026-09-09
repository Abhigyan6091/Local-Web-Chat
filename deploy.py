#!/usr/bin/env python3
"""
deploy.py — deploy the distributed chat cluster to the four lab containers
==========================================================================

Topology
--------
    Sys1  ssh 2237  172.17.0.38   Load balancer + chat UI   -> public :4237
    Sys2  ssh 2238  172.17.0.39   Backend + PostgreSQL      -> public :4238
    Sys3  ssh 2239  172.17.0.40   Backend                   -> public :4239
    Sys4  ssh 2240  172.17.0.41   Backend                   -> public :4240

Port mapping
------------
The lab host forwards a *fixed set* of internal ports out to the IIT Bhilai
network, and the external port keeps the container's SSH suffix:

    internal 3000/4000/5000/6000/7000  ->  external <prefix>237/238/239/240

So the load balancer listens on 4000 inside Sys1 and is reachable at
http://10.1.75.79:4237. Ports 8000/9000/8080 are NOT forwarded, which is why
the previous deployment on 8000 was unreachable from outside.

Usage
-----
    python deploy.py                 # full deploy (db check + backends + LB)
    python deploy.py --backends      # backends only
    python deploy.py --lb            # load balancer only
    python deploy.py --status        # health of every component
    python deploy.py --stop          # stop all services
    python deploy.py --logs Sys3     # tail a node's log
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko not installed. Run: pip install paramiko")
    sys.exit(1)

from lab_config import SSH_HOST, SSH_USER, SSH_PASSWORD as SSH_PASS, DB_PASSWORD, CHAT_SECRET

SERVICE_PORT = 4000          # internal port -> forwarded to <4><ssh suffix>
DB_PORT = 5432

MACHINES = {
    "Sys1": {"ssh": 2237, "ip": "172.17.0.38", "ext": 4237, "role": "loadbalancer"},
    "Sys2": {"ssh": 2238, "ip": "172.17.0.39", "ext": 4238, "role": "backend"},
    "Sys3": {"ssh": 2239, "ip": "172.17.0.40", "ext": 4239, "role": "backend"},
    "Sys4": {"ssh": 2240, "ip": "172.17.0.41", "ext": 4240, "role": "backend"},
}

BACKENDS = ["Sys2", "Sys3", "Sys4"]
DB_NODE = "Sys2"                       # PostgreSQL lives beside the Sys2 backend
DB_HOST = MACHINES[DB_NODE]["ip"]



# Switching threshold on the balancer's composite load score.
#
# Chosen from experiments/results/threshold_moderate.json and
# threshold_repeat.json. At 40 users throughput falls monotonically from
# 216 rps at 0.30 to 175 rps at 1.20 as the balancer over-sticks (switch count
# drops 1825 -> 37), and at 100 users the 0.30-0.55 band is the best of the
# repeated trials. 0.30 and 0.55 are statistically tied at both load levels;
# 0.55 is taken because it reaches the same throughput with fewer switches.
DEFAULT_THRESHOLD = 0.55

REMOTE_DIR = "/home/student/chatapp"
PUBLIC_URL = f"http://{SSH_HOST}:{MACHINES['Sys1']['ext']}"

HERE = Path(__file__).parent
UI_FILES = ["index.html", "style.css", "main.js", "websocket.js"]


# ── ssh helpers ──────────────────────────────────────────────────────────────
def connect(name: str) -> paramiko.SSHClient:
    cfg = MACHINES[name]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=SSH_HOST, port=cfg["ssh"], username=SSH_USER,
                   password=SSH_PASS, timeout=25, banner_timeout=25,
                   auth_timeout=25, allow_agent=False, look_for_keys=False)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 180) -> str:
    _, out, err = client.exec_command(cmd, timeout=timeout)
    stdout = out.read().decode("utf-8", "replace").strip()
    stderr = err.read().decode("utf-8", "replace").strip()
    return (stdout + ("\n" + stderr if stderr else "")).strip()


def sudo(client: paramiko.SSHClient, cmd: str, timeout: int = 300) -> str:
    quoted = "'" + cmd.replace("'", "'\"'\"'") + "'"
    return run(client, f"echo {SSH_PASS} | sudo -S -p '' bash -c {quoted}", timeout)


def put(sftp, local: Path, remote: str) -> None:
    sftp.put(str(local), remote)


def mkdirs(client: paramiko.SSHClient, *paths: str) -> None:
    run(client, "mkdir -p " + " ".join(paths))


def write_file(sftp, content: str, remote: str) -> None:
    sftp.putfo(io.StringIO(content), remote)


# ── supervised launcher ──────────────────────────────────────────────────────
SUPERVISOR = """#!/bin/bash
# Restarts the service if it ever exits, so a crash does not take the node out
# of the cluster for the rest of the evaluation window.
LOG="$1"; shift
while true; do
    "$@" >> "$LOG" 2>&1
    echo "[supervisor] service exited at $(date), restarting in 2s" >> "$LOG"
    sleep 2
done
"""


def start_supervised(client, sftp, name: str, log: str, command: str,
                     env: str = "") -> None:
    script = (f"#!/bin/bash\ncd {REMOTE_DIR}\n{env}\n"
              f"exec bash {REMOTE_DIR}/supervisor.sh {log} {command}\n")
    path = f"{REMOTE_DIR}/start_{name}.sh"
    write_file(sftp, script, path)
    run(client, f"chmod +x {path} {REMOTE_DIR}/supervisor.sh")
    run(client, f"setsid nohup bash {path} > /dev/null 2>&1 < /dev/null & disown; sleep 1")


def stop_services(client) -> None:
    """Stop every service on a node and wait for the port to be released.

    The patterns are bracketed ('[u]vicorn') so that pkill does not match the
    shell that is running this very command — an unbracketed `pkill -f uvicorn`
    kills its own parent shell and the remaining kills never execute, which
    leaves a stale process holding the port.
    """
    run(client,
        "pkill -f '[s]upervisor.sh' 2>/dev/null; "
        "pkill -f '[b]alancer.py' 2>/dev/null; "
        "pkill -f '[u]vicorn' 2>/dev/null; "
        "sleep 1; "
        # Anything still bound to the service port goes too.
        f"for pid in $(ss -tlnp 2>/dev/null | grep ':{SERVICE_PORT} ' "
        "| grep -oP 'pid=\\K[0-9]+' | sort -u); do kill -9 $pid 2>/dev/null; done; "
        "sleep 1", timeout=90)
    # Confirm the port is actually free before the caller starts a new service.
    for _ in range(10):
        holder = run(client, f"ss -tln 2>/dev/null | grep ':{SERVICE_PORT} ' | head -1")
        if not holder:
            return
        time.sleep(1)
    print(f"  WARNING: port {SERVICE_PORT} still bound after stop")


# ── database ─────────────────────────────────────────────────────────────────
def ensure_database() -> None:
    print(f"\n=== PostgreSQL on {DB_NODE} ({DB_HOST}) ===")
    client = connect(DB_NODE)
    status = run(client, "pg_lsclusters 2>/dev/null | tail -1")
    if "online" not in status:
        print("  cluster is down, starting...")
        print("  " + sudo(client, "pg_ctlcluster 16 main start 2>&1 || true"))
        time.sleep(3)
        status = run(client, "pg_lsclusters 2>/dev/null | tail -1")
    print(f"  {status}")
    probe = run(client, f"PGPASSWORD={DB_PASSWORD} psql -h 127.0.0.1 -U chatuser "
                        "-d chatdb -tAc 'select count(*) from messages;' 2>&1 | tail -1")
    print(f"  messages currently stored: {probe}")
    client.close()


# ── backends ─────────────────────────────────────────────────────────────────
def deploy_backend(name: str) -> None:
    cfg = MACHINES[name]
    print(f"\n=== Backend {name} ({cfg['ip']}:{SERVICE_PORT} -> public :{cfg['ext']}) ===")
    client = connect(name)
    sftp = client.open_sftp()

    stop_services(client)
    mkdirs(client, f"{REMOTE_DIR}/server", f"{REMOTE_DIR}/common")

    write_file(sftp, SUPERVISOR, f"{REMOTE_DIR}/supervisor.sh")
    for fname in ("main.py", "db_async.py", "crypto_utils.py"):
        put(sftp, HERE / "server" / fname, f"{REMOTE_DIR}/server/{fname}")
    put(sftp, HERE / "common" / "sysmetrics.py", f"{REMOTE_DIR}/common/sysmetrics.py")
    write_file(sftp, "", f"{REMOTE_DIR}/common/__init__.py")
    write_file(sftp, "", f"{REMOTE_DIR}/server/__init__.py")
    print("  uploaded server files")

    env = "\n".join([
        f"export NODE_ID={name}",
        f"export PORT={SERVICE_PORT}",
        f"export DB_HOST={DB_HOST}",
        f"export DB_PORT={DB_PORT}",
        "export DB_NAME=chatdb",
        "export DB_USER=chatuser",
        f"export DB_PASSWORD={DB_PASSWORD}",
        "export DB_POOL_MIN=4",
        "export DB_POOL_MAX=16",
        f"export CHAT_SECRET='{CHAT_SECRET}'",
        # Ceiling on the in-memory feed cache. Each cached message costs roughly
        # 1 KB across the item list, the concatenated body, the serialised
        # response and the gzip copy, so 80k caps the cache near 80 MB. That
        # matters most on Sys2, which shares its 512 MB with PostgreSQL.
        "export FEED_MAX_ITEMS=80000",
        "export PYTHONUNBUFFERED=1",
    ])
    # One worker per node: each container is limited to a single CPU, so extra
    # workers would only add context switching and duplicate feed caches.
    command = (f"python3 -m uvicorn server.main:asgi_app --host 0.0.0.0 "
               f"--port {SERVICE_PORT} --workers 1 --loop uvloop "
               f"--http httptools --log-level warning --no-access-log "
               f"--backlog 2048 --timeout-keep-alive 30")
    start_supervised(client, sftp, "backend", f"{REMOTE_DIR}/backend.log", command, env)

    time.sleep(5)
    health = run(client, f"curl -s -m 5 http://127.0.0.1:{SERVICE_PORT}/health")
    if '"status":"ok"' in health.replace(" ", ""):
        print(f"  UP  {health[:150]}")
    else:
        print(f"  NOT READY: {health[:200]}")
        print("  " + run(client, f"tail -n 15 {REMOTE_DIR}/backend.log"))
    sftp.close()
    client.close()


# ── load balancer ────────────────────────────────────────────────────────────
def deploy_loadbalancer(threshold: float = DEFAULT_THRESHOLD) -> None:
    cfg = MACHINES["Sys1"]
    print(f"\n=== Load Balancer Sys1 ({cfg['ip']}:{SERVICE_PORT} -> public :{cfg['ext']}) ===")
    client = connect("Sys1")
    sftp = client.open_sftp()

    stop_services(client)
    mkdirs(client, f"{REMOTE_DIR}/load_balancer", f"{REMOTE_DIR}/common",
           f"{REMOTE_DIR}/static")

    write_file(sftp, SUPERVISOR, f"{REMOTE_DIR}/supervisor.sh")
    for fname in ("balancer.py", "algorithms.py", "__init__.py"):
        local = HERE / "load_balancer" / fname
        if local.exists():
            put(sftp, local, f"{REMOTE_DIR}/load_balancer/{fname}")
    put(sftp, HERE / "common" / "sysmetrics.py", f"{REMOTE_DIR}/common/sysmetrics.py")
    write_file(sftp, "", f"{REMOTE_DIR}/common/__init__.py")
    for fname in UI_FILES:
        local = HERE / fname
        if local.exists():
            put(sftp, local, f"{REMOTE_DIR}/static/{fname}")
    print("  uploaded balancer + UI files")

    targets = ",".join(f"{n}=http://{MACHINES[n]['ip']}:{SERVICE_PORT}" for n in BACKENDS)
    command = (f"python3 load_balancer/balancer.py "
               f"--host 0.0.0.0 --port {SERVICE_PORT} "
               f"--backends {targets} "
               f"--algorithm adaptive_threshold "
               f"--threshold {threshold} "
               f"--static-dir {REMOTE_DIR}/static")
    start_supervised(client, sftp, "lb", f"{REMOTE_DIR}/lb.log", command,
                     "export PYTHONUNBUFFERED=1")

    time.sleep(4)
    status = run(client, f"curl -s -m 5 http://127.0.0.1:{SERVICE_PORT}/lb/health")
    if '"status"' in status:
        print(f"  UP  {status.replace(chr(10), ' ')[:160]}")
    else:
        print(f"  NOT READY: {status[:200]}")
        print("  " + run(client, f"tail -n 20 {REMOTE_DIR}/lb.log"))
    sftp.close()
    client.close()


# ── operations ───────────────────────────────────────────────────────────────
def check_status() -> None:
    print("\n=== Cluster status ===")
    for name, cfg in MACHINES.items():
        try:
            client = connect(name)
            path = "/lb/status" if cfg["role"] == "loadbalancer" else "/health"
            out = run(client, f"curl -s -m 5 http://127.0.0.1:{SERVICE_PORT}{path} | head -c 400")
            mem = run(client, "cat /sys/fs/cgroup/memory.current")
            up = '"status"' in out or '"service"' in out
            print(f"  {name} [{cfg['role']:>12}] {'UP  ' if up else 'DOWN'} "
                  f"mem={int(mem)//1048576 if mem.isdigit() else '?'}MB")
            if not up:
                print(f"      {out[:200]}")
            client.close()
        except Exception as exc:
            print(f"  {name}: SSH FAILED — {exc}")
    print(f"\n  Public load balancer URL : {PUBLIC_URL}")
    print(f"  Required routes          : {PUBLIC_URL}/message   {PUBLIC_URL}/feed")


def stop_all() -> None:
    print("\n=== Stopping services ===")
    for name in MACHINES:
        try:
            client = connect(name)
            stop_services(client)
            print(f"  {name}: stopped")
            client.close()
        except Exception as exc:
            print(f"  {name}: FAILED — {exc}")


def tail_logs(name: str, lines: int = 40) -> None:
    client = connect(name)
    log = "lb.log" if MACHINES[name]["role"] == "loadbalancer" else "backend.log"
    print(run(client, f"tail -n {lines} {REMOTE_DIR}/{log}"))
    client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Deploy the distributed chat cluster")
    ap.add_argument("--backends", action="store_true")
    ap.add_argument("--lb", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--logs", metavar="NODE")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()

    if args.status:
        return check_status()
    if args.stop:
        return stop_all()
    if args.logs:
        return tail_logs(args.logs)

    do_all = not (args.backends or args.lb)
    print(f"Deploying to {SSH_HOST} — internal port {SERVICE_PORT}, public {PUBLIC_URL}")

    if do_all or args.backends:
        ensure_database()
        for name in BACKENDS:
            try:
                deploy_backend(name)
            except Exception as exc:
                print(f"  FAILED {name}: {exc}")

    if do_all or args.lb:
        try:
            deploy_loadbalancer(args.threshold)
        except Exception as exc:
            print(f"  FAILED load balancer: {exc}")

    check_status()


if __name__ == "__main__":
    main()
