"""
sysmetrics.py — container-accurate system utilisation
=====================================================
Shared by the backends (which report their own load) and the load balancer
(which reports its own and aggregates the backends').

Why not psutil.cpu_percent()?
-----------------------------
The four lab systems are cgroup-v2 containers on one large host. `nproc` inside
them reports the host's 120 CPUs, and psutil's system-wide CPU percentage
reflects the *host*, so every container would report an almost identical value
and CPU-aware routing would be meaningless.

Each container is actually limited by `cpu.max = 100000 100000`, i.e. exactly
one core. This module reads the cgroup CPU accounting directly and divides the
consumed CPU-time by the quota, giving a true 0..1 utilisation of what this
container is really allowed to use. Memory is read the same way from
`memory.current` / `memory.max`.
"""

from __future__ import annotations

import os
import time
import threading

CG = "/sys/fs/cgroup"


def _read(path: str):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _cpu_quota_cores() -> float:
    """Cores this container may use, from cpu.max ('<quota> <period>' or 'max')."""
    raw = _read(f"{CG}/cpu.max")
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                return max(0.01, int(parts[0]) / int(parts[1]))
            except Exception:
                pass
    return float(os.cpu_count() or 1)


def _cpu_usage_usec():
    raw = _read(f"{CG}/cpu.stat")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("usage_usec"):
            try:
                return int(line.split()[1])
            except Exception:
                return None
    return None


def _mem():
    cur = _read(f"{CG}/memory.current")
    mx = _read(f"{CG}/memory.max")
    try:
        cur = int(cur)
    except Exception:
        return 0.0, 0, 0
    try:
        mx = int(mx)
    except Exception:
        mx = 0
    if not mx:
        return 0.0, cur, 0
    return cur / mx, cur, mx


class CpuSampler:
    """Samples container CPU utilisation between successive calls."""

    def __init__(self) -> None:
        self.cores = _cpu_quota_cores()
        self._lock = threading.Lock()
        self._last_usec = _cpu_usage_usec()
        self._last_t = time.monotonic()
        self._value = 0.0

    def sample(self) -> float:
        """Utilisation in 0..1 of this container's CPU allowance."""
        with self._lock:
            now = time.monotonic()
            usec = _cpu_usage_usec()
            if usec is None or self._last_usec is None:
                return self._value
            dt = now - self._last_t
            if dt < 0.05:
                return self._value
            busy = (usec - self._last_usec) / 1e6          # CPU-seconds consumed
            self._last_usec, self._last_t = usec, now
            self._value = max(0.0, min(1.5, busy / (dt * self.cores)))
            return self._value

    @property
    def value(self) -> float:
        return self._value


_sampler = CpuSampler()


def snapshot() -> dict:
    """Current container utilisation. Cheap enough for the request hot path."""
    cpu = _sampler.sample()
    mem_frac, mem_cur, mem_max = _mem()
    la1 = la5 = 0.0
    raw = _read("/proc/loadavg")
    if raw:
        parts = raw.split()
        try:
            la1, la5 = float(parts[0]), float(parts[1])
        except Exception:
            pass
    return {
        "cpu": round(cpu, 4),
        "cpu_cores": _sampler.cores,
        "mem": round(mem_frac, 4),
        "mem_bytes": mem_cur,
        "mem_limit_bytes": mem_max,
        "load1": la1,
        "load5": la5,
    }


if __name__ == "__main__":
    import json
    snapshot()
    time.sleep(1.0)
    print(json.dumps(snapshot(), indent=2))
