import threading
import hashlib
import time
from typing import List, Optional, Dict, Any


class BackendNode:
    def __init__(self, node_id: str, host: str, port: int, weight: int = 1):
        self.node_id = str(node_id)
        self.host = str(host)
        self.port = int(port)
        self.weight = max(1, int(weight))
        self.url = f"http://{self.host}:{self.port}"

        self.is_healthy = True
        self.active_connections = 0
        self.total_requests = 0
        self.failed_requests = 0
        self.last_health_check_time = time.time()
        self.last_latency_ms = 0.0

        # Smoothed (EMA) latency from real client requests.
        self._ema_alpha = 0.3
        self.avg_latency_ms = 0.0
        self._has_sample = False

        self._lock = threading.Lock()

    def mark_healthy(self, latency_ms: Optional[float] = None):
        """Mark node healthy from health checker without corrupting request EMA."""
        with self._lock:
            self.is_healthy = True
            self.last_health_check_time = time.time()
            if latency_ms is not None and latency_ms > 0:
                self.last_latency_ms = latency_ms

    def record_request_latency(self, latency_ms: float):
        """Record actual client proxy request latency and update exponential moving average."""
        with self._lock:
            self.is_healthy = True
            self.last_latency_ms = latency_ms
            if latency_ms > 0:
                if not self._has_sample:
                    self.avg_latency_ms = latency_ms
                    self._has_sample = True
                else:
                    self.avg_latency_ms = (
                        self._ema_alpha * latency_ms
                        + (1.0 - self._ema_alpha) * self.avg_latency_ms
                    )

    def mark_unhealthy(self):
        with self._lock:
            self.is_healthy = False
            self.last_health_check_time = time.time()

    def increment_connections(self):
        with self._lock:
            self.active_connections += 1
            self.total_requests += 1

    def decrement_connections(self, success: bool = True):
        with self._lock:
            if self.active_connections > 0:
                self.active_connections -= 1
            if not success:
                self.failed_requests += 1

    def load_score(self) -> float:
        """Combined load signal used to rank backends: smoothed latency
        weighted together with current in-flight connections. Lower = less loaded."""
        with self._lock:
            return self.avg_latency_ms + (self.active_connections * 25.0)

    def exceeds_threshold(self, latency_threshold_ms: float, connections_threshold: int) -> bool:
        with self._lock:
            return (
                (self.avg_latency_ms > latency_threshold_ms and self.total_requests > 3)
                or self.active_connections >= connections_threshold
            )

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.node_id,
                "host": self.host,
                "port": self.port,
                "url": self.url,
                "weight": self.weight,
                "healthy": self.is_healthy,
                "active_connections": self.active_connections,
                "total_requests": self.total_requests,
                "failed_requests": self.failed_requests,
                "last_latency_ms": round(self.last_latency_ms, 2),
                "avg_latency_ms": round(self.avg_latency_ms, 2),
                "last_seen_seconds_ago": round(time.time() - self.last_health_check_time, 1),
            }


class LoadBalancerAlgorithm:
    def __init__(self, nodes: Optional[List[BackendNode]] = None, **kwargs):
        self.nodes: List[BackendNode] = list(nodes) if nodes else []
        self._lock = threading.Lock()

    def set_nodes(self, nodes: List[BackendNode]):
        with self._lock:
            self.nodes = list(nodes)

    def add_or_update_node(self, node: BackendNode):
        with self._lock:
            for i, n in enumerate(self.nodes):
                if n.node_id == node.node_id or (n.host == node.host and n.port == node.port):
                    self.nodes[i] = node
                    return
            self.nodes.append(node)

    def remove_node(self, node_id: str):
        with self._lock:
            self.nodes = [n for n in self.nodes if n.node_id != node_id]

    def get_healthy_nodes(self) -> List[BackendNode]:
        with self._lock:
            return [node for node in self.nodes if node.is_healthy]

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        raise NotImplementedError


class RoundRobinAlgorithm(LoadBalancerAlgorithm):
    def __init__(self, nodes: Optional[List[BackendNode]] = None, **kwargs):
        super().__init__(nodes, **kwargs)
        self._index = 0

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy_nodes = [node for node in self.nodes if node.is_healthy]
            if not healthy_nodes:
                return None

            node = healthy_nodes[self._index % len(healthy_nodes)]
            self._index = (self._index + 1) % len(healthy_nodes)
            return node


