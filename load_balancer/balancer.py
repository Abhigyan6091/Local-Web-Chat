"""
balancer.py — Sys1 load balancer / reverse proxy
================================================
Clients only ever talk to this process. It exposes the two routes the
assignment requires — POST /message and GET /feed — plus the chat UI and the
WebSocket endpoint, and forwards everything to whichever backend is currently
in the best shape.

Why asyncio rather than ThreadingHTTPServer
-------------------------------------------
Sys1 is a one-core container (cgroup `cpu.max = 100000 100000`). A thread per
connection spends that single core on context switching long before the
backends saturate. This is a single-threaded event loop with keep-alive
connection pools to each backend, so the balancer stays cheap and the
backends stay the bottleneck — which is the point of the exercise.

Routing
-------
`AdaptiveThresholdAlgorithm` (see algorithms.py) scores every backend from its
reported CPU/memory plus the balancer's own in-flight and round-trip-time
measurements, and switches away from a backend as soon as it crosses the
configured threshold. Health is checked actively (periodic /health probes) and
passively (a connection failure while proxying immediately demotes a node).

Local endpoints
---------------
GET  /lb/status    routing state, per-backend scores and counters
GET  /lb/metrics   compact time-series sample for the load generator's plots
POST /lb/config    change threshold/weights at runtime (used by the sweep)
POST /register     dynamic backend registration
"""

from __future__ import annotations

import os
import sys

try:
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(65535, hard), hard))
except Exception:
    pass

import json
import time
import asyncio
import argparse
import logging
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from load_balancer.algorithms import BackendNode, ScoreConfig, get_algorithm
except ImportError:
    from algorithms import BackendNode, ScoreConfig, get_algorithm

try:
    from common import sysmetrics
except ImportError:
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "common")))
    import sysmetrics

