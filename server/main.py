"""
main.py — FastAPI WebSocket + HTTP backend with Persistence & Auto-Registration
==============================================================================
WebSocket : ws://<host>:<port>/ws
Health    : GET  http://<host>:<port>/health
Message   : POST http://<host>:<port>/message   {"client-name": "...", "msg": "...", "message_id": "..." (optional)}
Feed      : GET  http://<host>:<port>/feed
"""

from __future__ import annotations

import os
import sys
import json
import time
import uuid
import logging
import asyncio
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.getLogger("asyncio").setLevel(logging.CRITICAL)

server_dir = Path(__file__).parent
if str(server_dir) not in sys.path:
    sys.path.insert(0, str(server_dir))

try:
    import database as db
except ImportError:
    from server import database as db

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Node configuration from environment
NODE_ID = os.environ.get("NODE_ID", "Sys2")
MY_HOST = os.environ.get("MY_HOST", "127.0.0.1")
MY_PORT = int(os.environ.get("PORT", os.environ.get("BACKEND_PORT", 8000)))
LB_HOST = os.environ.get("LB_HOST", "127.0.0.1")
LB_PORT = int(os.environ.get("LB_PORT", 8000))
AUTO_REGISTER = os.environ.get("AUTO_REGISTER", "1") == "1"

# ── Initialize Database ──────────────────────────────────────────────────────
try:
    db.init_db()
except Exception as e:
    log.warning(f"Database init warning: {e}")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Distributed Group Chat Server", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HTTP_ROOM_ID = "global"


# ── Auto-Registration & Heartbeat Thread ─────────────────────────────────────
def _register_with_load_balancer():
    """Announces this backend to the Load Balancer so it is dynamically discovered."""
    if not AUTO_REGISTER:
        return

    register_url = f"http://{LB_HOST}:{LB_PORT}/register"
    payload = json.dumps({
        "id": NODE_ID,
        "host": MY_HOST,
        "port": MY_PORT,
        "weight": 1
    }).encode("utf-8")

    def _loop():
        # Small delay to let LB / network come up
        time.sleep(1.0)
        while True:
            try:
                req = urllib.request.Request(
                    register_url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        log.info(f"Registered with Load Balancer at {register_url} as {NODE_ID}")
            except Exception:
                pass
            time.sleep(10.0)

    t = threading.Thread(target=_loop, name="LB-AutoRegister", daemon=True)
    t.start()


@app.on_event("startup")
async def startup_event():
    _register_with_load_balancer()


# ── Request/response models for the required HTTP routes ────────────────────
class MessageIn(BaseModel):
    client_name: str = Field(..., alias="client-name")
    msg: str
    message_id: Optional[str] = None

    class Config:
        populate_by_name = True


# ── Connection Manager ────────────────────────────────────────────────────────
class RoomConnectionManager:
    """Manages WebSocket connections partitioned by room_id."""

    def __init__(self) -> None:
        self._clients: Dict[WebSocket, dict] = {}
        self._room_members: Dict[str, Set[WebSocket]] = {}

    async def _send(self, ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def broadcast_to_room(self, room_id: str, payload: dict, exclude: WebSocket | None = None) -> None:
        members = list(self._room_members.get(room_id, set()))
        for ws in members:
            if ws is not exclude:
                await self._send(ws, payload)

    def get_room_users(self, room_id: str) -> list[str]:
        members = self._room_members.get(room_id, set())
        users = []
        for ws in members:
            info = self._clients.get(ws)
            if info and info.get("username"):
                users.append(info["username"])
        return users

    async def register_user(self, ws: WebSocket, username: str) -> str:
        client_id = str(uuid.uuid4())[:8]
        self._clients[ws] = {
            "id": client_id,
            "username": username,
            "room_id": "global"
        }
        await self.switch_room(ws, "global")
        return client_id

    async def switch_room(self, ws: WebSocket, target_room_id: str) -> bool:
        client = self._clients.get(ws)
        if not client:
            return False

        current_room_id = client.get("room_id")
        username = client["username"]
        room_info = db.get_room_by_id(target_room_id)

        if not room_info:
            await self._send(ws, {"type": "error", "message": "Room not found"})
            return False

        if current_room_id and current_room_id in self._room_members:
            self._room_members[current_room_id].discard(ws)
            if not self._room_members[current_room_id]:
                del self._room_members[current_room_id]
            await self.broadcast_to_room(current_room_id, {
                "type": "user_left",
                "roomId": current_room_id,
                "username": username
            })

        client["room_id"] = target_room_id
        if target_room_id not in self._room_members:
            self._room_members[target_room_id] = set()
        self._room_members[target_room_id].add(ws)

        log.info("User %s switched to room: %s (%s)", username, room_info["name"], target_room_id)

        history = db.get_room_messages(target_room_id)
        room_users = self.get_room_users(target_room_id)

        await self._send(ws, {
            "type": "room_entered",
            "room": room_info,
            "history": history,
            "users": [u for u in room_users if u != username]
        })

        await self.broadcast_to_room(target_room_id, {
            "type": "user_joined",
            "roomId": target_room_id,
            "username": username
        }, exclude=ws)

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
                    "type": "user_left",
                    "roomId": room_id,
                    "username": username
                })

    async def handle_message(self, ws: WebSocket, text: str) -> None:
        client = self._clients.get(ws)
        if not client:
            return

        room_id = client["room_id"]
        username = client["username"]
        sender_id = client["id"]
        now = int(time.time() * 1000)

        msg_record = db.save_message(room_id, username, sender_id, text, now)

        payload = {
            "type": "message",
            "roomId": room_id,
            "sender": username,
            "senderId": sender_id,
            "text": msg_record["text"],
            "timestamp": now,
            "verified": msg_record["verified"],
            "tampered": msg_record["tampered"],
        }
        await self.broadcast_to_room(room_id, payload)

    async def broadcast_http_message(self, room_id: str, payload: dict) -> None:
        await self.broadcast_to_room(room_id, payload)


