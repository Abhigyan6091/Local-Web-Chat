"""
run_experiments.py — the measurement suite behind the report
============================================================
Every experiment resets the cluster first (see harness.reset_cluster), so runs
are compared from an identical starting state rather than inheriting the
previous run's message backlog.

    python -m experiments.run_experiments capacity     # users -> throughput/latency
    python -m experiments.run_experiments threshold    # threshold optimisation
    python -m experiments.run_experiments algorithms   # adaptive vs the classics
    python -m experiments.run_experiments failover     # unhealthy-backend handling
    python -m experiments.run_experiments timeline     # headline run for the plots
    python -m experiments.run_experiments all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments import harness
from load_generator.generator import LoadRun, RunConfig

URL = harness.PUBLIC_URL


def make_cfg(users: int, duration: float, **kw) -> RunConfig:
    base = dict(url=URL, users=users, duration=duration, ramp=2.0,
                min_len=16, max_len=256, min_interval=0.0, max_interval=0.05,
                feed_ratio=0.2, feed_limit=0, timeout=30.0,
                retry_duplicates=0.0, label="")
    base.update(kw)
    return RunConfig(**base)


async def one_run(cfg: RunConfig, reset: bool = True) -> Dict[str, Any]:
    if reset:
        harness.reset_cluster()
    run = LoadRun(cfg)
    summary = await run.run()
    summary["lb_status_after"] = harness.lb_status()
    summary["db_integrity"] = harness.db_duplicate_check()
    return {"summary": summary, "samples": [s.__dict__ for s in run.samples],
            "metrics": run.metrics}


def line(result: Dict[str, Any], prefix: str = "") -> None:
    s = result["summary"]
    o = s["overall"]
    integ = s["db_integrity"]
    print(f"  {prefix:<22} rps={s['throughput_rps']:>7.1f}  "
          f"p50={o['p50_ms']:>7.1f}  p95={o['p95_ms']:>8.1f}  p99={o['p99_ms']:>8.1f}  "
          f"err={o['error_rate_pct']:>5.2f}%  "
          f"rows={integ['rows']}/{integ['distinct_message_ids']}  "
          f"split={s['requests_per_backend']}")


# ── experiments ──────────────────────────────────────────────────────────────
async def exp_capacity(duration: float) -> Dict[str, Any]:
    print("\n[capacity] offered load vs throughput and latency")
    out = []
    for users in (10, 25, 50, 100, 150, 200):
        res = await one_run(make_cfg(users, duration))
        line(res, f"users={users}")
        out.append({"users": users, **res["summary"]})
    return {"experiment": "capacity", "runs": out}


async def exp_threshold(duration: float) -> Dict[str, Any]:
    print("\n[threshold] switching threshold optimisation (users=100)")
    out = []
    for thr in (0.30, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95, 1.20):
        harness.reset_cluster()
        harness.lb_config({"algorithm": "adaptive_threshold", "threshold": thr})
        res = await one_run(make_cfg(100, duration), reset=False)
        routing = res["summary"]["lb_status_after"]["routing"]
        res["summary"]["threshold"] = thr
        res["summary"]["switch_count"] = routing.get("switch_count")
        line(res, f"threshold={thr:.2f}")
        out.append({"threshold": thr, **res["summary"]})
    return {"experiment": "threshold", "runs": out}


async def exp_algorithms(duration: float) -> Dict[str, Any]:
    print("\n[algorithms] adaptive threshold vs fixed policies (users=100)")
    out = []
    for algo in ("round_robin", "ip_hash", "least_connections",
                 "least_load", "adaptive_threshold"):
        harness.reset_cluster()
        harness.lb_config({"algorithm": algo, "threshold": 0.65})
        res = await one_run(make_cfg(100, duration), reset=False)
        res["summary"]["algorithm"] = algo
        line(res, algo)
        out.append({"algorithm": algo, **res["summary"]})
    harness.lb_config({"algorithm": "adaptive_threshold", "threshold": 0.65})
    return {"experiment": "algorithms", "runs": out}


async def exp_failover(duration: float) -> Dict[str, Any]:
    """Kill a backend mid-run and confirm traffic keeps flowing, then recovers."""
    print("\n[failover] killing Sys4 mid-run, restoring it afterwards")
    harness.reset_cluster()
    harness.lb_config({"algorithm": "adaptive_threshold", "threshold": 0.65})

    cfg = make_cfg(60, duration)
    run = LoadRun(cfg)
    events: List[Dict[str, Any]] = []

    async def chaos() -> None:
        await asyncio.sleep(duration * 0.33)
        print("    -> killing Sys4 backend")
        events.append({"t": duration * 0.33, "event": "kill Sys4"})
        client = harness.connect("Sys4")
        harness.run(client, "pkill -f '^python3 -m uvicorn' 2>/dev/null")
        harness.run(client, "pkill -f '[s]upervisor.sh' 2>/dev/null")
        client.close()
        await asyncio.sleep(duration * 0.34)
        print("    -> restarting Sys4 backend")
        events.append({"t": duration * 0.67, "event": "restart Sys4"})
        client = harness.connect("Sys4")
        harness.run(client, f"setsid nohup bash {harness.REMOTE_DIR}/start_backend.sh "
                            "> /dev/null 2>&1 < /dev/null & disown; sleep 1")
        client.close()

    task = asyncio.create_task(chaos())
    summary = await run.run()
    await task
    summary["events"] = events
    summary["lb_status_after"] = harness.lb_status()
    summary["db_integrity"] = harness.db_duplicate_check()
    result = {"summary": summary, "samples": [s.__dict__ for s in run.samples],
              "metrics": run.metrics}
    line(result, "failover")
    harness.wait_healthy(90)
    return {"experiment": "failover", "run": result}


async def exp_timeline(duration: float) -> Dict[str, Any]:
    """Headline run: response time and utilisation of all four systems."""
    print(f"\n[timeline] sustained run for the report plots ({duration:.0f}s)")
    res = await one_run(make_cfg(100, duration, retry_duplicates=0.05,
                                 min_interval=0.0, max_interval=0.08))
    line(res, "timeline")
    return {"experiment": "timeline", "run": res}


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["capacity", "threshold", "algorithms",
                                      "failover", "timeline", "all"])
    ap.add_argument("--duration", type=float, default=30.0)
    args = ap.parse_args()

    jobs = {
        "capacity": (exp_capacity, "capacity.json"),
        "threshold": (exp_threshold, "threshold_sweep.json"),
        "algorithms": (exp_algorithms, "algorithm_comparison.json"),
        "failover": (exp_failover, "failover.json"),
        "timeline": (exp_timeline, "timeline.json"),
    }
    selected = list(jobs) if args.which == "all" else [args.which]

    t0 = time.time()
    for name in selected:
        fn, filename = jobs[name]
        duration = args.duration if name != "timeline" else max(args.duration, 60.0)
        data = await fn(duration)
        path = harness.save(filename, data)
        print(f"  saved -> {path}")
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    asyncio.run(amain())
