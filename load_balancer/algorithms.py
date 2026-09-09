"""
algorithms.py — backend bookkeeping and selection policies
==========================================================
`BackendNode` holds everything known about one backend: whether it is healthy,
what it last reported about its own utilisation, and what the balancer itself
has measured while proxying to it.

The policy the assignment asks for is `AdaptiveThreshold`. The classic
strategies below it (round robin, least connections, IP hash, weighted) are kept
so the report can compare against them on identical workloads.
"""

from __future__ import annotations

import time
import hashlib
import threading
from typing import Any, Dict, List, Optional


class BackendNode:
    def __init__(self, node_id: str, host: str, port: int, weight: int = 1):
        self.node_id = str(node_id)
        self.host = str(host)
        self.port = int(port)
        self.weight = max(1, int(weight))
        self.url = f"http://{self.host}:{self.port}"

        # ---- health ----
        self.is_healthy = True
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_health_check_time = 0.0
        self.last_state_change = time.time()

        # ---- self-reported by the backend's /health ----
        self.cpu = 0.0                 # 0..1 of the container's CPU allowance
        self.mem = 0.0                 # 0..1 of the container's memory limit
        self.backend_inflight = 0
        self.backend_latency_ms = 0.0
        self.ws_clients = 0

        # ---- measured by the balancer ----
        self.active_connections = 0    # in-flight right now, updates instantly
        self.total_requests = 0
        self.failed_requests = 0
        self.ewma_rtt_ms = 0.0
        self.last_latency_ms = 0.0
        self.health_rtt_ms = 0.0

        self._lock = threading.Lock()

    # ---- health transitions ------------------------------------------------
    def report_health_ok(self, metrics: Dict[str, Any], rtt_ms: float,
                         rise_threshold: int = 2) -> bool:
        """Returns True when this probe flipped the node back to healthy."""
        with self._lock:
            self.last_health_check_time = time.time()
            self.health_rtt_ms = rtt_ms
            self.cpu = float(metrics.get("cpu", 0.0) or 0.0)
            self.mem = float(metrics.get("mem", 0.0) or 0.0)
            self.backend_inflight = int(metrics.get("inflight", 0) or 0)
            self.backend_latency_ms = float(metrics.get("ewma_latency_ms", 0.0) or 0.0)
            self.ws_clients = int(metrics.get("ws_clients", 0) or 0)
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            if not self.is_healthy and self.consecutive_successes >= rise_threshold:
                self.is_healthy = True
                self.last_state_change = time.time()
                return True
            return False

    def report_health_fail(self, fail_threshold: int = 2) -> bool:
        """Returns True when this probe flipped the node to unhealthy."""
        with self._lock:
            self.last_health_check_time = time.time()
            self.consecutive_successes = 0
            self.consecutive_failures += 1
            if self.is_healthy and self.consecutive_failures >= fail_threshold:
                self.is_healthy = False
                self.last_state_change = time.time()
                return True
            return False

    def mark_unhealthy(self) -> None:
        """Passive failure detection: a proxied request could not be delivered."""
        with self._lock:
            self.consecutive_successes = 0
            self.consecutive_failures += 1
            if self.is_healthy:
                self.is_healthy = False
                self.last_state_change = time.time()

    # ---- request accounting ------------------------------------------------
    def begin_request(self) -> None:
        with self._lock:
            self.active_connections += 1
            self.total_requests += 1

    def end_request(self, latency_ms: float, success: bool = True) -> None:
        with self._lock:
            if self.active_connections > 0:
                self.active_connections -= 1
            if success:
                self.last_latency_ms = latency_ms
                self.ewma_rtt_ms = (latency_ms if self.ewma_rtt_ms == 0.0
                                    else 0.8 * self.ewma_rtt_ms + 0.2 * latency_ms)
            else:
                self.failed_requests += 1

    # ---- composite load score ---------------------------------------------
    def load_score(self, cfg: "ScoreConfig") -> float:
        """
        Normalised load in roughly 0..1+, where 1.0 means "at capacity".

        Blends what the node reports about itself (CPU, memory, its own queue)
        with what the balancer can see immediately (in-flight requests and
        round-trip time). The in-flight term is what makes the score react
        within a single request instead of waiting for the next health poll.
        """
        conn_term = self.active_connections / max(1.0, cfg.conn_capacity)
        lat_term = (self.ewma_rtt_ms / cfg.target_latency_ms) if cfg.target_latency_ms > 0 else 0.0
        return (cfg.w_cpu * self.cpu
                + cfg.w_conn * conn_term
                + cfg.w_lat * min(lat_term, 3.0)
                + cfg.w_mem * self.mem)

    def to_dict(self, cfg: Optional["ScoreConfig"] = None) -> Dict[str, Any]:
        with self._lock:
            data = {
                "id": self.node_id,
                "host": self.host,
                "port": self.port,
                "url": self.url,
                "weight": self.weight,
                "healthy": self.is_healthy,
                "cpu": round(self.cpu, 4),
                "mem": round(self.mem, 4),
                "active_connections": self.active_connections,
                "backend_inflight": self.backend_inflight,
                "ws_clients": self.ws_clients,
                "total_requests": self.total_requests,
                "failed_requests": self.failed_requests,
                "ewma_rtt_ms": round(self.ewma_rtt_ms, 2),
                "last_latency_ms": round(self.last_latency_ms, 2),
                "backend_latency_ms": round(self.backend_latency_ms, 2),
                "health_rtt_ms": round(self.health_rtt_ms, 2),
                "last_seen_seconds_ago": (round(time.time() - self.last_health_check_time, 2)
                                          if self.last_health_check_time else None),
            }
        if cfg is not None:
            data["load_score"] = round(self.load_score(cfg), 4)
        return data


