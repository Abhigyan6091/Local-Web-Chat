import sys
import os
import time
import json
import argparse
import logging
import socket
import select
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional

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

# Ensure current directory and project root are in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from load_balancer.algorithms import BackendNode, get_algorithm, LoadBalancerAlgorithm
    from load_balancer.health_checker import HealthChecker
except ImportError:
    try:
        from algorithms import BackendNode, get_algorithm, LoadBalancerAlgorithm
        from health_checker import HealthChecker
    except ImportError:
        from src.load_balancer.algorithms import BackendNode, get_algorithm, LoadBalancerAlgorithm
        from src.load_balancer.health_checker import HealthChecker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [LoadBalancer] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("LoadBalancer")


class LoadBalancerRequestHandler(BaseHTTPRequestHandler):
    server_version = "Sys1-LoadBalancer/2.0"

    def log_message(self, format, *args):
        pass

    def _handle_lb_status(self):
        lb_server = self.server
        with lb_server._nodes_lock:
            nodes_list = [node.to_dict() for node in lb_server.nodes]
            healthy_count = len([n for n in lb_server.nodes if n.is_healthy])
            total_count = len(lb_server.nodes)

        data = {
            "service": "Sys1-LoadBalancer",
            "algorithm": lb_server.algorithm_name,
            "host": lb_server.host,
            "port": lb_server.port,
            "latency_threshold_ms": getattr(lb_server, "latency_threshold_ms", 150.0),
            "connections_threshold": getattr(lb_server, "connections_threshold", 8),
            "uptime_seconds": round(time.time() - lb_server.start_time, 2),
            "total_requests_proxied": lb_server.request_count,
            "backends_count": total_count,
            "healthy_backends_count": healthy_count,
            "backends": nodes_list
        }
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_register(self):
        """Dynamic backend discovery endpoint: POST /register or POST /lb/register"""
        lb_server = self.server
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self._send_json_response(400, {"status": "error", "message": "Missing JSON request body"})
            return

        try:
            body_data = self.rfile.read(content_length).decode("utf-8")
            payload = json.loads(body_data)
        except Exception as e:
            self._send_json_response(400, {"status": "error", "message": f"Invalid JSON: {e}"})
            return

        node_id = payload.get("id") or payload.get("node_id") or payload.get("name")
        host = payload.get("host") or self.client_address[0]
        port = int(payload.get("port", 8000))
        weight = int(payload.get("weight", 1))

        if not node_id:
            node_id = f"{host}:{port}"

        node = lb_server.register_backend(node_id=str(node_id), host=str(host), port=port, weight=weight)

        logger.info(f"[DYNAMIC DISCOVERY] Backend '{node_id}' ({node.url}) successfully registered/updated via API.")
        self._send_json_response(200, {
            "status": "ok",
            "message": f"Backend '{node_id}' registered successfully",
            "registered_node": node.to_dict(),
            "total_backends": len(lb_server.nodes)
        })

    def _send_json_response(self, status_code: int, data: Dict[str, Any]):
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self):
        self._proxy_request("HEAD")

    def _is_websocket_request(self) -> bool:
        upgrade = self.headers.get("Upgrade", "").lower()
        connection = self.headers.get("Connection", "").lower()
        return upgrade == "websocket" and "upgrade" in connection

    def _relay_bytes(self, src: socket.socket, dst: socket.socket, label: str):
        """Relay raw bytes from src to dst until either side closes."""
        try:
            while True:
                ready, _, _ = select.select([src], [], [], 60.0)
                if not ready:
                    break
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    def _handle_websocket_tunnel(self):
        lb_server = self.server
        lb_server.request_count += 1
        req_id = lb_server.request_count
        client_ip = self.client_address[0]

        node = lb_server.algorithm.select_node(client_ip=client_ip)
        if not node:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"No healthy backends available"}')
            return

        node.increment_connections()
        logger.info(f"[WS #{req_id}] Tunnelling WebSocket {self.path} -> {node.node_id} ({node.host}:{node.port})")

        try:
            backend_sock = socket.create_connection((node.host, node.port), timeout=lb_server.timeout)
        except Exception as e:
            node.decrement_connections(success=False)
            node.mark_unhealthy()
            self.send_response(502)
            self.end_headers()
            logger.error(f"[WS #{req_id}] Failed to connect to backend {node.node_id}: {e}")
            return

        try:
            request_line = f"{self.command} {self.path} {self.request_version}\r\n"
            header_lines = "".join(
                f"{k}: {v}\r\n"
                for k, v in self.headers.items()
                if k.lower() != "host"
            )
            host_header = f"Host: {node.host}:{node.port}\r\n"
            forwarded_for = f"X-Forwarded-For: {client_ip}\r\n"
            forwarded_host = f"X-Forwarded-Host: {self.headers.get('Host', '')}\r\n"
            forwarded_proto = "X-Forwarded-Proto: http\r\n"
            raw_request = (
                request_line
                + host_header
                + forwarded_for
                + forwarded_host
                + forwarded_proto
                + header_lines
                + "\r\n"
            ).encode("latin-1")
            backend_sock.sendall(raw_request)

            response_data = b""
            while b"\r\n\r\n" not in response_data:
                chunk = backend_sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk

            client_sock = self.connection
            client_sock.sendall(response_data)

            t1 = threading.Thread(
                target=self._relay_bytes,
                args=(client_sock, backend_sock, "client->backend"),
                daemon=True
            )
            t2 = threading.Thread(
                target=self._relay_bytes,
                args=(backend_sock, client_sock, "backend->client"),
                daemon=True
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        except Exception as e:
            logger.warning(f"[WS #{req_id}] WebSocket tunnel error: {e}")
        finally:
            node.decrement_connections(success=True)
            try:
                backend_sock.close()
            except Exception:
                pass
            logger.info(f"[WS #{req_id}] WebSocket tunnel closed -> {node.node_id}")

    def do_GET(self):
        if self.path in ["/lb/status", "/status"]:
            self._handle_lb_status()
            return
        if self.path in ["/lb/health", "/health"]:
            self._send_json_response(200, {"status": "ok", "service": "Sys1-LoadBalancer"})
            return
        if self._is_websocket_request():
            self._handle_websocket_tunnel()
            return
        self._proxy_request("GET")

    def do_POST(self):
        if self.path in ["/register", "/lb/register", "/register_backend"]:
            self._handle_register()
            return
        self._proxy_request("POST")

    def do_PUT(self):
        self._proxy_request("PUT")

    def do_DELETE(self):
        self._proxy_request("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, PATCH")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_PATCH(self):
        self._proxy_request("PATCH")

    def _proxy_request(self, method: str):
        lb_server = self.server
        lb_server.request_count += 1
        req_id = lb_server.request_count
        start_time = time.time()
        client_ip = self.client_address[0]

        body = None
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            body = self.rfile.read(content_length)

        max_attempts = max(1, min(len(lb_server.nodes), lb_server.retry_attempts))
        attempt = 0
        last_error = None

        while attempt < max_attempts:
            attempt += 1
            node = lb_server.algorithm.select_node(client_ip=client_ip)

            if not node:
                self._send_error_response(
                    503,
                    f"Service Unavailable: No healthy backends available in cluster",
                    req_id,
                    start_time
                )
                return

            target_url = f"{node.url}{self.path}"
            node.increment_connections()

            try:
                req = urllib.request.Request(
                    url=target_url,
                    data=body if method in ["POST", "PUT", "PATCH"] else None,
                    method=method
                )

                for header_key, header_val in self.headers.items():
                    if header_key.lower() not in ["host", "content-length"]:
                        req.add_header(header_key, header_val)

                req.add_header("X-Forwarded-For", client_ip)
                req.add_header("X-Forwarded-Host", self.headers.get("Host", f"{lb_server.host}:{lb_server.port}"))
                req.add_header("X-Forwarded-Proto", "http")

                t_backend_start = time.time()
                with urllib.request.urlopen(req, timeout=lb_server.timeout) as backend_resp:
                    backend_status = backend_resp.status
                    backend_body = backend_resp.read()
                    backend_headers = dict(backend_resp.headers)
                    proxy_latency_ms = (time.time() - start_time) * 1000.0
                    backend_latency_ms = (time.time() - t_backend_start) * 1000.0

                    node.decrement_connections(success=True)
                    # Record actual backend processing latency
                    node.record_request_latency(backend_latency_ms)

                    self.send_response(backend_status)
                    for k, v in backend_headers.items():
                        if k.lower() not in ["transfer-encoding", "content-length"]:
                            self.send_header(k, v)

                    self.send_header("Content-Length", str(len(backend_body)))
                    self.send_header("X-Load-Balancer", "Sys1")
                    self.send_header("X-Selected-Backend", node.node_id)
                    self.send_header("X-Backend-Server", node.node_id)
                    self.send_header("X-Proxy-Total-Latency-Ms", f"{proxy_latency_ms:.2f}")
                    self.end_headers()
                    self.wfile.write(backend_body)

                    logger.info(
                        f"[REQ #{req_id}] {method} {self.path} -> {node.node_id} ({node.url}) "
                        f"[{backend_status}] (proxy: {proxy_latency_ms:.1f}ms, backend: {backend_latency_ms:.1f}ms)"
                    )
                    return

            except urllib.error.HTTPError as http_err:
                backend_body = http_err.read()
                proxy_latency_ms = (time.time() - start_time) * 1000.0
                node.decrement_connections(success=(http_err.code < 500))

                self.send_response(http_err.code)
                for k, v in http_err.headers.items():
                    if k.lower() not in ["transfer-encoding", "content-length"]:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(backend_body)))
                self.send_header("X-Load-Balancer", "Sys1")
                self.send_header("X-Selected-Backend", node.node_id)
                self.send_header("X-Backend-Server", node.node_id)
                self.end_headers()
                self.wfile.write(backend_body)

                logger.info(
                    f"[REQ #{req_id}] {method} {self.path} -> {node.node_id} "
                    f"[{http_err.code}] (took {proxy_latency_ms:.1f}ms)"
                )
                return

            except Exception as conn_err:
                node.decrement_connections(success=False)
                node.mark_unhealthy()
                last_error = conn_err
                logger.warning(
                    f"[FAILOVER] Backend {node.node_id} failed on {method} {self.path} ({conn_err}). "
                    f"Attempting next backend (attempt {attempt}/{max_attempts})..."
                )

        self._send_error_response(
            502,
            f"Bad Gateway: All target backends failed. Last error: {str(last_error)}",
            req_id,
            start_time
        )

    def _send_error_response(self, status_code: int, message: str, req_id: int, start_time: float):
        duration_ms = (time.time() - start_time) * 1000.0
        data = {
            "error": message,
            "status": status_code,
            "request_id": req_id,
            "load_balancer": "Sys1",
            "timestamp": time.time()
        }
        payload = json.dumps(data).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Load-Balancer", "Sys1")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS, PATCH")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(payload)
        logger.error(f"[REQ #{req_id}] {self.command} {self.path} -> {status_code} ERROR: {message} ({duration_ms:.1f}ms)")


