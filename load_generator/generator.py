import sys
import os
import time
import json
import random
import argparse
import logging
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
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

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from load_generator.metrics import MetricsCollector, RequestResult
except ImportError:
    try:
        from metrics import MetricsCollector, RequestResult
    except ImportError:
        from src.load_generator.metrics import MetricsCollector, RequestResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [LoadGen] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("LoadGenerator")


class LoadGenerator:
    def __init__(
        self,
        target_url: str,
        total_requests: int = 300,
        concurrency: int = 15,
        scenario: str = "mixed",
        work_delay_ms: float = 20.0,
        timeout: float = 10.0,
        experiment_name: str = "Benchmark"
    ):
        self.target_url = target_url.rstrip("/")
        self.total_requests = max(1, total_requests)
        self.concurrency = max(1, concurrency)
        self.scenario = scenario
        self.work_delay_ms = work_delay_ms
        self.timeout = timeout
        self.metrics = MetricsCollector(experiment_name=experiment_name)
        self._completed_count = 0
        self._lock = threading.Lock()

    def _generate_request_data(self, req_index: int) -> Dict[str, Any]:
        if self.scenario == "workload":
            return {
                "method": "POST",
                "path": "/api/workload",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"delay_ms": self.work_delay_ms, "req_id": req_index}).encode("utf-8")
            }
        elif self.scenario == "messages_read":
            return {
                "method": "GET",
                "path": f"/api/messages?limit=20",
                "headers": {},
                "body": None
            }
        elif self.scenario == "messages_write":
            senders = ["Alice", "Bob", "Charlie", "Diana", "Evan", "Fiona"]
            channels = ["general", "distributed-systems", "announcements"]
            return {
                "method": "POST",
                "path": "/api/messages",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "sender": random.choice(senders),
                    "content": f"Test message #{req_index} from load generator",
                    "channel": random.choice(channels)
                }).encode("utf-8")
            }
        else:
            dice = random.random()
            if dice < 0.40:
                return {
                    "method": "GET",
                    "path": "/api/messages?limit=10",
                    "headers": {},
                    "body": None
                }
            elif dice < 0.70:
                senders = ["Alice", "Bob", "Charlie", "Diana"]
                return {
                    "method": "POST",
                    "path": "/api/messages",
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({
                        "sender": random.choice(senders),
                        "content": f"Mixed message #{req_index}",
                        "channel": "general"
                    }).encode("utf-8")
                }
            else:
                return {
                    "method": "POST",
                    "path": "/api/workload",
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"delay_ms": self.work_delay_ms}).encode("utf-8")
                }

    def _execute_single_request(self, req_index: int) -> RequestResult:
        req_spec = self._generate_request_data(req_index)
        url = f"{self.target_url}{req_spec['path']}"
        t0 = time.time()

        try:
            req = urllib.request.Request(
                url=url,
                data=req_spec["body"],
                headers=req_spec["headers"],
                method=req_spec["method"]
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                latency_ms = (time.time() - t0) * 1000.0
                status_code = resp.status
                headers = dict(resp.headers)
                
                backend_id = headers.get("X-Selected-Backend") or headers.get("X-Backend-Server") or headers.get("X-Handled-By") or "Unknown"
                
                return RequestResult(
                    success=(200 <= status_code < 400),
                    status_code=status_code,
                    latency_ms=latency_ms,
                    backend_id=backend_id
                )
        except urllib.error.HTTPError as e:
            latency_ms = (time.time() - t0) * 1000.0
            headers = dict(e.headers) if hasattr(e, "headers") else {}
            backend_id = headers.get("X-Selected-Backend") or headers.get("X-Backend-Server") or "Unknown"
            return RequestResult(
                success=False,
                status_code=e.code,
                latency_ms=latency_ms,
                backend_id=backend_id,
                error_message=str(e)
            )
        except Exception as e:
            latency_ms = (time.time() - t0) * 1000.0
            return RequestResult(
                success=False,
                status_code=0,
                latency_ms=latency_ms,
                backend_id="Unavailable",
                error_message=str(e)
            )

    def run(self) -> Dict[str, Any]:
        logger.info(f"Starting load test -> {self.target_url}")
        logger.info(f"Requests: {self.total_requests} | Concurrency: {self.concurrency} | Scenario: {self.scenario}")
        
        self.metrics.start()
        start_time = time.time()
        
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = [executor.submit(self._execute_single_request, i + 1) for i in range(self.total_requests)]
            
            for future in as_completed(futures):
                result = future.result()
                self.metrics.record(result)
                with self._lock:
                    self._completed_count += 1
                    if self._completed_count % max(1, self.total_requests // 10) == 0 or self._completed_count == self.total_requests:
                        elapsed = time.time() - start_time
                        rps = self._completed_count / max(0.001, elapsed)
                        pct = (self._completed_count / self.total_requests) * 100
                        print(f" Progress: {self._completed_count}/{self.total_requests} ({pct:.0f}%) | Current Throughput: {rps:.1f} RPS", end="\r", flush=True)

        self.metrics.finish()
        print()
        self.metrics.print_summary()
        return self.metrics.calculate_summary()


def run_benchmark(
    target_url: str = "http://localhost:8000",
    requests: int = 300,
    concurrency: int = 15,
    scenario: str = "mixed",
    delay_ms: float = 20.0,
    output_file: Optional[str] = None,
    experiment_name: str = "Benchmark"
) -> Dict[str, Any]:
    generator = LoadGenerator(
        target_url=target_url,
        total_requests=requests,
        concurrency=concurrency,
        scenario=scenario,
        work_delay_ms=delay_ms,
        experiment_name=experiment_name
    )
    results = generator.run()
    
    if output_file:
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        generator.metrics.save_json(output_file)
        logger.info(f"Metrics saved to {output_file}")
    
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Distributed Load Generator Client")
    parser.add_argument("--url", type=str, default="http://localhost:8000", help="Target URL (default: http://localhost:8000)")
    parser.add_argument("--requests", type=int, default=300, help="Total number of requests (default: 300)")
    parser.add_argument("--concurrency", type=int, default=15, help="Number of concurrent workers (default: 15)")
    parser.add_argument("--scenario", type=str, default="mixed", choices=["mixed", "workload", "messages_read", "messages_write"], help="Workload scenario")
    parser.add_argument("--delay-ms", type=float, default=20.0, help="Simulated processing delay in ms")
    parser.add_argument("--output", type=str, default=None, help="Path to save JSON metrics")
    parser.add_argument("--name", type=str, default="Benchmark", help="Experiment name")
    
    args = parser.parse_args()
    run_benchmark(
        target_url=args.url,
        requests=args.requests,
        concurrency=args.concurrency,
        scenario=args.scenario,
        delay_ms=args.delay_ms,
        output_file=args.output,
        experiment_name=args.name
    )
