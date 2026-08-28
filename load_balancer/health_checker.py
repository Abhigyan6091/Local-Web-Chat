import time
import threading
import logging
import urllib.request
import urllib.error
from typing import List
try:
    from load_balancer.algorithms import BackendNode
except ImportError:
    try:
        from algorithms import BackendNode
    except ImportError:
        from src.load_balancer.algorithms import BackendNode

logger = logging.getLogger("HealthChecker")

class HealthChecker:
    def __init__(self, nodes: List[BackendNode], interval_seconds: float = 3.0, timeout_seconds: float = 1.5):
        self.nodes = nodes
        self.interval = max(0.5, float(interval_seconds))
        self.timeout = max(0.2, float(timeout_seconds))
        self._running = False
        self._thread: threading.Thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="HealthCheckerThread", daemon=True)
        self._thread.start()
        logger.info(f"Health checker started (interval={self.interval}s, timeout={self.timeout}s)")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Health checker stopped")

    def _check_node(self, node: BackendNode):
        health_url = f"{node.url}/health"
        was_healthy = node.is_healthy
        t0 = time.time()
        
        try:
            req = urllib.request.Request(health_url, headers={"User-Agent": "LoadBalancer-HealthChecker/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                latency_ms = (time.time() - t0) * 1000.0
                if resp.status == 200:
                    node.mark_healthy(latency_ms)
                    if not was_healthy:
                        logger.info(f"[HEALTH RESTORED] Backend {node.node_id} ({node.url}) is now UP (latency: {latency_ms:.2f}ms)")
                else:
                    node.mark_unhealthy()
                    if was_healthy:
                        logger.warning(f"[HEALTH FAILED] Backend {node.node_id} ({node.url}) returned status {resp.status}")
        except Exception as e:
            node.mark_unhealthy()
            if was_healthy:
                logger.warning(f"[HEALTH FAILED] Backend {node.node_id} ({node.url}) is UNREACHABLE: {e}")

    def _run_loop(self):
        while self._running:
            for node in list(self.nodes):
                self._check_node(node)
            time.sleep(self.interval)
