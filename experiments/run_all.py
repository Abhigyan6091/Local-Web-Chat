import sys
import os
import time
import json
import subprocess
import urllib.request
import urllib.error
from typing import List

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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from experiments.run_experiment1_single import run_experiment1
from experiments.run_experiment2_three import run_experiment2
from experiments.compare_experiments import generate_comparison

BACKEND_CONFIGS = [
    {"id": "Sys2", "port": 8001, "host": "127.0.0.1"},
    {"id": "Sys3", "port": 8002, "host": "127.0.0.1"},
    {"id": "Sys4", "port": 8003, "host": "127.0.0.1"},
]

LB_HOST = "127.0.0.1"
LB_PORT = 8000

def wait_for_endpoint(url: str, timeout: float = 8.0, expected_status: int = 200) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == expected_status:
                    return True
        except Exception:
            time.sleep(0.3)
    return False

def verify_backend_independently(node_cfg: dict) -> bool:
    url = f"http://{node_cfg['host']}:{node_cfg['port']}/health"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                print(f"  [{node_cfg['id']}] Reachable at http://{node_cfg['host']}:{node_cfg['port']} | Status: {data.get('status')} | Service: {data.get('service')}")
                return True
    except Exception as e:
        print(f"  [{node_cfg['id']}] Failed to reach at {url}: {e}")
        return False
    return False

def main():
    print("\n" + "=" * 75)
    print("DISTRIBUTED SYSTEMS LAB: LOAD BALANCER EXPERIMENT SUITE")
    print("=" * 75)
    
    processes: List[subprocess.Popen] = []
    py_exec = sys.executable

    try:
        print("\n[Step 1/6] Starting Backend Servers (Sys2, Sys3, Sys4)...")
        for cfg in BACKEND_CONFIGS:
            cmd = [
                py_exec,
                os.path.abspath("src/backend/server.py"),
                "--id", cfg["id"],
                "--host", cfg["host"],
                "--port", str(cfg["port"]),
                "--delay-ms", "0"
            ]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(p)
            print(f"  -> Spawned {cfg['id']} on http://{cfg['host']}:{cfg['port']} (PID: {p.pid})")

        print("\n[Step 2/6] Verifying independent reachability of backends...")
        all_reachable = True
        for cfg in BACKEND_CONFIGS:
            health_url = f"http://{cfg['host']}:{cfg['port']}/health"
            if not wait_for_endpoint(health_url, timeout=5.0):
                print(f"  {cfg['id']} failed to start within timeout!")
                all_reachable = False
            else:
                verify_backend_independently(cfg)
        
        if not all_reachable:
            print("Aborting: One or more backends failed to start.")
            return

        print("\n[Step 3/6] Starting Sys1 Load Balancer with SINGLE backend (Sys2:8001)...")
        lb_single_cmd = [
            py_exec,
            os.path.abspath("src/load_balancer/balancer.py"),
            "--port", str(LB_PORT),
            "--backends", f"http://127.0.0.1:8001"
        ]
        lb_single_proc = subprocess.Popen(lb_single_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(lb_single_proc)
        
        lb_status_url = f"http://{LB_HOST}:{LB_PORT}/lb/status"
        if not wait_for_endpoint(lb_status_url, timeout=5.0):
            print("Failed to start Load Balancer for Experiment 1.")
            return
        print(f"  Sys1 Load Balancer is UP on http://{LB_HOST}:{LB_PORT} (Single Backend Sys2)")

        workload_reqs = 300
        concurrency = 15
        work_delay = 20.0
        
        print("\n[Step 4/6] Executing Experiment 1 Workload...")
        run_experiment1(
            target_url=f"http://{LB_HOST}:{LB_PORT}",
            requests=workload_reqs,
            concurrency=concurrency,
            scenario="mixed",
            delay_ms=work_delay
        )

        lb_single_proc.terminate()
        lb_single_proc.wait(timeout=3.0)
        processes.remove(lb_single_proc)
        time.sleep(1.0)

        print("\n[Step 5/6] Starting Sys1 Load Balancer with THREE backends (Sys2, Sys3, Sys4)...")
        backends_arg = "http://127.0.0.1:8001,http://127.0.0.1:8002,http://127.0.0.1:8003"
        lb_multi_cmd = [
            py_exec,
            os.path.abspath("src/load_balancer/balancer.py"),
            "--port", str(LB_PORT),
            "--algorithm", "round_robin",
            "--backends", backends_arg
        ]
        lb_multi_proc = subprocess.Popen(lb_multi_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(lb_multi_proc)

        if not wait_for_endpoint(lb_status_url, timeout=5.0):
            print("Failed to start Load Balancer for Experiment 2.")
            return
        print(f"  Sys1 Load Balancer is UP on http://{LB_HOST}:{LB_PORT} (Three Backends: Sys2, Sys3, Sys4)")

        print("\n[Step 5b/6] Executing Experiment 2 Workload (Identical Workload)...")
        run_experiment2(
            target_url=f"http://{LB_HOST}:{LB_PORT}",
            requests=workload_reqs,
            concurrency=concurrency,
            scenario="mixed",
            delay_ms=work_delay
        )

        print("\n[Step 6/6] Generating Comparison & Performance Evaluation...")
        generate_comparison(
            exp1_file="experiments/results/experiment1_single_backend.json",
            exp2_file="experiments/results/experiment2_three_backends.json",
            output_md="experiments/results/comparison_report.md"
        )

        print("All experiments completed successfully!")

    finally:
        print("\nCleaning up and terminating all background servers...")
        for p in processes:
            try:
                p.terminate()
                p.wait(timeout=1.0)
            except Exception:
                p.kill()
        print("Cleanup complete. All servers stopped.")

if __name__ == "__main__":
    main()
