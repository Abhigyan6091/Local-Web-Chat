import sys
import os
import argparse
import logging

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
from src.load_generator.generator import run_benchmark

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Experiment1")

def run_experiment1(
    target_url: str = "http://localhost:8000",
    requests: int = 300,
    concurrency: int = 15,
    scenario: str = "mixed",
    delay_ms: float = 20.0,
    output_dir: str = "experiments/results"
):
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "experiment1_single_backend.json")
    
    print("\n" + "=" * 70)
    print("EXPERIMENT 1: SINGLE BACKEND (SYS2 ONLY)")
    print("=" * 70)
    print(f" Target URL     : {target_url}")
    print(f" Requests       : {requests}")
    print(f" Concurrency    : {concurrency}")
    print(f" Work Delay     : {delay_ms} ms")
    print(f" Output File    : {output_file}")
    print("=" * 70 + "\n")

    results = run_benchmark(
        target_url=target_url,
        requests=requests,
        concurrency=concurrency,
        scenario=scenario,
        delay_ms=delay_ms,
        output_file=output_file,
        experiment_name="Experiment 1 - Single Backend (Sys2)"
    )
    return results

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experiment 1: Single Backend Benchmark")
    parser.add_argument("--url", type=str, default="http://localhost:8000", help="Load Balancer URL")
    parser.add_argument("--requests", type=int, default=300, help="Total requests")
    parser.add_argument("--concurrency", type=int, default=15, help="Concurrency")
    parser.add_argument("--scenario", type=str, default="mixed", help="Scenario")
    parser.add_argument("--delay-ms", type=float, default=20.0, help="Simulated processing delay in ms")
    
    args = parser.parse_args()
    run_experiment1(
        target_url=args.url,
        requests=args.requests,
        concurrency=args.concurrency,
        scenario=args.scenario,
        delay_ms=args.delay_ms
    )
