import sys
import os
import json
import argparse
from typing import Dict, Any, Optional

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

def load_metrics(filepath: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(filepath):
        print(f"Error: File not found: {filepath}")
        return None
    with open(filepath, "r") as f:
        return json.load(f)

def generate_comparison(
    exp1_file: str = "experiments/results/experiment1_single_backend.json",
    exp2_file: str = "experiments/results/experiment2_three_backends.json",
    output_md: str = "experiments/results/comparison_report.md"
):
    e1 = load_metrics(exp1_file)
    e2 = load_metrics(exp2_file)

    if not e1 or not e2:
        print("Cannot perform comparison: One or both experiment result files are missing.")
        return

    total_req_1 = e1["total_requests"]
    total_req_2 = e2["total_requests"]
    succ_1 = e1["successful_requests"]
    succ_2 = e2["successful_requests"]
    fail_1 = e1["failed_requests"]
    fail_2 = e2["failed_requests"]
    err_rate_1 = e1["error_rate_pct"]
    err_rate_2 = e2["error_rate_pct"]
    dur_1 = e1["duration_seconds"]
    dur_2 = e2["duration_seconds"]
    rps_1 = e1["throughput_rps"]
    rps_2 = e2["throughput_rps"]

    lat1 = e1["latency_ms"]
    lat2 = e2["latency_ms"]

    speedup = round(rps_2 / rps_1, 2) if rps_1 > 0 else 0
    duration_reduction_pct = round(((dur_1 - dur_2) / dur_1) * 100.0, 1) if dur_1 > 0 else 0
    avg_lat_reduction_pct = round(((lat1["avg"] - lat2["avg"]) / lat1["avg"]) * 100.0, 1) if lat1["avg"] > 0 else 0
    p95_lat_reduction_pct = round(((lat1["p95"] - lat2["p95"]) / lat1["p95"]) * 100.0, 1) if lat1["p95"] > 0 else 0

    print("\n" + "=" * 80)
    print("LOAD BALANCER PERFORMANCE COMPARISON & BENCHMARK ANALYSIS")
    print("=" * 80)
    header = f"{'Metric':<32} | {'Exp 1: Single Backend':<22} | {'Exp 2: Three Backends':<22} | {'Improvement / Delta':<18}"
    sep = "-" * len(header)
    print(header)
    print(sep)
    
    rows = [
        ("Active Backends", "1 (Sys2)", "3 (Sys2, Sys3, Sys4)", "+2 Backends (+200%)"),
        ("Total Requests", str(total_req_1), str(total_req_2), "Identical Workload"),
        ("Successful Requests", f"{succ_1} ({(succ_1/total_req_1)*100:.1f}%)", f"{succ_2} ({(succ_2/total_req_2)*100:.1f}%)", f"{succ_2 - succ_1:+d} reqs"),
        ("Failed Requests", str(fail_1), str(fail_2), f"{fail_2 - fail_1:+d} reqs"),
        ("Error Rate (%)", f"{err_rate_1}%", f"{err_rate_2}%", f"{err_rate_2 - err_rate_1:+.2f}%"),
        ("Total Test Duration", f"{dur_1:.2f} s", f"{dur_2:.2f} s", f"{duration_reduction_pct:+.1f}% faster"),
        ("Throughput (RPS)", f"{rps_1:.2f} req/s", f"{rps_2:.2f} req/s", f"{speedup:.2f}x Speedup"),
        ("Average Latency", f"{lat1['avg']:.2f} ms", f"{lat2['avg']:.2f} ms", f"{avg_lat_reduction_pct:+.1f}% latency"),
        ("Median Latency (P50)", f"{lat1['p50']:.2f} ms", f"{lat2['p50']:.2f} ms", f"{lat1['p50'] - lat2['p50']:+.2f} ms"),
        ("90th Percentile Latency (P90)", f"{lat1['p90']:.2f} ms", f"{lat2['p90']:.2f} ms", f"{lat1['p90'] - lat2['p90']:+.2f} ms"),
        ("95th Percentile Latency (P95)", f"{lat1['p95']:.2f} ms", f"{lat2['p95']:.2f} ms", f"{p95_lat_reduction_pct:+.1f}% latency"),
        ("99th Percentile Latency (P99)", f"{lat1['p99']:.2f} ms", f"{lat2['p99']:.2f} ms", f"{lat1['p99'] - lat2['p99']:+.2f} ms"),
        ("Maximum Latency", f"{lat1['max']:.2f} ms", f"{lat2['max']:.2f} ms", f"{lat1['max'] - lat2['max']:+.2f} ms"),
        ("Minimum Latency", f"{lat1['min']:.2f} ms", f"{lat2['min']:.2f} ms", f"{lat1['min'] - lat2['min']:+.2f} ms"),
    ]

    for metric, c1, c2, delta in rows:
        print(f"{metric:<32} | {c1:<22} | {c2:<22} | {delta:<18}")
    print("=" * 80)

    print("\nExperiment 2 Backend Request Distribution:")
    for b_id, count in sorted(e2["backend_distribution"].items()):
        pct = (count / total_req_2) * 100.0
        bar = "|" * int(pct / 3)
        print(f"   - {b_id:<20}: {count:>5} requests ({pct:>5.1f}%)  {bar}")
    print()

    os.makedirs(os.path.dirname(os.path.abspath(output_md)), exist_ok=True)
    with open(output_md, "w", encoding="utf-8") as f:
        f.write("# Distributed Systems Lab: Load Balancer Performance Comparison Report\n\n")
        f.write(f"**Date:** {e2.get('timestamp', 'N/A')}  \n")
        f.write(f"**Load Balancer:** Sys1 (Round Robin)  \n\n")
        
        f.write("## 1. Executive Performance Summary\n\n")
        f.write(f"- **Throughput Speedup:** **`{speedup:.2f}x`** increase in requests processed per second (`{rps_1:.2f} RPS` -> `{rps_2:.2f} RPS`).\n")
        f.write(f"- **Average Latency Improvement:** **`{avg_lat_reduction_pct:.1f}%` reduction** in average response time (`{lat1['avg']:.2f} ms` down to `{lat2['avg']:.2f} ms`).\n")
        f.write(f"- **P95 Latency Improvement:** **`{p95_lat_reduction_pct:.1f}%` reduction** under concurrent load.\n")
        f.write(f"- **Workload Duration:** Total execution time dropped from `{dur_1:.2f}s` to `{dur_2:.2f}s` (`{duration_reduction_pct:.1f}%` reduction).\n\n")
        
        f.write("## 2. Comprehensive Comparison Table\n\n")
        f.write("| Performance Metric | Experiment 1 (Single Backend: Sys2) | Experiment 2 (Three Backends: Sys2, Sys3, Sys4) | Relative Delta / Improvement |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")
        for metric, c1, c2, delta in rows:
            f.write(f"| **{metric}** | {c1} | {c2} | **{delta}** |\n")
        
        f.write("\n## 3. Backend Workload Distribution (Experiment 2)\n\n")
        f.write("| Backend Node | Host / Port | Requests Handled | Share (%) | Distribution Visual |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- |\n")
        for b_id, count in sorted(e2["backend_distribution"].items()):
            pct = (count / total_req_2) * 100.0
            bar = "`" + "|" * int(pct / 4) + "`"
            f.write(f"| **{b_id}** | Cluster Node | {count} | {pct:.1f}% | {bar} |\n")

        f.write("\n## 4. Theoretical Analysis & Key Insights\n\n")
        f.write("1. **Horizontal Scalability:** Adding two additional backend nodes triples the available computing and I/O concurrency. Queued request bottlenecks at the single server are distributed across the cluster, preventing head-of-line blocking.\n")
        f.write("2. **Round-Robin Fairness:** As shown in the distribution table, Round Robin balances traffic with virtually equal distribution (~33.3% per node) when request processing times are uniformly distributed.\n")
        f.write("3. **Tail Latency Mitigation:** The 95th and 99th percentile latencies are dramatically reduced because incoming bursts of concurrent requests do not get serialized behind a single thread/worker pool.\n")

    print(f"Detailed comparison report written to: {output_md}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare Load Balancer Experiments")
    parser.add_argument("--exp1", type=str, default="experiments/results/experiment1_single_backend.json", help="Path to Experiment 1 JSON")
    parser.add_argument("--exp2", type=str, default="experiments/results/experiment2_three_backends.json", help="Path to Experiment 2 JSON")
    parser.add_argument("--output", type=str, default="experiments/results/comparison_report.md", help="Path to write Markdown report")
    
    args = parser.parse_args()
    generate_comparison(args.exp1, args.exp2, args.output)
