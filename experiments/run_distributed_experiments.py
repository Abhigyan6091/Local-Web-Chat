import os
import sys
import time
import json
import paramiko

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HOST = os.environ.get("LAB_HOST", "10.1.75.79")
USERNAME = os.environ.get("LAB_USER", "student")
PASSWORD = os.environ.get("LAB_PASSWORD", "REDACTED")

SYS1_PORT = 2237
SYS2_PORT = 2238
SYS3_PORT = 2239
SYS4_PORT = 2240

SYS1_IP = "172.17.0.38"
SYS2_IP = "172.17.0.39"
SYS3_IP = "172.17.0.40"
SYS4_IP = "172.17.0.41"

def get_ssh_client(port):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=port, username=USERNAME, password=PASSWORD, timeout=10)
    return client

def kill_existing_processes():
    print("\nCleaning up any existing backend / LB processes across all 4 systems...", flush=True)
    for p, name in [(SYS1_PORT, "Sys1"), (SYS2_PORT, "Sys2"), (SYS3_PORT, "Sys3"), (SYS4_PORT, "Sys4")]:
        try:
            client = get_ssh_client(p)
            client.exec_command("pkill -9 -f 'python.*(server|balancer|generator)' || true")
            client.close()
            print(f"  Cleared processes on {name} (Port {p})", flush=True)
        except Exception as e:
            print(f"  Could not cleanup {name}: {e}", flush=True)

def run_distributed_pipeline():
    print("=" * 75)
    print("DISTRIBUTED LAB CLUSTER: MULTI-SYSTEM EXPERIMENT RUNNER")
    print(f"Target Server: {HOST}")
    print(f"Sys1 (LB)     : Port {SYS1_PORT} (Internal IP: {SYS1_IP})")
    print(f"Sys2 (Backend): Port {SYS2_PORT} (Internal IP: {SYS2_IP})")
    print(f"Sys3 (Backend): Port {SYS3_PORT} (Internal IP: {SYS3_IP})")
    print(f"Sys4 (Backend): Port {SYS4_PORT} (Internal IP: {SYS4_IP})")
    print("=" * 75)

    kill_existing_processes()
    time.sleep(1.0)

    try:
        print("\n[Step 1/6] Starting Backend Servers on Sys2, Sys3, Sys4...", flush=True)
        for p, name, ip in [(SYS2_PORT, "Sys2", SYS2_IP), (SYS3_PORT, "Sys3", SYS3_IP), (SYS4_PORT, "Sys4", SYS4_IP)]:
            client = get_ssh_client(p)
            cmd = f"nohup python3 /home/student/Load_Balancer/src/backend/server.py --id {name} --port 8000 --host 0.0.0.0 > /tmp/backend_{name}.log 2>&1 < /dev/null &"
            client.exec_command(cmd)
            client.close()
            print(f"  -> Launched backend {name} on {ip}:8000", flush=True)

        time.sleep(2.0)

        print("\n[Step 2/6] Verifying independent reachability of all 3 backends from Sys1...", flush=True)
        sys1_client = get_ssh_client(SYS1_PORT)
        
        for name, ip in [("Sys2", SYS2_IP), ("Sys3", SYS3_IP), ("Sys4", SYS4_IP)]:
            stdin, stdout, stderr = sys1_client.exec_command(f"curl -s http://{ip}:8000/health")
            resp = stdout.read().decode("utf-8")
            if "healthy" in resp:
                print(f"  [{name}] Reachable at http://{ip}:8000/health -> {resp.strip()[:90]}", flush=True)
            else:
                print(f"  [{name}] Failed to reach at http://{ip}:8000/health: {resp}", flush=True)

        print(f"\n[Step 3/6] Starting Sys1 Load Balancer with SINGLE backend (Sys2: http://{SYS2_IP}:8000)...", flush=True)
        lb_single_cmd = f"nohup python3 /home/student/Load_Balancer/src/load_balancer/balancer.py --port 8000 --backends http://{SYS2_IP}:8000 > /tmp/lb_single.log 2>&1 < /dev/null &"
        sys1_client.exec_command(lb_single_cmd)
        time.sleep(2.0)

        stdin, stdout, stderr = sys1_client.exec_command("curl -s http://127.0.0.1:8000/lb/status")
        lb_status = stdout.read().decode("utf-8")
        print(f"  Sys1 Load Balancer is UP: {lb_status.strip()[:90]}...", flush=True)

        print("\n[Step 4/6] Executing Experiment 1 Workload on Sys1 (300 requests, 15 concurrency)...", flush=True)
        stdin, stdout, stderr = sys1_client.exec_command(
            "python3 /home/student/Load_Balancer/experiments/run_experiment1_single.py --url http://127.0.0.1:8000 --requests 300 --concurrency 15"
        )
        print(stdout.read().decode("utf-8"), flush=True)

        sys1_client.exec_command("pkill -9 -f balancer.py || true")
        time.sleep(1.0)

        print(f"\n[Step 5/6] Starting Sys1 Load Balancer with THREE backends (Sys2, Sys3, Sys4)...", flush=True)
        backends_arg = f"http://{SYS2_IP}:8000,http://{SYS3_IP}:8000,http://{SYS4_IP}:8000"
        lb_multi_cmd = f"nohup python3 /home/student/Load_Balancer/src/load_balancer/balancer.py --port 8000 --algorithm round_robin --backends {backends_arg} > /tmp/lb_multi.log 2>&1 < /dev/null &"
        sys1_client.exec_command(lb_multi_cmd)
        time.sleep(2.0)

        stdin, stdout, stderr = sys1_client.exec_command("curl -s http://127.0.0.1:8000/lb/status")
        print(f"  Sys1 Load Balancer (3 Backends) is UP!", flush=True)

        print("\n[Step 5b/6] Executing Experiment 2 Workload on Sys1 (Identical Workload)...", flush=True)
        stdin, stdout, stderr = sys1_client.exec_command(
            "python3 /home/student/Load_Balancer/experiments/run_experiment2_three.py --url http://127.0.0.1:8000 --requests 300 --concurrency 15"
        )
        print(stdout.read().decode("utf-8"), flush=True)

        print("\n[Step 6/6] Generating Comparison Report on Sys1...", flush=True)
        stdin, stdout, stderr = sys1_client.exec_command(
            "python3 /home/student/Load_Balancer/experiments/compare_experiments.py"
        )
        print(stdout.read().decode("utf-8"), flush=True)

        print("\nFetching experiment results from Sys1 to local workspace...", flush=True)
        sftp = sys1_client.open_sftp()
        local_results_dir = os.path.abspath("experiments/results")
        os.makedirs(local_results_dir, exist_ok=True)
        
        for f in ["experiment1_single_backend.json", "experiment2_three_backends.json", "comparison_report.md"]:
            remote_f = f"/home/student/Load_Balancer/experiments/results/{f}"
            local_f = os.path.join(local_results_dir, f)
            try:
                sftp.get(remote_f, local_f)
                print(f"  Downloaded {f}", flush=True)
            except Exception as e:
                print(f"  Could not download {f}: {e}", flush=True)
        sftp.close()
        sys1_client.close()

        print("\n" + "=" * 75)
        print("DISTRIBUTED 4-SYSTEM EXPERIMENT PIPELINE COMPLETED SUCCESSFULLY!")
        print("=" * 75)

    finally:
        kill_existing_processes()

if __name__ == "__main__":
    run_distributed_pipeline()
