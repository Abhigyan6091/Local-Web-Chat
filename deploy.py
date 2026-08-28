#!/usr/bin/env python3
"""
deploy.py - Deploy Local-Web-Chat to 4-machine distributed lab

Usage:
    python deploy.py              # Deploy everything
    python deploy.py --backends   # Only deploy backends (Sys2/3/4)
    python deploy.py --lb         # Only deploy load balancer (Sys1)
    python deploy.py --status     # Check if services are running
    python deploy.py --stop       # Stop all remote services
"""

import os
import sys
import time
import argparse
import io
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko not installed. Run: pip install paramiko")
    sys.exit(1)

# Lab Configuration
SSH_HOST = "10.1.75.79"
SSH_USER = "student"
SSH_PASS = "REDACTED"

MACHINES = {
    "Sys1": {"ssh_port": 2237, "internal_ip": "172.17.0.38", "role": "loadbalancer"},
    "Sys2": {"ssh_port": 2238, "internal_ip": "172.17.0.39", "role": "backend"},
    "Sys3": {"ssh_port": 2239, "internal_ip": "172.17.0.40", "role": "backend"},
    "Sys4": {"ssh_port": 2240, "internal_ip": "172.17.0.41", "role": "backend"},
}

BACKEND_MACHINES = ["Sys2", "Sys3", "Sys4"]
BACKEND_INTERNAL_IPS = [MACHINES[s]["internal_ip"] for s in BACKEND_MACHINES]

REMOTE_BACKEND_DIR = "/home/student/chat-backend"
REMOTE_LB_DIR = "/home/student/loadbalancer"

HERE = Path(__file__).parent
SERVER_DIR = HERE / "server"
LB_DIR = HERE / "load_balancer"


def make_ssh(sys_name):
    cfg = MACHINES[sys_name]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"  Connecting to {sys_name} (ssh_port {cfg['ssh_port']})...", end=" ", flush=True)
    client.connect(
        hostname=SSH_HOST, port=cfg["ssh_port"],
        username=SSH_USER, password=SSH_PASS,
        timeout=20, banner_timeout=20,
    )
    print("OK")
    return client


def run_remote(client, cmd, ignore_errors=False):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=90)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if err and not ignore_errors:
        if any(w in err.lower() for w in ["error", "failed", "fatal", "traceback"]):
            print(f"    [STDERR] {err[:200]}")
    return out


def kill_remote_services(client, role):
    if role == "backend":
        run_remote(client, "pkill -f 'uvicorn server.main' 2>/dev/null; sleep 1", ignore_errors=True)
        run_remote(client, "pkill -f uvicorn 2>/dev/null", ignore_errors=True)
    elif role == "loadbalancer":
        run_remote(client, "pkill -f 'balancer.py' 2>/dev/null", ignore_errors=True)


def check_python(client):
    out = run_remote(client, "which python3 || which python", ignore_errors=True)
    if not out:
        raise RuntimeError("No Python found on remote machine!")
    return out.split("\n")[0].strip()