class ScoreConfig:
    """Weights and normalisers for `BackendNode.load_score`."""

    def __init__(self, w_cpu: float = 0.45, w_conn: float = 0.20,
                 w_lat: float = 0.30, w_mem: float = 0.05,
                 target_latency_ms: float = 120.0, conn_capacity: float = 24.0):
        total = w_cpu + w_conn + w_lat + w_mem
        self.w_cpu, self.w_conn = w_cpu / total, w_conn / total
        self.w_lat, self.w_mem = w_lat / total, w_mem / total
        self.target_latency_ms = target_latency_ms
        self.conn_capacity = conn_capacity

    def to_dict(self) -> Dict[str, Any]:
        return {"w_cpu": round(self.w_cpu, 3), "w_conn": round(self.w_conn, 3),
                "w_lat": round(self.w_lat, 3), "w_mem": round(self.w_mem, 3),
                "target_latency_ms": self.target_latency_ms,
                "conn_capacity": self.conn_capacity}


class LoadBalancerAlgorithm:
    name = "base"

    def __init__(self, nodes: Optional[List[BackendNode]] = None,
                 cfg: Optional[ScoreConfig] = None, **kwargs):
        self.nodes: List[BackendNode] = list(nodes or [])
        self.cfg = cfg or ScoreConfig()
        self._lock = threading.Lock()

    def set_nodes(self, nodes: List[BackendNode]) -> None:
        with self._lock:
            self.nodes = list(nodes)

    def healthy(self) -> List[BackendNode]:
        return [n for n in self.nodes if n.is_healthy]

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"algorithm": self.name}