class WeightedRoundRobinAlgorithm(LoadBalancerAlgorithm):
    def __init__(self, nodes: Optional[List[BackendNode]] = None, **kwargs):
        super().__init__(nodes, **kwargs)
        self._current_index = -1
        self._current_weight = 0

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [node for node in self.nodes if node.is_healthy]
            if not healthy:
                return None

            max_weight = max(node.weight for node in healthy)
            gcd_weight = 1

            while True:
                self._current_index = (self._current_index + 1) % len(healthy)
                if self._current_index == 0:
                    self._current_weight -= gcd_weight
                    if self._current_weight <= 0:
                        self._current_weight = max_weight
                        if self._current_weight == 0:
                            return None
                if healthy[self._current_index].weight >= self._current_weight:
                    return healthy[self._current_index]


class LeastConnectionsAlgorithm(LoadBalancerAlgorithm):
    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [node for node in self.nodes if node.is_healthy]
            if not healthy:
                return None
            return min(healthy, key=lambda n: n.active_connections)


class IPHashAlgorithm(LoadBalancerAlgorithm):
    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [node for node in self.nodes if node.is_healthy]
            if not healthy:
                return None
            hash_val = int(hashlib.md5(client_ip.encode("utf-8")).hexdigest(), 16)
            return healthy[hash_val % len(healthy)]


class ThresholdBasedAlgorithm(LoadBalancerAlgorithm):
    """
    Performance-based / dynamic threshold algorithm.

    Directs requests to the active backend. When its smoothed latency or
    active concurrent connections cross the threshold, traffic dynamically
    switches / spills over to the least-loaded healthy backend.
    """

    def __init__(
        self,
        nodes: Optional[List[BackendNode]] = None,
        latency_threshold_ms: float = 150.0,
        connections_threshold: int = 2,
        **kwargs,
    ):
        super().__init__(nodes, **kwargs)
        self.latency_threshold_ms = float(latency_threshold_ms)
        self.connections_threshold = int(connections_threshold)
        self._current: Optional[BackendNode] = None

    def _pick_least_loaded(self, healthy: List[BackendNode]) -> BackendNode:
        return min(healthy, key=lambda n: n.load_score())

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        with self._lock:
            healthy = [node for node in self.nodes if node.is_healthy]
            if not healthy:
                self._current = None
                return None

            # If no current node or current node left the pool / became unhealthy
            if self._current is None or self._current not in healthy:
                self._current = self._pick_least_loaded(healthy)
                return self._current

            # Check if current backend is overloaded or if another backend is significantly less loaded
            if self._current.exceeds_threshold(self.latency_threshold_ms, self.connections_threshold):
                candidates = [n for n in healthy if n is not self._current]
                if candidates:
                    self._current = self._pick_least_loaded(candidates)

            return self._current


def get_algorithm(algorithm_name: str, nodes: List[BackendNode], **kwargs) -> LoadBalancerAlgorithm:
    algo = algorithm_name.lower().replace("-", "_").replace(" ", "_")
    if algo in ["round_robin", "rr"]:
        return RoundRobinAlgorithm(nodes)
    elif algo in ["weighted_round_robin", "wrr"]:
        return WeightedRoundRobinAlgorithm(nodes)
    elif algo in ["least_connections", "least_conn", "lc"]:
        return LeastConnectionsAlgorithm(nodes)
    elif algo in ["ip_hash", "iphash"]:
        return IPHashAlgorithm(nodes)
    elif algo in ["load_threshold", "threshold", "performance", "perf", "dynamic"]:
        return ThresholdBasedAlgorithm(
            nodes,
            latency_threshold_ms=kwargs.get("latency_threshold_ms", 150.0),
            connections_threshold=kwargs.get("connections_threshold", 2),
        )
    else:
        return ThresholdBasedAlgorithm(
            nodes,
            latency_threshold_ms=kwargs.get("latency_threshold_ms", 150.0),
            connections_threshold=kwargs.get("connections_threshold", 2),
        )