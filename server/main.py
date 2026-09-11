"""
main.py — Distributed secure group-chat backend node (Sys2 / Sys3 / Sys4)
=========================================================================
This is the same secure, persistent group-chat application as before — global
and private rooms, presence, room codes, AES-GCM encryption at rest, Ed25519
signatures and tamper detection — extended so that three independent nodes
serve one shared conversation behind a load balancer.

Endpoints
---------
POST /message   {"client-name": ..., "msg": ...}   required assignment route
GET  /feed                                          required assignment route
GET  /health                                        liveness + load report used by the LB
GET  /stats                                         detailed node diagnostics
WS   /ws                                            full chat protocol (unchanged)

Every node reads and writes the same PostgreSQL database, so `/feed` returns
the same conversation no matter which node the load balancer picked.
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
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Dict, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response

server_dir = Path(__file__).parent
for extra in (str(server_dir), str(server_dir.parent)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

try:
    import db_async as db
except ImportError:
    from server import db_async as db

try:
    import crypto_utils as crypto
except ImportError:
    from server import crypto_utils as crypto

try:
    from common import sysmetrics
except ImportError:
    sys.path.insert(0, str(server_dir.parent / "common"))
    import sysmetrics


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
log = logging.getLogger("backend")

NODE_ID = os.environ.get("NODE_ID", "Sys?")
MY_PORT = int(os.environ.get("PORT", "4000"))
HTTP_ROOM_ID = "global"

# Feed cache ceiling. Keeps a node inside its 512 MB container even if the load
# generator posts for a very long time; oldest messages are dropped from the
# in-memory cache first (they remain in PostgreSQL).
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "150000"))

# Write-behind buffer: POST /message adds here; background flusher does the DB
# INSERT in batches so the HTTP response is not blocked by PostgreSQL latency.
_write_queue: asyncio.Queue = asyncio.Queue(maxsize=0)  # unbounded
WRITE_FLUSH_INTERVAL = float(os.environ.get("WRITE_FLUSH_MS", "50")) / 1000.0
WRITE_BATCH_SIZE = int(os.environ.get("WRITE_BATCH_SIZE", "200"))

# Auto-prune: delete the oldest DB rows every N seconds (default 20 min).
PRUNE_INTERVAL_S = int(os.environ.get("FEED_PRUNE_INTERVAL_S", "1200"))
PRUNE_KEEP_COUNT = int(os.environ.get("FEED_PRUNE_KEEP", "50000"))

JSON_HEADERS = {"Access-Control-Allow-Origin": "*"}

app = FastAPI(title="Distributed Secure Group Chat — Backend Node", version="4.0.0")


# ── Live load accounting ─────────────────────────────────────────────────────
class NodeLoad:
    """Per-node counters the load balancer consumes through /health."""

    def __init__(self) -> None:
        self.inflight = 0
        self.total_requests = 0
        self.total_messages = 0
        self.duplicates = 0
        self.errors = 0
        self.ewma_latency_ms = 0.0
        self.started = time.time()

    def observe(self, latency_ms: float) -> None:
        # EWMA with alpha=0.1 — smooth enough not to flap, fast enough to react.
        self.ewma_latency_ms = (
            latency_ms if self.ewma_latency_ms == 0.0
            else 0.9 * self.ewma_latency_ms + 0.1 * latency_ms
        )


load = NodeLoad()


class LoadTrackingMiddleware:
    """Raw ASGI middleware that keeps the per-node load counters current.

    Deliberately not `@app.middleware("http")`: that wraps every request in a
    Starlette BaseHTTPMiddleware, which spawns an anyio task group and a message
    queue per request. On a one-core container that overhead is a measurable
    fraction of the request budget. This does the same bookkeeping with a plain
    function call, and passes lifespan and websocket scopes straight through.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        load.inflight += 1
        load.total_requests += 1
        t0 = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            load.inflight -= 1
            load.observe((time.perf_counter() - t0) * 1000.0)


