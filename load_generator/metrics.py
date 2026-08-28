import sys
import os
import time
import math
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

@dataclass
class RequestResult:
    success: bool
    status_code: int
    latency_ms: float
    backend_id: str = "Unknown"
    error_message: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class MetricsCollector:
    def __init__(self, experiment_name: str = "Experiment"):
        self.experiment_name = experiment_name
        self.results: List[RequestResult] = []
        self.start_time: float = 0.0
        self.end_time: float = 0.0

    def start(self):
        self.start_time = time.time()

    def record(self, result: RequestResult):
        self.results.append(result)

    def finish(self):
        self.end_time = time.time()

    def calculate_summary(self) -> Dict[str, Any]:
        total_requests = len(self.results)
        if total_requests == 0:
            return {"error": "No requests recorded"}

        duration_seconds = max(0.001, (self.end_time - self.start_time) if self.end_time > self.start_time else (time.time() - self.start_time))
        
        successful_requests = sum(1 for r in self.results if r.success)
        failed_requests = total_requests - successful_requests
        error_rate = (failed_requests / total_requests) * 100.0
        throughput_rps = total_requests / duration_seconds

        latencies = sorted([r.latency_ms for r in self.results])
        avg_latency = sum(latencies) / len(latencies)
        min_latency = latencies[0]
        max_latency = latencies[-1]
        
        def percentile(p: float) -> float:
            k = (len(latencies) - 1) * (p / 100.0)
            f = math.floor(k)
            c = math.ceil(k)
            if f == c:
                return latencies[int(k)]
            d0 = latencies[int(f)] * (c - k)
            d1 = latencies[int(c)] * (k - f)
            return d0 + d1

        p50 = percentile(50)
        p90 = percentile(90)
        p95 = percentile(95)
        p99 = percentile(99)

        backend_distribution: Dict[str, int] = {}
        for r in self.results:
            b_id = r.backend_id or "Unknown"
            backend_distribution[b_id] = backend_distribution.get(b_id, 0) + 1

        status_codes: Dict[int, int] = {}
        for r in self.results:
            status_codes[r.status_code] = status_codes.get(r.status_code, 0) + 1

        return {
            "experiment_name": self.experiment_name,
            "total_requests": total_requests,
            "successful_requests": successful_requests,
            "failed_requests": failed_requests,
            "error_rate_pct": round(error_rate, 2),
            "duration_seconds": round(duration_seconds, 3),
            "throughput_rps": round(throughput_rps, 2),
            "latency_ms": {
                "min": round(min_latency, 2),
                "avg": round(avg_latency, 2),
                "max": round(max_latency, 2),
                "p50": round(p50, 2),
                "p90": round(p90, 2),
                "p95": round(p95, 2),
                "p99": round(p99, 2)
            },
            "backend_distribution": backend_distribution,
            "status_codes": status_codes,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }

    def print_summary(self):
        summary = self.calculate_summary()
        lat = summary["latency_ms"]
        print("\n" + "=" * 65)
        print(f"Performance Summary: {summary['experiment_name']}")
        print("=" * 65)
        print(f" Total Requests       : {summary['total_requests']}")
        print(f" Successful Requests  : {summary['successful_requests']} ({(summary['successful_requests']/summary['total_requests'])*100:.1f}%)")
        print(f" Failed Requests      : {summary['failed_requests']} ({summary['error_rate_pct']}%)")
        print(f" Total Duration       : {summary['duration_seconds']} s")
        print(f" Throughput           : {summary['throughput_rps']} requests/sec (RPS)")
        print("-" * 65)
        print(f" Latency (Min)        : {lat['min']} ms")
        print(f" Latency (Avg)        : {lat['avg']} ms")
        print(f" Latency (Median P50) : {lat['p50']} ms")
        print(f" Latency (P90)        : {lat['p90']} ms")
        print(f" Latency (P95)        : {lat['p95']} ms")
        print(f" Latency (P99)        : {lat['p99']} ms")
        print(f" Latency (Max)        : {lat['max']} ms")
        print("-" * 65)
        print(" Backend Distribution :")
        for b_id, count in sorted(summary['backend_distribution'].items()):
            pct = (count / summary['total_requests']) * 100.0
            bar = "|" * int(pct / 4)
            print(f"   - {b_id:<18}: {count:>5} requests ({pct:>5.1f}%)  {bar}")
        print("=" * 65 + "\n")

    def save_json(self, filepath: str):
        summary = self.calculate_summary()
        with open(filepath, "w") as f:
            json.dump(summary, f, indent=2)