class AdaptiveThresholdAlgorithm(LoadBalancerAlgorithm):
    """
    Performance-based routing with threshold-triggered switching.

    Behaviour
    ---------
    * Traffic stays on the current backend while its composite load score is at
      or below `threshold` — this keeps connection reuse high and avoids the
      pointless spraying that fixed round-robin does.
    * The moment that score crosses `threshold`, the balancer switches to the
      healthy backend with the lowest score, so load follows real capacity.
    * Hysteresis (`release_ratio`) stops the two nodes trading traffic back and
      forth: once we have moved off a node, we only consider it "recovered"
      after its score falls to `threshold * release_ratio`.
    * Unhealthy backends are never selected. If every backend is above the
      threshold the least-loaded one still wins, so the cluster degrades
      gracefully instead of refusing traffic.
    """

    name = "adaptive_threshold"

    def __init__(self, nodes=None, cfg=None, threshold: float = 0.55,
                 release_ratio: float = 0.80, **kwargs):
        super().__init__(nodes, cfg)
        self.threshold = float(threshold)
        self.release_ratio = float(release_ratio)
        self._current: Optional[BackendNode] = None
        self.switch_count = 0
        self.saturated_selections = 0
        self.last_switch_reason = "startup"
        self.last_switch_time = 0.0

    def _switch_to(self, node: BackendNode, reason: str) -> None:
        if self._current is not node:
            self.switch_count += 1
            self.last_switch_reason = reason
            self.last_switch_time = time.time()
        self._current = node

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [n for n in self.nodes if n.is_healthy]
            if not healthy:
                self._current = None
                return None

            cur = self._current
            if cur is not None and cur.is_healthy:
                score = cur.load_score(self.cfg)
                if score <= self.threshold:
                    return cur                      # still comfortable, stay put
            else:
                cur = None

            # Current backend is over threshold (or gone) -> pick the best one.
            best = min(healthy, key=lambda n: n.load_score(self.cfg))
            best_score = best.load_score(self.cfg)

            if cur is not None and best is cur:
                # Nothing better exists; everything is loaded.
                self.saturated_selections += 1
                return cur

            if best_score > self.threshold:
                self.saturated_selections += 1
                self._switch_to(best, f"all backends above threshold "
                                      f"(best={best.node_id} @ {best_score:.2f})")
            else:
                self._switch_to(best, (f"{cur.node_id} exceeded threshold"
                                       if cur is not None else "initial selection"))
            return best

    def describe(self) -> Dict[str, Any]:
        return {
            "algorithm": self.name,
            "threshold": self.threshold,
            "release_ratio": self.release_ratio,
            "current_backend": self._current.node_id if self._current else None,
            "switch_count": self.switch_count,
            "saturated_selections": self.saturated_selections,
            "last_switch_reason": self.last_switch_reason,
            "weights": self.cfg.to_dict(),
        }


class LeastLoadAlgorithm(LoadBalancerAlgorithm):
    """Always route to the lowest composite score (no stickiness)."""

    name = "least_load"

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [n for n in self.nodes if n.is_healthy]
            if not healthy:
                return None
            return min(healthy, key=lambda n: n.load_score(self.cfg))


class RoundRobinAlgorithm(LoadBalancerAlgorithm):
    name = "round_robin"

    def __init__(self, nodes=None, cfg=None, **kwargs):
        super().__init__(nodes, cfg)
        self._index = 0

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [n for n in self.nodes if n.is_healthy]
            if not healthy:
                return None
            node = healthy[self._index % len(healthy)]
            self._index += 1
            return node


class WeightedRoundRobinAlgorithm(LoadBalancerAlgorithm):
    name = "weighted_round_robin"

    def __init__(self, nodes=None, cfg=None, **kwargs):
        super().__init__(nodes, cfg)
        self._counter = 0

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [n for n in self.nodes if n.is_healthy]
            if not healthy:
                return None
            expanded = [n for n in healthy for _ in range(n.weight)]
            node = expanded[self._counter % len(expanded)]
            self._counter += 1
            return node


class LeastConnectionsAlgorithm(LoadBalancerAlgorithm):
    name = "least_connections"

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [n for n in self.nodes if n.is_healthy]
            if not healthy:
                return None
            return min(healthy, key=lambda n: n.active_connections)


class IPHashAlgorithm(LoadBalancerAlgorithm):
    name = "ip_hash"

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [n for n in self.nodes if n.is_healthy]
            if not healthy:
                return None
            digest = hashlib.md5(client_ip.encode("utf-8")).hexdigest()
            return healthy[int(digest, 16) % len(healthy)]


_ALGORITHMS = {
    "adaptive_threshold": AdaptiveThresholdAlgorithm,
    "adaptive": AdaptiveThresholdAlgorithm,
    "least_load": LeastLoadAlgorithm,
    "round_robin": RoundRobinAlgorithm,
    "rr": RoundRobinAlgorithm,
    "weighted_round_robin": WeightedRoundRobinAlgorithm,
    "wrr": WeightedRoundRobinAlgorithm,
    "least_connections": LeastConnectionsAlgorithm,
    "least_conn": LeastConnectionsAlgorithm,
    "lc": LeastConnectionsAlgorithm,
    "ip_hash": IPHashAlgorithm,
    "iphash": IPHashAlgorithm,
}


def get_algorithm(name: str, nodes: List[BackendNode],
                  cfg: Optional[ScoreConfig] = None, **kwargs) -> LoadBalancerAlgorithm:
    key = (name or "").lower().replace("-", "_").replace(" ", "_")
    cls = _ALGORITHMS.get(key, AdaptiveThresholdAlgorithm)
    return cls(nodes, cfg, **kwargs)