manager = RoomConnectionManager()


# ── HTTP Routes (required by assignment spec) ────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "Backend",
        "node_id": NODE_ID,
        "total_clients": len(manager._clients)
    }


@app.post("/message")
async def post_message(request: Request) -> dict:
    """
    Accepts {"client-name": ..., "msg": ...} (message_id optional).
    Also supports form-urlencoded / raw JSON payloads.
    Persists with UUID deduplication and broadcasts to global room.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    client_name = str(body.get("client-name") or body.get("client_name") or "Anonymous").strip()[:50]
    text = str(body.get("msg") or body.get("message") or body.get("text") or "").strip()
    message_id = body.get("message_id") or body.get("messageId")
    now = int(time.time() * 1000)

    if not text:
        return {"status": "error", "message": "msg cannot be empty"}

    sender_id = f"http-{client_name}"

    msg_record = db.save_message(
        room_id=HTTP_ROOM_ID,
        sender=client_name,
        sender_id=sender_id,
        text=text,
        timestamp=now,
        message_id=str(message_id) if message_id else None,
    )

    if not msg_record.get("duplicate"):
        await manager.broadcast_http_message(HTTP_ROOM_ID, {
            "type": "message",
            "roomId": HTTP_ROOM_ID,
            "sender": client_name,
            "senderId": sender_id,
            "text": msg_record["text"],
            "timestamp": msg_record["timestamp"],
            "verified": msg_record["verified"],
            "tampered": msg_record["tampered"],
        })

    return {
        "status": "ok",
        "message_id": msg_record["id"],
        "duplicate": msg_record.get("duplicate", False),
        "timestamp": msg_record["timestamp"],
        "node_id": NODE_ID
    }


@app.get("/feed")
async def get_feed(limit: int = 500) -> dict:
    """Returns all messages across the shared chat."""
    messages = db.get_all_messages(limit=limit)
    return {"status": "ok", "count": len(messages), "messages": messages, "node_id": NODE_ID}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    username: str | None = None

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

            elif msg_type == "create_room":
                if not username:
                    continue
                room_name = str(msg.get("name", "Private Room")).strip()[:30] or "Private Room"
                new_room = db.create_room(room_name, is_private=True)
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
                    room = db.get_room_by_code(target_code)
                elif target_id:
                    room = db.get_room_by_id(target_id)

                if room:
                    await manager.switch_room(ws, room["id"])
                else:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"Room code '{target_code}' not found." if target_code else "Room not found."
                    }))

            elif msg_type == "switch_room":
                if not username:
                    continue
                target_id = str(msg.get("roomId", "global")).strip()
                await manager.switch_room(ws, target_id)

            elif msg_type == "message":
                if not username:
                    continue
                text = str(msg.get("text", "")).strip()
                if text:
                    await manager.handle_message(ws, text)

    except WebSocketDisconnect:
        await manager.disconnect(ws)