def deploy_backend(sys_name):
    print(f"\n{'='*55}")
    print(f"  Deploying BACKEND to {sys_name}")
    print(f"{'='*55}")

    client = make_ssh(sys_name)
    sftp = client.open_sftp()

    kill_remote_services(client, "backend")
    run_remote(client, f"mkdir -p {REMOTE_BACKEND_DIR}/server")

    backend_files = ["main.py", "database.py", "crypto_utils.py", "requirements.txt", "secret.key"]
    print("  Uploading server files...")
    for fname in backend_files:
        local_f = SERVER_DIR / fname
        if local_f.exists():
            sftp.put(str(local_f), f"{REMOTE_BACKEND_DIR}/server/{fname}")
            print(f"    [OK] {fname}")
        else:
            print(f"    [SKIP] {fname} (not found locally)")

    # Ensure requirements.txt exists remotely
    run_remote(
        client,
        f"test -f {REMOTE_BACKEND_DIR}/server/requirements.txt || "
        f"echo 'fastapi\\nuvicorn[standard]\\ncryptography\\npython-multipart' "
        f"> {REMOTE_BACKEND_DIR}/server/requirements.txt"
    )

    python_bin = check_python(client)

    print("  Installing pip dependencies...", end=" ", flush=True)
    pip_out = run_remote(
        client,
        f"cd {REMOTE_BACKEND_DIR} && "
        f"{python_bin} -m pip install -r server/requirements.txt -q --break-system-packages 2>&1 || "
        f"{python_bin} -m pip install -r server/requirements.txt -q 2>&1",
        ignore_errors=True,
    )
    print("OK" if "error" not in pip_out.lower() else f"WARN ({pip_out[:80]})")

    # Write startup script
    startup = (
        f"#!/bin/bash\n"
        f"cd {REMOTE_BACKEND_DIR}\n"
        f"export BACKEND_PORT=8000\n"
        f"nohup {python_bin} -m uvicorn server.main:app --host 0.0.0.0 --port 8000 "
        f"> {REMOTE_BACKEND_DIR}/backend.log 2>&1 &\n"
        f"echo $! > {REMOTE_BACKEND_DIR}/backend.pid\n"
        f"echo Backend started with PID $(cat {REMOTE_BACKEND_DIR}/backend.pid)\n"
    )
    sftp.putfo(io.StringIO(startup), f"{REMOTE_BACKEND_DIR}/start_backend.sh")
    run_remote(client, f"chmod +x {REMOTE_BACKEND_DIR}/start_backend.sh")

    print("  Starting backend...", end=" ", flush=True)
    out = run_remote(client, f"bash {REMOTE_BACKEND_DIR}/start_backend.sh")
    print(out if out else "launched")

    time.sleep(3)
    health = run_remote(client, "curl -s http://127.0.0.1:8000/health 2>/dev/null || echo NORESPONSE", ignore_errors=True)
    if '"status"' in health or '"ok"' in health:
        print(f"  [HEALTHY] {sys_name} backend is UP: {health}")
    else:
        print(f"  [STARTING] {sys_name} still starting - check: tail -f {REMOTE_BACKEND_DIR}/backend.log")
        log_out = run_remote(client, f"tail -n 10 {REMOTE_BACKEND_DIR}/backend.log 2>/dev/null", ignore_errors=True)
        print(f"  [LOG]: {log_out}")

    sftp.close()
    client.close()


def deploy_loadbalancer():
    sys_name = "Sys1"
    print(f"\n{'='*55}")
    print(f"  Deploying LOAD BALANCER to {sys_name}")
    print(f"{'='*55}")

    client = make_ssh(sys_name)
    sftp = client.open_sftp()

    kill_remote_services(client, "loadbalancer")
    run_remote(client, f"mkdir -p {REMOTE_LB_DIR}/load_balancer")

    lb_files = ["__init__.py", "balancer.py", "algorithms.py", "health_checker.py"]
    print("  Uploading load balancer files...")
    for fname in lb_files:
        lf = LB_DIR / fname
        if lf.exists():
            sftp.put(str(lf), f"{REMOTE_LB_DIR}/load_balancer/{fname}")
            print(f"    [OK] {fname}")
        else:
            print(f"    [SKIP] {fname}")

    python_bin = check_python(client)
    backend_urls = ",".join(f"http://{ip}:8000" for ip in BACKEND_INTERNAL_IPS)
    print(f"  Backend targets: {backend_urls}")

    startup = (
        f"#!/bin/bash\n"
        f"cd {REMOTE_LB_DIR}\n"
        f"nohup {python_bin} load_balancer/balancer.py "
        f"--host 0.0.0.0 --port 8000 "
        f"--backends {backend_urls} "
        f"--algorithm ip_hash "
        f"> {REMOTE_LB_DIR}/lb.log 2>&1 &\n"
        f"echo $! > {REMOTE_LB_DIR}/lb.pid\n"
        f"echo LB started with PID $(cat {REMOTE_LB_DIR}/lb.pid)\n"
    )
    sftp.putfo(io.StringIO(startup), f"{REMOTE_LB_DIR}/start_lb.sh")
    run_remote(client, f"chmod +x {REMOTE_LB_DIR}/start_lb.sh")

    print("  Starting load balancer...", end=" ", flush=True)
    out = run_remote(client, f"bash {REMOTE_LB_DIR}/start_lb.sh")
    print(out if out else "launched")

    time.sleep(3)
    status = run_remote(client, "curl -s http://127.0.0.1:8000/lb/status 2>/dev/null | head -c 300 || echo NORESPONSE", ignore_errors=True)
    if '"service"' in status or "Sys1" in status:
        print(f"  [HEALTHY] {sys_name} Load Balancer is UP")
    else:
        print(f"  [STARTING] LB still starting - check: tail -f {REMOTE_LB_DIR}/lb.log")
        log_out = run_remote(client, f"tail -n 10 {REMOTE_LB_DIR}/lb.log 2>/dev/null", ignore_errors=True)
        print(f"  [LOG]: {log_out}")

    sftp.close()
    client.close()


