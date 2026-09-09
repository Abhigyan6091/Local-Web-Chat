"""
harness.py — controlled experiment support for the distributed chat cluster
===========================================================================
Benchmarks on this cluster are only comparable if every run starts from the
same state. `/feed` returns the whole conversation, so a run that inherits the
previous run's messages ships larger responses and looks slower for reasons
that have nothing to do with the setting under test.

`reset_cluster()` therefore truncates the shared table and restarts the backend
processes so their in-memory feed caches start empty too. The supervisor script
installed by deploy.py brings each backend straight back up.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore")
import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploy import (MACHINES, BACKENDS, DB_NODE, SERVICE_PORT, SSH_HOST,
                    SSH_USER, SSH_PASS, REMOTE_DIR, PUBLIC_URL)

RESULTS_DIR = Path(__file__).parent / "results"


def connect(name: str) -> paramiko.SSHClient:
    cfg = MACHINES[name]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=SSH_HOST, port=cfg["ssh"], username=SSH_USER,
                   password=SSH_PASS, timeout=25, banner_timeout=25,
                   auth_timeout=25, allow_agent=False, look_for_keys=False)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, out, err = client.exec_command(cmd, timeout=timeout)
    return (out.read().decode("utf-8", "replace").strip() + "\n"
            + err.read().decode("utf-8", "replace").strip()).strip()


def truncate_messages() -> int:
    client = connect(DB_NODE)
    run(client, "PGPASSWORD=REDACTED psql -h 127.0.0.1 -U chatuser -d chatdb "
                "-c 'TRUNCATE TABLE messages RESTART IDENTITY;'")
    remaining = run(client, "PGPASSWORD=REDACTED psql -h 127.0.0.1 -U chatuser "
                            "-d chatdb -tAc 'select count(*) from messages;'")
    client.close()
    try:
        return int(remaining.splitlines()[0])
    except Exception:
        return -1


def restart_backends() -> None:
    """Kill each backend; its supervisor restarts it with an empty feed cache.

    The pattern is anchored to '^python3 -m uvicorn' on purpose. The supervisor's
    own command line ends with the uvicorn command it launches, so an unanchored
    match would kill the supervisor too and nothing would come back up.
    """
    for name in BACKENDS:
        client = connect(name)
        run(client, "pkill -f '^python3 -m uvicorn' 2>/dev/null; sleep 0.5")
        client.close()


def wait_healthy(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{PUBLIC_URL}/lb/health", timeout=5) as r:
                data = json.loads(r.read())
            if data.get("healthy_backends") == len(BACKENDS):
                # A healthy probe only means the port answers; give the pools a
                # moment to finish warming before measuring.
                time.sleep(2.0)
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def reset_cluster(verbose: bool = True) -> None:
    if verbose:
        print("    resetting cluster...", end=" ", flush=True)
    truncate_messages()
    restart_backends()
    ok = wait_healthy()
    lb_config({"reset_counters": True})
    if verbose:
        print("ready" if ok else "TIMED OUT waiting for backends")


def lb_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(cfg).encode()
    req = urllib.request.Request(f"{PUBLIC_URL}/lb/config", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def lb_status() -> Dict[str, Any]:
    with urllib.request.urlopen(f"{PUBLIC_URL}/lb/status", timeout=10) as r:
        return json.loads(r.read())


def db_message_count() -> int:
    client = connect(DB_NODE)
    out = run(client, "PGPASSWORD=REDACTED psql -h 127.0.0.1 -U chatuser -d chatdb "
                      "-tAc 'select count(*) from messages;'")
    client.close()
    try:
        return int(out.splitlines()[0])
    except Exception:
        return -1


def db_duplicate_check() -> Dict[str, int]:
    """Confirms the uniqueness guarantee actually held during a run."""
    client = connect(DB_NODE)
    total = run(client, "PGPASSWORD=REDACTED psql -h 127.0.0.1 -U chatuser -d chatdb "
                        "-tAc 'select count(*) from messages;'")
    distinct = run(client, "PGPASSWORD=REDACTED psql -h 127.0.0.1 -U chatuser -d chatdb "
                           "-tAc 'select count(distinct message_id) from messages;'")
    client.close()

    def first_int(text: str) -> int:
        try:
            return int(text.splitlines()[0])
        except Exception:
            return -1

    return {"rows": first_int(total), "distinct_message_ids": first_int(distinct)}


def save(name: str, payload: Any) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
    return path