class LoadBalancerServer(ThreadingHTTPServer):
    def __init__(
        self,
        host: str,
        port: int,
        nodes: List[BackendNode],
        algorithm_name: str = "load_threshold",
        health_interval: float = 3.0,
        health_timeout: float = 1.5,
        timeout: float = 5.0,
        retry_attempts: int = 2,
        latency_threshold_ms: float = 150.0,
        connections_threshold: int = 8
    ):
        self.host = host
        self.port = port
        self.nodes = list(nodes)
        self.algorithm_name = algorithm_name
        self.latency_threshold_ms = float(latency_threshold_ms)
        self.connections_threshold = int(connections_threshold)
        self._nodes_lock = threading.Lock()

        self.algorithm: LoadBalancerAlgorithm = get_algorithm(
            algorithm_name,
            self.nodes,
            latency_threshold_ms=latency_threshold_ms,
            connections_threshold=connections_threshold,
        )
        self.timeout = timeout
        self.retry_attempts = max(1, retry_attempts)
        self.start_time = time.time()
        self.request_count = 0

        self.health_checker = HealthChecker(self.nodes, interval_seconds=health_interval, timeout_seconds=health_timeout)
        self.health_checker.start()

        super().__init__((host, port), LoadBalancerRequestHandler)

    def register_backend(self, node_id: str, host: str, port: int, weight: int = 1) -> BackendNode:
        """Thread-safe dynamic backend registration (auto-discovery)."""
        with self._nodes_lock:
            for n in self.nodes:
                if n.node_id == node_id or (n.host == host and n.port == port):
                    n.host = host
                    n.port = port
                    n.weight = max(1, int(weight))
                    n.url = f"http://{host}:{port}"
                    n.mark_healthy()
                    self.algorithm.add_or_update_node(n)
                    self.health_checker.add_or_update_node(n)
                    return n

            new_node = BackendNode(node_id=node_id, host=host, port=port, weight=weight)
            self.nodes.append(new_node)
            self.algorithm.add_or_update_node(new_node)
            self.health_checker.add_or_update_node(new_node)
            return new_node

    def update_backends(self, new_nodes: List[BackendNode]):
        with self._nodes_lock:
            self.nodes = list(new_nodes)
            self.algorithm.set_nodes(new_nodes)
            self.health_checker.set_nodes(new_nodes)