logging.basicConfig(
    level=os.environ.get("LB_LOGLEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] [LB] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("LoadBalancer")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

MAX_HEAD = 64 * 1024
RELAY_CHUNK = 64 * 1024


# ── HTTP helpers ─────────────────────────────────────────────────────────────
class HttpHead:
    __slots__ = ("method", "target", "version", "headers", "lower", "raw")

    def __init__(self, method: str, target: str, version: str,
                 headers: List[Tuple[str, str]], raw: bytes):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.lower = {k.lower(): v for k, v in headers}
        self.raw = raw

    def get(self, name: str, default: str = "") -> str:
        return self.lower.get(name, default)

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]

    @property
    def query(self) -> str:
        return self.target.split("?", 1)[1] if "?" in self.target else ""

    def is_websocket(self) -> bool:
        return ("websocket" in self.get("upgrade").lower()
                and "upgrade" in self.get("connection").lower())

    def wants_close(self) -> bool:
        conn = self.get("connection").lower()
        if self.version == "HTTP/1.0":
            return "keep-alive" not in conn
        return "close" in conn


async def read_head(reader: asyncio.StreamReader) -> Optional[HttpHead]:
    """Read one request/response head. Returns None on a clean EOF."""
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    except asyncio.LimitOverrunError:
        raise ValueError("header block too large")
    if len(raw) > MAX_HEAD:
        raise ValueError("header block too large")

    lines = raw.split(b"\r\n")
    start = lines[0].decode("latin-1")
    parts = start.split(" ", 2)
    if len(parts) < 3:
        raise ValueError(f"malformed start line: {start!r}")

    headers: List[Tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        idx = line.find(b":")
        if idx <= 0:
            continue
        headers.append((line[:idx].decode("latin-1").strip(),
                        line[idx + 1:].decode("latin-1").strip()))
    return HttpHead(parts[0], parts[1], parts[2], headers, raw)


async def read_body(reader: asyncio.StreamReader, head: HttpHead) -> bytes:
    """Read a request body (Content-Length or chunked). Bodies here are small."""
    if head.get("transfer-encoding").lower().startswith("chunked"):
        out = bytearray()
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size = int(size_line.strip().split(b";")[0] or b"0", 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                break
            out += await reader.readexactly(size)
            await reader.readexactly(2)
        return bytes(out)
    length = head.get("content-length")
    if length:
        try:
            n = int(length)
        except ValueError:
            return b""
        if n > 0:
            return await reader.readexactly(n)
    return b""


def build_request(head: HttpHead, node: BackendNode, client_ip: str,
                  body: bytes, extra: Optional[Dict[str, str]] = None) -> bytes:
    out = [f"{head.method} {head.target} HTTP/1.1\r\n"]
    out.append(f"Host: {node.host}:{node.port}\r\n")
    seen = set()
    for k, v in head.headers:
        kl = k.lower()
        if kl in HOP_BY_HOP or kl == "host":
            continue
        if extra and kl in extra:
            continue
        seen.add(kl)
        out.append(f"{k}: {v}\r\n")
    if extra:
        for k, v in extra.items():
            out.append(f"{k}: {v}\r\n")
    if "x-forwarded-for" not in seen:
        out.append(f"X-Forwarded-For: {client_ip}\r\n")
    out.append("X-Forwarded-Proto: http\r\n")
    out.append("Connection: keep-alive\r\n")
    if body and "content-length" not in seen:
        out.append(f"Content-Length: {len(body)}\r\n")
    out.append("\r\n")
    return "".join(out).encode("latin-1") + body


def build_upgrade_request(head: HttpHead, node: BackendNode, client_ip: str) -> bytes:
    out = [f"{head.method} {head.target} HTTP/1.1\r\n",
           f"Host: {node.host}:{node.port}\r\n"]
    for k, v in head.headers:
        if k.lower() == "host":
            continue
        out.append(f"{k}: {v}\r\n")
    out.append(f"X-Forwarded-For: {client_ip}\r\n")
    out.append("X-Forwarded-Proto: http\r\n")
    out.append("\r\n")
    return "".join(out).encode("latin-1")


# ── Backend connection pool ──────────────────────────────────────────────────
class ConnectionPool:
    """Keep-alive sockets per backend; avoids a TCP handshake per request."""

    def __init__(self, max_idle: int = 256, max_idle_age: float = 20.0):
        self._idle: Dict[str, Deque[Tuple[asyncio.StreamReader, asyncio.StreamWriter, float]]] = {}
        self.max_idle = max_idle
        self.max_idle_age = max_idle_age

    async def acquire(self, node: BackendNode, timeout: float):
        q = self._idle.get(node.node_id)
        now = time.monotonic()
        while q:
            reader, writer, ts = q.popleft()
            if not writer.is_closing() and not reader.at_eof() and (now - ts) < self.max_idle_age:
                return reader, writer
            try:
                writer.close()
            except Exception:
                pass
        return await asyncio.wait_for(
            asyncio.open_connection(node.host, node.port), timeout=timeout)

    def release(self, node: BackendNode, reader, writer) -> None:
        if writer.is_closing() or reader.at_eof():
            try:
                writer.close()
            except Exception:
                pass
            return
        q = self._idle.setdefault(node.node_id, deque())
        if len(q) >= self.max_idle:
            try:
                writer.close()
            except Exception:
                pass
            return
        q.append((reader, writer, time.monotonic()))

    def drop(self, node: BackendNode) -> None:
        q = self._idle.pop(node.node_id, None)
        if not q:
            return
        for item in q:
            try:
                item[1].close()
            except Exception:
                pass

    def idle_count(self) -> Dict[str, int]:
        return {k: len(v) for k, v in self._idle.items()}


# ── The balancer ─────────────────────────────────────────────────────────────
class LoadBalancer:
    def __init__(self, host: str, port: int, nodes: List[BackendNode],
                 algorithm: str = "adaptive_threshold",
                 threshold: float = 0.55, release_ratio: float = 0.80,
                 score_cfg: Optional[ScoreConfig] = None,
                 health_interval: float = 1.0, health_timeout: float = 1.0,
                 connect_timeout: float = 5.0, request_timeout: float = 30.0,
                 retry_attempts: int = 3, static_dir: Optional[str] = None):
        self.host, self.port = host, port
        self.nodes = nodes
        self.score_cfg = score_cfg or ScoreConfig()
        self.algorithm_name = algorithm
        self.algorithm = get_algorithm(algorithm, nodes, self.score_cfg,
                                       threshold=threshold, release_ratio=release_ratio)
        self.health_interval = health_interval
        self.health_timeout = health_timeout
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.retry_attempts = max(1, retry_attempts)
        self.pool = ConnectionPool()

        self.start_time = time.time()
        self.request_count = 0
        self.error_count = 0
        self.retry_count = 0
        self.ws_count = 0
        self.active_clients = 0
        self.latency_sum_ms = 0.0
        self.routed: Dict[str, int] = {}

        self.static: Dict[str, Tuple[bytes, str]] = {}
        if static_dir and os.path.isdir(static_dir):
            self._load_static(static_dir)

    # ---- static UI --------------------------------------------------------
    def _load_static(self, directory: str) -> None:
        types = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                 ".css": "text/css", ".ico": "image/x-icon", ".png": "image/png",
                 ".svg": "image/svg+xml", ".json": "application/json"}
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            with open(path, "rb") as fh:
                self.static["/" + name] = (fh.read(), types.get(ext, "application/octet-stream"))
        if "/index.html" in self.static:
            self.static["/"] = self.static["/index.html"]
        logger.info("Serving %d static UI files from %s", len(self.static), directory)

    # ---- health monitoring ------------------------------------------------
    async def health_loop(self) -> None:
        while True:
            await asyncio.gather(*(self._probe(n) for n in list(self.nodes)),
                                 return_exceptions=True)
            await asyncio.sleep(self.health_interval)

    async def _probe(self, node: BackendNode) -> None:
        t0 = time.perf_counter()
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(node.host, node.port), timeout=self.health_timeout)
            writer.write(f"GET /health HTTP/1.1\r\nHost: {node.host}\r\n"
                         f"Connection: close\r\n\r\n".encode())
            await writer.drain()
            head = await asyncio.wait_for(read_head(reader), timeout=self.health_timeout)
            if head is None:
                raise IOError("no response to health probe")
            if not head.target.startswith("200"):
                raise IOError(f"health status {head.target}")
            # Read exactly the framed body: reading to EOF would stall against a
            # backend that honours keep-alive.
            body = await asyncio.wait_for(read_body(reader, head), timeout=self.health_timeout)
            metrics = json.loads(body or b"{}")
            rtt = (time.perf_counter() - t0) * 1000.0
            if node.report_health_ok(metrics, rtt):
                logger.info("[HEALTH] %s (%s) recovered — routing restored",
                            node.node_id, node.url)
        except Exception as exc:
            if node.report_health_fail():
                logger.warning("[HEALTH] %s (%s) marked UNHEALTHY: %s",
                               node.node_id, node.url, exc)
                self.pool.drop(node)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass

    # ---- local endpoints --------------------------------------------------
    def status_payload(self) -> Dict[str, Any]:
        m = sysmetrics.snapshot()
        healthy = [n for n in self.nodes if n.is_healthy]
        uptime = time.time() - self.start_time
        return {
            "service": "Sys1-LoadBalancer",
            "version": "4.0.0",
            "algorithm": self.algorithm_name,
            "routing": self.algorithm.describe(),
            "host": self.host,
            "port": self.port,
            "uptime_seconds": round(uptime, 2),
            "total_requests_proxied": self.request_count,
            "websocket_sessions": self.ws_count,
            "errors": self.error_count,
            "retries": self.retry_count,
            "active_client_connections": self.active_clients,
            "avg_proxy_latency_ms": round(self.latency_sum_ms / self.request_count, 3)
                                    if self.request_count else 0.0,
            "throughput_rps": round(self.request_count / uptime, 2) if uptime > 0 else 0.0,
            "requests_per_backend": dict(self.routed),
            "idle_pooled_connections": self.pool.idle_count(),
            "load_balancer_system": m,
            "backends_count": len(self.nodes),
            "healthy_backends_count": len(healthy),
            "backends": [n.to_dict(self.score_cfg) for n in self.nodes],
        }

    def metrics_payload(self) -> Dict[str, Any]:
        """Compact sample used by the load generator to plot all four systems."""
        m = sysmetrics.snapshot()
        return {
            "t": time.time(),
            "lb": {"cpu": m["cpu"], "mem": m["mem"], "inflight": self.active_clients,
                   "requests": self.request_count, "errors": self.error_count,
                   "retries": self.retry_count},
            "backends": {
                n.node_id: {
                    "cpu": round(n.cpu, 4), "mem": round(n.mem, 4),
                    "healthy": n.is_healthy,
                    "active_connections": n.active_connections,
                    "backend_inflight": n.backend_inflight,
                    "ewma_rtt_ms": round(n.ewma_rtt_ms, 3),
                    "score": round(n.load_score(self.score_cfg), 4),
                    "requests": n.total_requests, "failed": n.failed_requests,
                } for n in self.nodes
            },
            "current_backend": getattr(self.algorithm, "_current", None).node_id
                               if getattr(self.algorithm, "_current", None) else None,
            "switches": getattr(self.algorithm, "switch_count", 0),
            "threshold": getattr(self.algorithm, "threshold", None),
        }

    def apply_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        changed = {}
        if "threshold" in cfg and hasattr(self.algorithm, "threshold"):
            self.algorithm.threshold = float(cfg["threshold"])
            changed["threshold"] = self.algorithm.threshold
        if "release_ratio" in cfg and hasattr(self.algorithm, "release_ratio"):
            self.algorithm.release_ratio = float(cfg["release_ratio"])
            changed["release_ratio"] = self.algorithm.release_ratio
        if "algorithm" in cfg:
            self.algorithm_name = str(cfg["algorithm"])
            self.algorithm = get_algorithm(
                self.algorithm_name, self.nodes, self.score_cfg,
                threshold=float(cfg.get("threshold", 0.65)),
                release_ratio=float(cfg.get("release_ratio", 0.80)))
            changed["algorithm"] = self.algorithm_name
        for key in ("w_cpu", "w_conn", "w_lat", "w_mem",
                    "target_latency_ms", "conn_capacity"):
            if key in cfg:
                setattr(self.score_cfg, key, float(cfg[key]))
                changed[key] = float(cfg[key])
        if "reset_counters" in cfg:
            self.request_count = self.error_count = self.retry_count = 0
            self.latency_sum_ms = 0.0
            self.routed.clear()
            for n in self.nodes:
                n.total_requests = n.failed_requests = 0
                n.ewma_rtt_ms = 0.0
            if hasattr(self.algorithm, "switch_count"):
                self.algorithm.switch_count = 0
                self.algorithm.saturated_selections = 0
            changed["reset_counters"] = True
        logger.info("[CONFIG] updated: %s", changed)
        return changed

    def register_backend(self, node_id: str, host: str, port: int,
                         weight: int = 1) -> BackendNode:
        for n in self.nodes:
            if n.node_id == node_id:
                n.host, n.port, n.weight = host, port, weight
                n.url = f"http://{host}:{port}"
                return n
        node = BackendNode(node_id, host, port, weight)
        self.nodes.append(node)
        self.algorithm.set_nodes(self.nodes)
        logger.info("[REGISTER] backend %s -> %s", node_id, node.url)
        return node

    # ---- client connection handling ---------------------------------------
    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        self.active_clients += 1
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else "0.0.0.0"
        try:
            while True:
                try:
                    head = await read_head(reader)
                except ValueError:
                    await self._send_simple(writer, 400, {"error": "malformed request"})
                    return
                if head is None:
                    return

                if head.is_websocket():
                    await self._handle_websocket(head, reader, writer, client_ip)
                    return

                body = await read_body(reader, head)
                path = head.path

                if path.startswith("/lb/") or path == "/register":
                    keep = await self._handle_local(head, body, writer)
                elif path in self.static and head.method in ("GET", "HEAD"):
                    payload, ctype = self.static[path]
                    keep = await self._send_bytes(writer, 200, payload, ctype,
                                                  head.method == "HEAD")
                elif head.method == "OPTIONS":
                    keep = await self._send_cors_preflight(writer)
                else:
                    keep = await self._proxy(head, body, reader, writer, client_ip)

                if not keep or head.wants_close():
                    return
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            logger.debug("client loop error: %s", exc)
        finally:
            self.active_clients -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_local(self, head: HttpHead, body: bytes,
                            writer: asyncio.StreamWriter) -> bool:
        path = head.path
        if path == "/lb/status":
            return await self._send_json(writer, 200, self.status_payload())
        if path == "/lb/metrics":
            return await self._send_json(writer, 200, self.metrics_payload())
        if path == "/lb/health":
            return await self._send_json(writer, 200, {
                "status": "ok",
                "healthy_backends": len([n for n in self.nodes if n.is_healthy]),
                "total_backends": len(self.nodes)})
        if path == "/lb/config":
            cfg: Dict[str, Any] = {}
            if body:
                try:
                    cfg = json.loads(body)
                except Exception:
                    pass
            for k, vals in parse_qs(head.query).items():
                cfg[k] = vals[0]
            return await self._send_json(writer, 200,
                                         {"status": "ok", "changed": self.apply_config(cfg)})
        if path == "/register":
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                return await self._send_json(writer, 400, {"error": "invalid json"})
            node = self.register_backend(
                str(payload.get("id") or payload.get("node_id") or "unknown"),
                str(payload.get("host", "127.0.0.1")),
                int(payload.get("port", 4000)),
                int(payload.get("weight", 1)))
            return await self._send_json(writer, 200,
                                         {"status": "ok", "registered": node.to_dict()})
        return await self._send_json(writer, 404, {"error": "unknown load balancer route"})

    # ---- the proxy hot path ------------------------------------------------
    async def _proxy(self, head: HttpHead, body: bytes,
                     client_reader: asyncio.StreamReader,
                     client_writer: asyncio.StreamWriter, client_ip: str) -> bool:
        self.request_count += 1
        t_start = time.perf_counter()

        # Stamp a stable message id so a retry of POST /message against a second
        # backend is deduplicated by the database instead of stored twice.
        extra: Dict[str, str] = {}
        if head.method == "POST" and head.path == "/message" and not head.get("x-message-id"):
            extra["X-Message-Id"] = str(uuid.uuid4())

        attempts = min(len(self.nodes) or 1, self.retry_attempts)
        tried: List[str] = []
        last_error = "no healthy backend"

        for attempt in range(attempts):
            node = self.algorithm.select_node(client_ip=client_ip)
            if node is None:
                break
            if node.node_id in tried and attempt > 0:
                alt = [n for n in self.nodes if n.is_healthy and n.node_id not in tried]
                if not alt:
                    break
                node = min(alt, key=lambda n: n.load_score(self.score_cfg))
            tried.append(node.node_id)

            node.begin_request()
            t_node = time.perf_counter()
            backend_reader = backend_writer = None
            sent_any = False
            try:
                backend_reader, backend_writer = await self.pool.acquire(
                    node, self.connect_timeout)
                backend_writer.write(build_request(head, node, client_ip, body, extra))
                await backend_writer.drain()

                resp = await asyncio.wait_for(read_head(backend_reader),
                                              timeout=self.request_timeout)
                if resp is None:
                    raise IOError("backend closed connection before responding")

                sent_any = True
                keep_backend = await self._relay_response(
                    resp, backend_reader, client_writer, node)

                latency = (time.perf_counter() - t_node) * 1000.0
                node.end_request(latency, success=True)
                self.latency_sum_ms += (time.perf_counter() - t_start) * 1000.0
                self.routed[node.node_id] = self.routed.get(node.node_id, 0) + 1

                if keep_backend:
                    self.pool.release(node, backend_reader, backend_writer)
                else:
                    backend_writer.close()
                return True

            except Exception as exc:
                node.end_request((time.perf_counter() - t_node) * 1000.0, success=False)
                last_error = f"{type(exc).__name__}: {exc}"
                if backend_writer is not None:
                    try:
                        backend_writer.close()
                    except Exception:
                        pass
                if sent_any:
                    # Response already started streaming to the client; we cannot
                    # safely retry on another backend.
                    self.error_count += 1
                    return False

                if not (isinstance(exc, OSError) and getattr(exc, 'errno', None) == 24):
                    self.pool.drop(node)
                    if node.mark_unhealthy():
                        logger.warning("[FAILOVER] %s exceeded failure threshold — marked UNHEALTHY", node.node_id)
                self.retry_count += 1
                logger.warning("[FAILOVER] %s failed on %s %s (%s) — trying another backend",
                               node.node_id, head.method, head.path, last_error)

        # If all retries failed, attempt one emergency fallback to the least-loaded backend
        if self.nodes and not sent_any:
            fallback_node = min(self.nodes, key=lambda n: n.load_score(self.score_cfg))
            try:
                fallback_reader, fallback_writer = await self.pool.acquire(
                    fallback_node, self.connect_timeout)
                fallback_writer.write(build_request(head, fallback_node, client_ip, body, extra))
                await fallback_writer.drain()
                resp = await asyncio.wait_for(read_head(fallback_reader), timeout=self.request_timeout)
                if resp is not None:
                    keep_backend = await self._relay_response(
                        resp, fallback_reader, client_writer, fallback_node)
                    if keep_backend:
                        self.pool.release(fallback_node, fallback_reader, fallback_writer)
                    else:
                        fallback_writer.close()
                    return True
            except Exception:
                pass

        self.error_count += 1
        await self._send_json(client_writer, 503, {
            "error": "No healthy backend available",
            "detail": last_error,
            "tried": tried,
            "load_balancer": "Sys1",
        })
        return True

    async def _relay_response(self, resp: HttpHead, backend_reader: asyncio.StreamReader,
                              client_writer: asyncio.StreamWriter,
                              node: BackendNode) -> bool:
        """Stream one backend response to the client. Returns keep-alive-ness."""
        # read_head() parses a response start line into
        # method="HTTP/1.1", target="<status>", version="<reason>".
        head_parts = [f"{resp.method} {resp.target} {resp.version}\r\n"]
        chunked = resp.get("transfer-encoding").lower().startswith("chunked")
        length = resp.get("content-length")

        for k, v in resp.headers:
            if k.lower() in HOP_BY_HOP:
                continue
            head_parts.append(f"{k}: {v}\r\n")
        head_parts.append(f"X-Selected-Backend: {node.node_id}\r\n")
        head_parts.append("X-Load-Balancer: Sys1\r\n")
        if "access-control-allow-origin" not in resp.lower:
            head_parts.append("Access-Control-Allow-Origin: *\r\n")

        keep = True
        if chunked:
            head_parts.append("Transfer-Encoding: chunked\r\n")
        elif length is None:
            # No framing information available: closing is the only way to
            # delimit the body, so this connection cannot be reused.
            keep = False
        head_parts.append("Connection: keep-alive\r\n" if keep else "Connection: close\r\n")
        head_parts.append("\r\n")
        client_writer.write("".join(head_parts).encode("latin-1"))

        if chunked:
            while True:
                size_line = await backend_reader.readuntil(b"\r\n")
                client_writer.write(size_line)
                size = int(size_line.strip().split(b";")[0] or b"0", 16)
                if size == 0:
                    trailer = await backend_reader.readuntil(b"\r\n")
                    client_writer.write(trailer)
                    break
                remaining = size + 2
                while remaining > 0:
                    chunk = await backend_reader.read(min(RELAY_CHUNK, remaining))
                    if not chunk:
                        raise IOError("backend closed mid-chunk")
                    client_writer.write(chunk)
                    remaining -= len(chunk)
                await client_writer.drain()
        elif length is not None:
            remaining = int(length)
            while remaining > 0:
                chunk = await backend_reader.read(min(RELAY_CHUNK, remaining))
                if not chunk:
                    raise IOError("backend closed mid-body")
                client_writer.write(chunk)
                remaining -= len(chunk)
                await client_writer.drain()
        else:
            while True:
                chunk = await backend_reader.read(RELAY_CHUNK)
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()

        await client_writer.drain()
        return keep and "close" not in resp.get("connection").lower()

    # ---- websocket tunnelling ---------------------------------------------
    async def _handle_websocket(self, head: HttpHead, client_reader: asyncio.StreamReader,
                                client_writer: asyncio.StreamWriter, client_ip: str) -> None:
        node = self.algorithm.select_node(client_ip=client_ip)
        if node is None:
            await self._send_json(client_writer, 503, {"error": "No healthy backends"})
            return

        self.ws_count += 1
        node.begin_request()
        logger.info("[WS] %s -> %s (%s)", head.path, node.node_id, node.url)
        backend_writer = None
        t0 = time.perf_counter()
        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(node.host, node.port), timeout=self.connect_timeout)
            backend_writer.write(build_upgrade_request(head, node, client_ip))
            await backend_writer.drain()

            resp = await asyncio.wait_for(read_head(backend_reader), timeout=self.request_timeout)
            if resp is None:
                raise IOError("no upgrade response")
            client_writer.write(resp.raw)
            await client_writer.drain()

            await asyncio.gather(
                self._pump(client_reader, backend_writer),
                self._pump(backend_reader, client_writer),
                return_exceptions=True)
        except Exception as exc:
            logger.warning("[WS] tunnel to %s failed: %s", node.node_id, exc)
            node.mark_unhealthy()
        finally:
            node.end_request((time.perf_counter() - t0) * 1000.0, success=True)
            if backend_writer is not None:
                try:
                    backend_writer.close()
                except Exception:
                    pass
            logger.info("[WS] tunnel closed -> %s", node.node_id)

    @staticmethod
    async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(RELAY_CHUNK)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    # ---- small response helpers -------------------------------------------
    async def _send_bytes(self, writer: asyncio.StreamWriter, status: int, payload: bytes,
                          ctype: str, head_only: bool = False) -> bool:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                  503: "Service Unavailable", 204: "No Content"}.get(status, "OK")
        header = (f"HTTP/1.1 {status} {reason}\r\n"
                  f"Content-Type: {ctype}\r\n"
                  f"Content-Length: {len(payload)}\r\n"
                  f"Access-Control-Allow-Origin: *\r\n"
                  f"Connection: keep-alive\r\n\r\n").encode("latin-1")
        writer.write(header if head_only else header + payload)
        await writer.drain()
        return True

    async def _send_json(self, writer: asyncio.StreamWriter, status: int,
                         payload: Dict[str, Any]) -> bool:
        return await self._send_bytes(
            writer, status, json.dumps(payload, indent=2).encode(),
            "application/json; charset=utf-8")

    async def _send_simple(self, writer: asyncio.StreamWriter, status: int,
                           payload: Dict[str, Any]) -> bool:
        return await self._send_json(writer, status, payload)

    async def _send_cors_preflight(self, writer: asyncio.StreamWriter) -> bool:
        writer.write(b"HTTP/1.1 204 No Content\r\n"
                     b"Access-Control-Allow-Origin: *\r\n"
                     b"Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS, PATCH\r\n"
                     b"Access-Control-Allow-Headers: *\r\n"
                     b"Access-Control-Max-Age: 86400\r\n"
                     b"Content-Length: 0\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        await writer.drain()
        return True

    # ---- run ---------------------------------------------------------------
    async def serve(self) -> None:
        sysmetrics.snapshot()
        asyncio.create_task(self.health_loop())
        server = await asyncio.start_server(
            self.handle_client, self.host, self.port, backlog=2048,
            reuse_address=True)
        logger.info("=" * 68)
        logger.info("Sys1 Load Balancer listening on http://%s:%d", self.host, self.port)
        logger.info("Algorithm      : %s (threshold=%s)", self.algorithm_name,
                    getattr(self.algorithm, "threshold", "n/a"))
        logger.info("Score weights  : %s", self.score_cfg.to_dict())
        logger.info("Required routes: POST /message   GET /feed")
        logger.info("Diagnostics    : /lb/status  /lb/metrics  /lb/config")
        for n in self.nodes:
            logger.info("  backend %-6s -> %s (weight %d)", n.node_id, n.url, n.weight)
        logger.info("=" * 68)
        async with server:
            await server.serve_forever()


def parse_backends(spec: str) -> List[BackendNode]:
    nodes = []
    for idx, item in enumerate(x.strip() for x in spec.split(",") if x.strip()):
        if "=" in item:
            node_id, url = item.split("=", 1)
        else:
            node_id, url = f"Sys{idx + 2}", item
        parsed = urlparse(url if "://" in url else f"http://{url}")
        nodes.append(BackendNode(node_id, parsed.hostname or "127.0.0.1",
                                 parsed.port or 4000))
    return nodes


def main() -> None:
    ap = argparse.ArgumentParser(description="Sys1 adaptive load balancer")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--backends", required=True,
                    help="comma list, e.g. Sys2=http://172.17.0.39:4000,Sys3=...")
    ap.add_argument("--algorithm", default="adaptive_threshold")
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--release-ratio", type=float, default=0.80)
    ap.add_argument("--w-cpu", type=float, default=0.45)
    ap.add_argument("--w-conn", type=float, default=0.20)
    ap.add_argument("--w-lat", type=float, default=0.30)
    ap.add_argument("--w-mem", type=float, default=0.05)
    ap.add_argument("--target-latency-ms", type=float, default=120.0)
    ap.add_argument("--conn-capacity", type=float, default=24.0)
    ap.add_argument("--health-interval", type=float, default=1.0)
    ap.add_argument("--health-timeout", type=float, default=1.0)
    ap.add_argument("--retry-attempts", type=int, default=2)
    ap.add_argument("--static-dir", default=None)
    args = ap.parse_args()

    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop enabled")
    except Exception:
        pass

    cfg = ScoreConfig(args.w_cpu, args.w_conn, args.w_lat, args.w_mem,
                      args.target_latency_ms, args.conn_capacity)
    lb = LoadBalancer(
        host=args.host, port=args.port, nodes=parse_backends(args.backends),
        algorithm=args.algorithm, threshold=args.threshold,
        release_ratio=args.release_ratio, score_cfg=cfg,
        health_interval=args.health_interval, health_timeout=args.health_timeout,
        retry_attempts=args.retry_attempts, static_dir=args.static_dir)
    try:
        asyncio.run(lb.serve())
    except KeyboardInterrupt:
        logger.info("load balancer stopped")


if __name__ == "__main__":
    main()