def check_status():
    print(f"\n{'='*55}")
    print("  Service Status")
    print(f"{'='*55}")
    for sys_name, cfg in MACHINES.items():
        try:
            client = make_ssh(sys_name)
            role = cfg["role"]
            if role == "backend":
                out = run_remote(client, "curl -s http://127.0.0.1:8000/health 2>/dev/null || echo DOWN", ignore_errors=True)
                ok = '"status"' in out or '"ok"' in out
                print(f"  {sys_name} Backend       : {'UP' if ok else 'DOWN'} | {out[:60]}")
            elif role == "loadbalancer":
                out = run_remote(client, "curl -s http://127.0.0.1:8000/lb/status 2>/dev/null | head -c 100 || echo DOWN", ignore_errors=True)
                ok = '"service"' in out
                print(f"  {sys_name} LoadBalancer  : {'UP' if ok else 'DOWN'}")
            client.close()
        except Exception as e:
            print(f"  {sys_name}: SSH FAILED - {e}")


def stop_all():
    print(f"\n{'='*55}")
    print("  Stopping all remote services")
    print(f"{'='*55}")
    for sys_name, cfg in MACHINES.items():
        try:
            client = make_ssh(sys_name)
            kill_remote_services(client, cfg["role"])
            print(f"  {sys_name}: stopped")
            client.close()
        except Exception as e:
            print(f"  {sys_name}: FAILED - {e}")


def main():
    parser = argparse.ArgumentParser(description="Deploy Local-Web-Chat to lab machines")
    parser.add_argument("--backends", action="store_true")
    parser.add_argument("--lb", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()

    if args.status:
        check_status()
        return
    if args.stop:
        stop_all()
        return

    deploy_backends_only = args.backends and not args.lb
    deploy_lb_only = args.lb and not args.backends
    deploy_all = not args.backends and not args.lb

    print("\nLocal-Web-Chat Distributed Lab Deployment")
    print(f"  SSH Host : {SSH_HOST}")
    print(f"  Backends : {', '.join(BACKEND_INTERNAL_IPS)}")
    print(f"  LB       : {MACHINES['Sys1']['internal_ip']}")
    print("  Algorithm: ip_hash\n")

    if deploy_all or deploy_backends_only:
        for sys_name in BACKEND_MACHINES:
            try:
                deploy_backend(sys_name)
            except Exception as e:
                print(f"\n  FAILED {sys_name}: {e}")

    if deploy_all or deploy_lb_only:
        try:
            deploy_loadbalancer()
        except Exception as e:
            print(f"\n  FAILED LoadBalancer: {e}")

    print(f"\n{'='*55}")
    print("  Deployment complete!")
    print()
    print("  NEXT STEPS:")
    print("  1. Run tunnel.bat in a NEW terminal window (keep it open)")
    print("     This forwards localhost:8000 -> Sys1 LB")
    print()
    print("  2. In this terminal, start frontend:")
    print("     npm run frontend")
    print()
    print("  3. Open browser: http://localhost:5000")
    print()
    print("  4. LB status: http://localhost:8000/lb/status  (tunnel must be running)")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
