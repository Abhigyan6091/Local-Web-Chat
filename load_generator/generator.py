"""
generator.py — load generator for the distributed chat cluster
==============================================================
Drives the two assignment routes through the load balancer only:

    POST /message   {"client-name": ..., "msg": ...}
    GET  /feed

Workload shape (all three required dimensions are variable)
-----------------------------------------------------------
* users     — `--users N`, optionally ramped in over `--ramp` seconds
* length    — every message body is a random length drawn uniformly from
              `--min-len`..`--max-len` characters
* interval  — every user sleeps a random think-time between requests, drawn
              from `--min-interval`..`--max-interval` seconds

While traffic runs, a sampler polls the balancer's `/lb/metrics` so the report
can plot CPU and memory of all four systems against response time on one
timeline.

Example
-------
    python -m load_generator.generator --url http://10.1.75.79:4237 \
        --users 60 --duration 60 --out results/run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import string
import statistics
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import aiohttp

WORDS = ("hello", "there", "team", "meeting", "deadline", "assignment", "server",
         "latency", "cluster", "message", "please", "review", "update", "status",
         "chat", "network", "balance", "throughput", "report", "distributed")


def random_message(min_len: int, max_len: int) -> str:
    """A random-length, human-looking message body."""
    target = random.randint(min_len, max_len)
    parts: List[str] = []
    size = 0
    while size < target:
        w = random.choice(WORDS)
        parts.append(w)
        size += len(w) + 1
    text = " ".join(parts)[:target]
    return text or random.choice(WORDS)


@dataclass
class Sample:
    t: float                 # seconds since run start
    endpoint: str
    latency_ms: float
    status: int
    ok: bool
    backend: Optional[str] = None
    duplicate: bool = False
    bytes_in: int = 0


@dataclass
class RunConfig:
    url: str
    users: int
    duration: float
    ramp: float
    min_len: int
    max_len: int
    min_interval: float
    max_interval: float
    feed_ratio: float
    feed_limit: int
    timeout: float
    retry_duplicates: float
    label: str = ""


class LoadRun:
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.samples: List[Sample] = []
        self.metrics: List[Dict[str, Any]] = []
        self.t0 = 0.0
        self.stop = False
        self.sent_ids: List[str] = []

    # ---- one virtual user --------------------------------------------------
    async def user(self, session: aiohttp.ClientSession, index: int) -> None:
        cfg = self.cfg
        if cfg.ramp > 0:
            await asyncio.sleep(cfg.ramp * index / max(1, cfg.users))
        name = f"user{index:03d}"

        while not self.stop:
            if random.random() < cfg.feed_ratio:
                await self.do_feed(session)
            else:
                await self.do_message(session, name)
            if self.stop:
                break
            await asyncio.sleep(random.uniform(cfg.min_interval, cfg.max_interval))

    async def do_message(self, session: aiohttp.ClientSession, name: str) -> None:
        cfg = self.cfg
        msg_id = str(uuid.uuid4())
        payload = {"client-name": name,
                   "msg": random_message(cfg.min_len, cfg.max_len),
                   "message_id": msg_id}
        await self._post(session, payload)

        # Deliberately replay a fraction of messages to prove the cluster
        # deduplicates retries rather than storing them twice.
        if cfg.retry_duplicates and random.random() < cfg.retry_duplicates:
            await self._post(session, payload)

    async def _post(self, session: aiohttp.ClientSession, payload: dict) -> None:
        t = time.perf_counter()
        try:
            async with session.post(f"{self.cfg.url}/message", json=payload) as resp:
                body = await resp.read()
                dt = (time.perf_counter() - t) * 1000.0
                dup = False
                try:
                    dup = bool(json.loads(body).get("duplicate", False))
                except Exception:
                    pass
                self.samples.append(Sample(
                    time.perf_counter() - self.t0, "message", dt, resp.status,
                    200 <= resp.status < 300,
                    resp.headers.get("X-Selected-Backend"), dup, len(body)))
        except Exception:
            self.samples.append(Sample(
                time.perf_counter() - self.t0, "message",
                (time.perf_counter() - t) * 1000.0, 0, False))

    async def do_feed(self, session: aiohttp.ClientSession) -> None:
        t = time.perf_counter()
        url = f"{self.cfg.url}/feed"
        if self.cfg.feed_limit:
            url += f"?limit={self.cfg.feed_limit}"
        try:
            async with session.get(url) as resp:
                body = await resp.read()
                self.samples.append(Sample(
                    time.perf_counter() - self.t0, "feed",
                    (time.perf_counter() - t) * 1000.0, resp.status,
                    200 <= resp.status < 300,
                    resp.headers.get("X-Selected-Backend"), False, len(body)))
        except Exception:
            self.samples.append(Sample(
                time.perf_counter() - self.t0, "feed",
                (time.perf_counter() - t) * 1000.0, 0, False))

    # ---- system utilisation sampler ---------------------------------------
    async def sampler(self, session: aiohttp.ClientSession, interval: float = 0.5) -> None:
        while not self.stop:
            try:
                async with session.get(f"{self.cfg.url}/lb/metrics") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        data["t_rel"] = time.perf_counter() - self.t0
                        self.metrics.append(data)
            except Exception:
                pass
            await asyncio.sleep(interval)

    # ---- driver ------------------------------------------------------------
    async def run(self) -> Dict[str, Any]:
        cfg = self.cfg
        conn = aiohttp.TCPConnector(limit=cfg.users * 2 + 8, ttl_dns_cache=300,
                                    force_close=False)
        timeout = aiohttp.ClientTimeout(total=cfg.timeout)
        async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
            self.t0 = time.perf_counter()
            tasks = [asyncio.create_task(self.user(session, i)) for i in range(cfg.users)]
            tasks.append(asyncio.create_task(self.sampler(session)))
            await asyncio.sleep(cfg.duration)
            self.stop = True
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.summary()

    # ---- reporting ---------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        def pct(values: List[float], p: float) -> float:
            if not values:
                return 0.0
            s = sorted(values)
            k = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
            return round(s[k], 3)

        def block(name: str, rows: List[Sample]) -> Dict[str, Any]:
            lat = [r.latency_ms for r in rows if r.ok]
            ok = sum(1 for r in rows if r.ok)
            return {
                "requests": len(rows),
                "successful": ok,
                "failed": len(rows) - ok,
                "error_rate_pct": round(100.0 * (len(rows) - ok) / len(rows), 3) if rows else 0.0,
                "mean_ms": round(statistics.fmean(lat), 3) if lat else 0.0,
                "p50_ms": pct(lat, 50), "p90_ms": pct(lat, 90),
                "p95_ms": pct(lat, 95), "p99_ms": pct(lat, 99),
                "max_ms": round(max(lat), 3) if lat else 0.0,
                "bytes_received": sum(r.bytes_in for r in rows),
            }

        elapsed = max(1e-6, (self.samples[-1].t if self.samples else self.cfg.duration))
        msgs = [s for s in self.samples if s.endpoint == "message"]
        feeds = [s for s in self.samples if s.endpoint == "feed"]
        backends: Dict[str, int] = {}
        for s in self.samples:
            if s.backend:
                backends[s.backend] = backends.get(s.backend, 0) + 1

        return {
            "config": asdict(self.cfg),
            "elapsed_s": round(elapsed, 3),
            "total_requests": len(self.samples),
            "throughput_rps": round(len(self.samples) / elapsed, 2),
            "overall": block("overall", self.samples),
            "message": block("message", msgs),
            "feed": block("feed", feeds),
            "duplicates_reported": sum(1 for s in msgs if s.duplicate),
            "requests_per_backend": backends,
            "metric_samples": len(self.metrics),
        }

    def dump(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump({
                "summary": self.summary(),
                "samples": [asdict(s) for s in self.samples],
                "metrics": self.metrics,
            }, fh)


def print_summary(s: Dict[str, Any]) -> None:
    cfg = s["config"]
    print(f"\n  {'-' * 62}")
    print(f"  users={cfg['users']}  duration={cfg['duration']}s  "
          f"msg-len={cfg['min_len']}..{cfg['max_len']}  "
          f"think={cfg['min_interval']}..{cfg['max_interval']}s")
    print(f"  {'-' * 62}")
    print(f"  throughput        : {s['throughput_rps']:>9.2f} req/s")
    print(f"  total requests    : {s['total_requests']:>9}")
    print(f"  failed            : {s['overall']['failed']:>9} "
          f"({s['overall']['error_rate_pct']}%)")
    for key in ("overall", "message", "feed"):
        b = s[key]
        if not b["requests"]:
            continue
        print(f"  {key:<8} p50/p95/p99 : {b['p50_ms']:>8.2f} / "
              f"{b['p95_ms']:>8.2f} / {b['p99_ms']:>8.2f} ms  (n={b['requests']})")
    print(f"  duplicates blocked: {s['duplicates_reported']:>9}")
    print(f"  per backend       : {s['requests_per_backend']}")


async def amain(args: argparse.Namespace) -> None:
    cfg = RunConfig(
        url=args.url.rstrip("/"), users=args.users, duration=args.duration,
        ramp=args.ramp, min_len=args.min_len, max_len=args.max_len,
        min_interval=args.min_interval, max_interval=args.max_interval,
        feed_ratio=args.feed_ratio, feed_limit=args.feed_limit,
        timeout=args.timeout, retry_duplicates=args.retry_duplicates,
        label=args.label)
    run = LoadRun(cfg)
    print(f"\nLoad test -> {cfg.url}   ({cfg.users} users, {cfg.duration}s)")
    summary = await run.run()
    print_summary(summary)
    if args.out:
        run.dump(args.out)
        print(f"\n  raw results written to {args.out}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Chat cluster load generator")
    ap.add_argument("--url", default="http://10.1.75.79:4237",
                    help="load balancer base URL")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--ramp", type=float, default=2.0)
    ap.add_argument("--min-len", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=256)
    ap.add_argument("--min-interval", type=float, default=0.02)
    ap.add_argument("--max-interval", type=float, default=0.30)
    ap.add_argument("--feed-ratio", type=float, default=0.25,
                    help="fraction of requests that read /feed instead of posting")
    ap.add_argument("--feed-limit", type=int, default=0,
                    help="pass ?limit=N to /feed (0 = full feed)")
    ap.add_argument("--retry-duplicates", type=float, default=0.05,
                    help="fraction of messages re-sent with the same id")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--label", default="")
    return ap


if __name__ == "__main__":
    asyncio.run(amain(build_parser().parse_args()))