def create_nodes_from_config(config_dict: Dict[str, Any]) -> List[BackendNode]:
    nodes = []
    for b in config_dict.get("backends", []):
        node = BackendNode(
            node_id=b.get("id", f"{b.get('host')}:{b.get('port')}"),
            host=b.get("host", "127.0.0.1"),
            port=int(b.get("port", 8001)),
            weight=int(b.get("weight", 1))
        )
        nodes.append(node)
    return nodes


def run_load_balancer(
    host: str = "0.0.0.0",
    port: int = 8000,
    nodes: Optional[List[BackendNode]] = None,
    algorithm: str = "load_threshold",
    health_interval: float = 3.0,
    health_timeout: float = 1.5,
    latency_threshold_ms: float = 150.0,
    connections_threshold: int = 8
):
    if nodes is None:
        nodes = []

    server = LoadBalancerServer(
        host=host,
        port=port,
        nodes=nodes,
        algorithm_name=algorithm,
        health_interval=health_interval,
        health_timeout=health_timeout,
        latency_threshold_ms=latency_threshold_ms,
        connections_threshold=connections_threshold
    )

    logger.info("=" * 60)
    logger.info(f"Sys1 Load Balancer running on http://{host}:{port}")
    logger.info(f"Algorithm : {algorithm.upper()}")
    logger.info(f"  latency_threshold_ms   = {latency_threshold_ms}")
    logger.info(f"  connections_threshold  = {connections_threshold}")
    logger.info(f"Status URL: http://{host}:{port}/lb/status")
    logger.info(f"Dynamic Registration URL: http://{host}:{port}/register (POST)")
    logger.info(f"Configured Initial Backends ({len(nodes)} total):")
    for n in nodes:
        logger.info(f"  - [{n.node_id}] {n.url} (weight: {n.weight})")
    logger.info("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down load balancer...")
    finally:
        server.health_checker.stop()
        server.server_close()
        logger.info("Load balancer stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sys1 HTTP Reverse Proxy Dynamic Load Balancer")
    parser.add_argument("--config", type=str, default=None, help="Path to config JSON file")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    parser.add_argument(
        "--algorithm", type=str, default="load_threshold",
        help="Algorithm: load_threshold, round_robin, weighted_round_robin, least_connections, ip_hash"
    )
    parser.add_argument("--backends", type=str, default=None, help="Comma-separated backend URLs, e.g. http://127.0.0.1:8001,http://127.0.0.1:8002")
    parser.add_argument(
        "--threshold-ms", type=float, default=150.0,
        help="For --algorithm load_threshold: switch away from the current backend when its smoothed latency exceeds this many ms"
    )
    parser.add_argument(
        "--connections-threshold", type=int, default=8,
        help="For --algorithm load_threshold: switch away from the current backend when its active connections exceed this count"
    )

    args = parser.parse_args()

    nodes = []
    host = args.host
    port = args.port
    algo = args.algorithm
    threshold_ms = args.threshold_ms
    conn_threshold = args.connections_threshold

    if args.config and os.path.exists(args.config):
        with open(args.config, "r") as f:
            cfg = json.load(f)
            nodes = create_nodes_from_config(cfg)
            lb_cfg = cfg.get("load_balancer", {})
            host = lb_cfg.get("host", host)
            port = lb_cfg.get("port", port)
            algo = lb_cfg.get("algorithm", algo)
            threshold_ms = lb_cfg.get("threshold_ms", threshold_ms)
            conn_threshold = lb_cfg.get("connections_threshold", conn_threshold)
    elif args.backends:
        backend_urls = [u.strip() for u in args.backends.split(",") if u.strip()]
        for idx, u in enumerate(backend_urls):
            parsed = urlparse(u if "://" in u else f"http://{u}")
            node_id = f"Sys{idx+2}"
            nodes.append(BackendNode(node_id, parsed.hostname or "127.0.0.1", parsed.port or 8000))
    else:
        # Default empty or initial fallback
        nodes = []

    run_load_balancer(
        host=host, port=port, nodes=nodes, algorithm=algo,
        latency_threshold_ms=threshold_ms, connections_threshold=conn_threshold
    )