# ── WebSocket room manager (unchanged chat semantics) ────────────────────────
class RoomConnectionManager:
    def __init__(self) -> None:
        self._clients: Dict[WebSocket, dict] = {}
        self._room_members: Dict[str, Set[WebSocket]] = {}

    def client_count(self) -> int:
        return len(self._clients)

    async def _send(self, ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def broadcast_to_room(self, room_id: str, payload: dict,
                                exclude: WebSocket | None = None) -> None:
        for ws in list(self._room_members.get(room_id, ())):
            if ws is not exclude:
                await self._send(ws, payload)

    async def broadcast_raw(self, room_id: str, raw: str,
                            exclude: WebSocket | None = None) -> None:
        for ws in list(self._room_members.get(room_id, ())):
            if ws is not exclude:
                try:
                    await ws.send_text(raw)
                except Exception:
                    pass

    def get_room_users(self, room_id: str) -> list[str]:
        users = []
        for ws in self._room_members.get(room_id, ()):
            info = self._clients.get(ws)
            if info and info.get("username"):
                users.append(info["username"])
        return users

    async def register_user(self, ws: WebSocket, username: str) -> str:
        client_id = str(uuid.uuid4())[:8]
        self._clients[ws] = {"id": client_id, "username": username, "room_id": None}
        await self.switch_room(ws, "global")
        return client_id

    async def switch_room(self, ws: WebSocket, target_room_id: str) -> bool:
        client = self._clients.get(ws)
        if not client:
            return False

        current_room_id = client.get("room_id")
        username = client["username"]
        room_info = await db.get_room_by_id(target_room_id)
        if not room_info:
            await self._send(ws, {"type": "error", "message": "Room not found"})
            return False

        if current_room_id and current_room_id in self._room_members:
            self._room_members[current_room_id].discard(ws)
            if not self._room_members[current_room_id]:
                del self._room_members[current_room_id]
            await self.broadcast_to_room(current_room_id, {
                "type": "user_left", "roomId": current_room_id, "username": username})

        client["room_id"] = target_room_id
        self._room_members.setdefault(target_room_id, set()).add(ws)
        log.info("User %s switched to room %s (%s)", username, room_info["name"], target_room_id)

        history = await db.get_room_messages(target_room_id)
        room_users = self.get_room_users(target_room_id)
        await self._send(ws, {
            "type": "room_entered",
            "room": room_info,
            "history": history,
            "users": [u for u in room_users if u != username],
            "node_id": NODE_ID,
        })
        await self.broadcast_to_room(target_room_id, {
            "type": "user_joined", "roomId": target_room_id, "username": username},
            exclude=ws)
        return True

    async def disconnect(self, ws: WebSocket) -> None:
        client = self._clients.pop(ws, None)
        if not client:
            return
        room_id = client.get("room_id")
        username = client.get("username")
        if room_id and room_id in self._room_members:
            self._room_members[room_id].discard(ws)
            if not self._room_members[room_id]:
                del self._room_members[room_id]
            if username:
                log.info("User left: %s from room %s", username, room_id)
                await self.broadcast_to_room(room_id, {
                    "type": "user_left", "roomId": room_id, "username": username})

    async def handle_message(self, ws: WebSocket, text: str,
                             message_id: Optional[str] = None) -> None:
        client = self._clients.get(ws)
        if not client:
            return
        room_id = client["room_id"]
        record = await db.save_message(
            room_id=room_id, sender=client["username"], sender_id=client["id"],
            text=text, timestamp=int(time.time() * 1000), message_id=message_id)
        if record["duplicate"]:
            return
        await db.feed_cache.add_local(record)
        await self.broadcast_to_room(room_id, {
            "type": "message",
            "id": record["id"],
            "roomId": room_id,
            "sender": record["sender"],
            "senderId": record["senderId"],
            "text": record["text"],
            "timestamp": record["timestamp"],
            "verified": True,
            "tampered": False,
        })


manager = RoomConnectionManager()


# ── Cross-node fan-out ───────────────────────────────────────────────────────
# A message posted through Sys3 must reach WebSocket users attached to Sys2 and
# Sys4. Rather than paying for a NOTIFY on every insert, this pump runs only
# while this node actually has WebSocket clients: it reuses the feed cache's
# incremental sync and forwards anything new to local sockets. During a pure
# HTTP load test (no WS clients) it costs nothing at all.
class FanoutPump:
    def __init__(self, interval: float = 0.25) -> None:
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._cursor = 0

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._cursor = db.feed_cache.item_count()
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while manager.client_count() > 0:
                await asyncio.sleep(self.interval)
                try:
                    self._cursor, new_items = await db.feed_cache.items_since(self._cursor)
                except Exception as exc:
                    log.warning("fanout sync failed: %s", exc)
                    continue
                for blob in new_items:
                    try:
                        payload = json.loads(blob)
                    except Exception:
                        continue
                    room_id = payload.get("roomId")
                    if room_id in manager._room_members:
                        await manager.broadcast_raw(room_id, json.dumps({
                            "type": "message",
                            "id": payload.get("id"),
                            "roomId": room_id,
                            "sender": payload.get("sender"),
                            "senderId": payload.get("senderId"),
                            "text": payload.get("text"),
                            "timestamp": payload.get("timestamp"),
                            "verified": payload.get("verified", True),
                            "tampered": payload.get("tampered", False),
                            "remote": True,
                        }))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("fanout pump stopped: %s", exc)


fanout = FanoutPump()


# ── Write-behind flusher ────────────────────────────────────────────────────
async def _write_flusher() -> None:
    """Drain _write_queue in batches and INSERT into PostgreSQL.

    Runs forever in the background.  Decouples HTTP response latency from
    database write latency — callers enqueue and return immediately.
    """
    while True:
        try:
            batch = []
            while not _write_queue.empty() and len(batch) < 1000:
                try:
                    batch.append(_write_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if batch:
                try:
                    await db.batch_insert(batch)
                except Exception as exc:
                    log.warning("write-behind flush failed (%d records): %s", len(batch), exc)
                # If there are still more items waiting, don't sleep — continue immediately
                if not _write_queue.empty():
                    await asyncio.sleep(0)
                    continue
            await asyncio.sleep(WRITE_FLUSH_INTERVAL)
        except asyncio.CancelledError:
            # Flush whatever is left before exiting.
            remaining = []
            while not _write_queue.empty():
                try:
                    remaining.append(_write_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if remaining:
                try:
                    await db.batch_insert(remaining)
                except Exception:
                    pass
            raise
        except Exception as exc:
            log.warning("write flusher error: %s", exc)


async def _feed_pruner() -> None:
    """Every PRUNE_INTERVAL_S seconds delete the oldest DB rows.

    Keeps only the newest PRUNE_KEEP_COUNT messages on disk so that the DB
    never bloats between leaderboard runs.  The in-memory cache is unaffected
    (it has its own ceiling and eviction), so /feed continues to serve the
    full history that was received during the current uptime window.
    """
    while True:
        try:
            await asyncio.sleep(PRUNE_INTERVAL_S)
            deleted = await db.prune_old_messages(keep_count=PRUNE_KEEP_COUNT)
            if deleted:
                log.info("feed pruner: deleted %d old messages from DB (keeping %d)",
                         deleted, PRUNE_KEEP_COUNT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("feed pruner error: %s", exc)


# ── Lifecycle ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    await db.init_pool()
    db.feed_cache.max_items = FEED_MAX_ITEMS
    await db.feed_cache.sync(force=True)
    sysmetrics.snapshot()  # prime the CPU sampler
    asyncio.create_task(_write_flusher())
    if NODE_ID == "Sys2":
        asyncio.create_task(_feed_pruner())
    log.info("Backend %s ready on port %s (feed cached: %d messages)",
             NODE_ID, MY_PORT, db.feed_cache.item_count())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await db.close_pool()


# ── Required assignment routes ───────────────────────────────────────────────
@app.get("/health")
async def health() -> Response:
    """Liveness plus the load signals the balancer scores backends on."""
    m = sysmetrics.snapshot()
    body = json.dumps({
        "status": "ok",
        "service": "Backend",
        "node_id": NODE_ID,
        "cpu": m["cpu"],
        "mem": m["mem"],
        "load1": m["load1"],
        "cpu_cores": m["cpu_cores"],
        "inflight": load.inflight,
        "ewma_latency_ms": round(load.ewma_latency_ms, 3),
        "total_requests": load.total_requests,
        "total_messages": load.total_messages,
        "duplicates": load.duplicates,
        "ws_clients": manager.client_count(),
        "uptime_s": round(time.time() - load.started, 1),
    }, separators=(",", ":"))
    return Response(content=body, media_type="application/json", headers=JSON_HEADERS)


async def _extract_message(request: Request) -> tuple[str, str, Optional[str]]:
    """Accepts JSON, form-encoded or query-string input, in that order."""
    client_name = text = message_id = None
    ctype = request.headers.get("content-type", "")
    raw = await request.body()

    if raw:
        if "application/json" in ctype or raw[:1] in (b"{", b"["):
            try:
                body = json.loads(raw)
                if isinstance(body, dict):
                    client_name = body.get("client-name") or body.get("client_name") or body.get("clientName")
                    text = body.get("msg") or body.get("message") or body.get("text")
                    message_id = (body.get("message_id") or body.get("messageId")
                                  or body.get("id") or body.get("msg_id"))
            except Exception:
                pass
        if client_name is None and text is None:
            try:
                # request.body() above already consumed the ASGI receive stream,
                # so request.form() would return empty. Parse raw bytes directly.
                from urllib.parse import parse_qs
                parsed = parse_qs(raw.decode("latin-1"), keep_blank_values=True)
                client_name = (parsed.get("client-name") or parsed.get("client_name") or [None])[0]
                text = (parsed.get("msg") or parsed.get("message") or [None])[0]
                message_id = (parsed.get("message_id") or parsed.get("messageId") or [None])[0]
            except Exception:
                pass

    qp = request.query_params
    client_name = client_name or qp.get("client-name") or qp.get("client_name")
    text = text if text is not None else (qp.get("msg") or qp.get("message"))
    message_id = message_id or qp.get("message_id")

    # The balancer stamps a stable id so that a request it retries against a
    # second backend cannot be stored twice.
    message_id = message_id or request.headers.get("X-Message-Id")

    return (str(client_name).strip()[:64] if client_name else "Anonymous",
            str(text) if text is not None else "",
            str(message_id) if message_id else None)


# The assignment fixes the path but not the verb or the encoding, so accept the
# shapes a load generator might reasonably use: POST or GET, JSON, form-encoded
# or query string, with or without a trailing slash.
@app.post("/message")
@app.get("/message")
@app.post("/message/")
@app.get("/message/")
async def post_message(request: Request) -> Response:
    """Accept a message, add it to the in-memory feed immediately, and enqueue
    the DB write for the background flusher.  The caller gets a 200 response
    in ~1 ms regardless of current database load.
    """
    client_name, text, message_id = await _extract_message(request)
    if not text.strip():
        load.errors += 1
        return Response(
            content=json.dumps({"status": "error", "message": "msg cannot be empty",
                                "node_id": NODE_ID}),
            status_code=400, media_type="application/json", headers=JSON_HEADERS)

    # ── Fast path: encrypt + sign in memory, no DB round-trip ────────────────
    msg_id = str(message_id) if message_id else str(uuid.uuid4())
    ts = int(time.time() * 1000)
    ciphertext, nonce = crypto.encrypt_message(text)
    signature, public_key = crypto.sign_and_pubkey(client_name, text)


    record = {
        "id": msg_id,
        "seq": None,
        "room_id": HTTP_ROOM_ID,
        "sender": client_name,
        "senderId": f"http-{client_name}",
        "text": text,
        "timestamp": ts,
        "verified": True,
        "tampered": False,
        "duplicate": False,
        # Fields needed by batch_insert
        "message_id": msg_id,
        "sender_id": f"http-{client_name}",
        "ciphertext": ciphertext,
        "nonce": nonce,
        "signature": signature,
        "sender_public_key": public_key,
        "origin_node": NODE_ID,
    }

    # Add to feed cache immediately so /feed reflects it without waiting for DB.
    await db.feed_cache.add_local(record)
    load.total_messages += 1

    # Enqueue DB write — the _write_flusher coroutine will batch-insert shortly.
    _write_queue.put_nowait(record)

    if manager.client_count():
        await manager.broadcast_to_room(HTTP_ROOM_ID, {
            "type": "message",
            "id": msg_id,
            "roomId": HTTP_ROOM_ID,
            "sender": client_name,
            "senderId": record["senderId"],
            "text": text,
            "timestamp": ts,
            "verified": True,
            "tampered": False,
        })

    body = json.dumps({
        "status": "ok",
        "message_id": msg_id,
        "duplicate": False,
        "timestamp": ts,
        "node_id": NODE_ID,
    }, separators=(",", ":"))
    return Response(content=body, media_type="application/json", headers=JSON_HEADERS)


@app.get("/feed")
@app.get("/feed/")
async def get_feed(request: Request, limit: int = 0) -> Response:
    """Every message in the shared conversation, oldest first."""
    if limit and limit > 0:
        body = await db.feed_cache.response_limited(NODE_ID, limit)
        return Response(content=body, media_type="application/json", headers=JSON_HEADERS)

    gzip_ok = "gzip" in request.headers.get("accept-encoding", "")
    body = await db.feed_cache.response_bytes(NODE_ID, gzipped=gzip_ok)
    headers = dict(JSON_HEADERS)
    if gzip_ok:
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(content=body, media_type="application/json", headers=headers)


@app.get("/stats")
async def stats() -> Response:
    m = sysmetrics.snapshot()
    body = json.dumps({
        "node_id": NODE_ID,
        "system": m,
        "load": {
            "inflight": load.inflight,
            "total_requests": load.total_requests,
            "total_messages": load.total_messages,
            "duplicates": load.duplicates,
            "errors": load.errors,
            "ewma_latency_ms": round(load.ewma_latency_ms, 3),
        },
        "feed_cache": db.feed_cache.stats(),
        "ws_clients": manager.client_count(),
        "db_rows": await db.count_messages(),
    }, indent=2)
    return Response(content=body, media_type="application/json", headers=JSON_HEADERS)


# ── WebSocket chat (full original protocol) ──────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    username: Optional[str] = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")

            if msg_type == "join":
                username = str(msg.get("username", "Anonymous")).strip()[:20]
                await manager.register_user(ws, username)
                fanout.ensure_running()

            elif msg_type == "create_room":
                if not username:
                    continue
                room_name = str(msg.get("name", "Private Room")).strip()[:30] or "Private Room"
                new_room = await db.create_room(room_name, is_private=True)
                log.info("Room created: %s (code=%s)", new_room["name"], new_room["code"])
                await ws.send_text(json.dumps({"type": "room_created", "room": new_room}))
                await manager.switch_room(ws, new_room["id"])

            elif msg_type == "join_room":
                if not username:
                    continue
                target_code = str(msg.get("code", "")).strip().upper()
                target_id = str(msg.get("roomId", "")).strip()
                room = None
                if target_code:
                    room = await db.get_room_by_code(target_code)
                elif target_id:
                    room = await db.get_room_by_id(target_id)
                if room:
                    await manager.switch_room(ws, room["id"])
                else:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": (f"Room code '{target_code}' not found."
                                    if target_code else "Room not found."),
                    }))

            elif msg_type == "switch_room":
                if not username:
                    continue
                await manager.switch_room(ws, str(msg.get("roomId", "global")).strip())

            elif msg_type == "message":
                if not username:
                    continue
                text = str(msg.get("text", "")).strip()
                if text:
                    await manager.handle_message(ws, text, msg.get("message_id"))

    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as exc:
        log.warning("websocket error: %s", exc)
        await manager.disconnect(ws)


# The ASGI entrypoint uvicorn serves (`server.main:asgi_app`).
asgi_app = LoadTrackingMiddleware(app)
