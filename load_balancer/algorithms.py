import threading
import hashlib
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
        self.last_health_check_time = 0.0
        self.last_latency_ms = 0.0
        self._lock = threading.Lock()

    def mark_healthy(self, latency_ms: float = 0.0):
        with self._lock:
            self.is_healthy = True
            self.last_latency_ms = latency_ms

    def mark_unhealthy(self):
        with self._lock:
            self.is_healthy = False

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
                "last_latency_ms": round(self.last_latency_ms, 2)
            }


class LoadBalancerAlgorithm:
    def __init__(self, nodes: Optional[List[BackendNode]] = None):
        self.nodes: List[BackendNode] = nodes or []
        self._lock = threading.Lock()

    def set_nodes(self, nodes: List[BackendNode]):
        with self._lock:
            self.nodes = list(nodes)

    def get_healthy_nodes(self) -> List[BackendNode]:
        return [node for node in self.nodes if node.is_healthy]

    def select_node(self, client_ip: str = "127.0.0.1") -> Optional[BackendNode]:
        raise NotImplementedError


class RoundRobinAlgorithm(LoadBalancerAlgorithm):
    def __init__(self, nodes: Optional[List[BackendNode]] = None):
        super().__init__(nodes)
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
    def __init__(self, nodes: Optional[List[BackendNode]] = None):
        super().__init__(nodes)
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


def get_algorithm(algorithm_name: str, nodes: List[BackendNode]) -> LoadBalancerAlgorithm:
    algo = algorithm_name.lower().replace("-", "_").replace(" ", "_")
    if algo in ["round_robin", "rr"]:
        return RoundRobinAlgorithm(nodes)
    elif algo in ["weighted_round_robin", "wrr"]:
        return WeightedRoundRobinAlgorithm(nodes)
    elif algo in ["least_connections", "least_conn", "lc"]:
        return LeastConnectionsAlgorithm(nodes)
    elif algo in ["ip_hash", "iphash"]:
        return IPHashAlgorithm(nodes)
    else:
        return RoundRobinAlgorithm(nodes)
