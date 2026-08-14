"""
main.py — FastAPI WebSocket backend with Private Rooms & Persistence
===================================================================
Endpoint : ws://localhost:8000/ws
Health   : GET http://localhost:8000/health
"""

from __future__ import annotations

import json
import time
import uuid
import logging
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import sys
from pathlib import Path
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

# ── Initialize Database ──────────────────────────────────────────────────────
db.init_db()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Group Chat Server with Private Rooms", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Connection Manager ────────────────────────────────────────────────────────
class RoomConnectionManager:
    """Manages WebSocket connections partitioned by room_id."""

    def __init__(self) -> None:
        # Maps WebSocket → {"id": str, "username": str, "room_id": str}
        self._clients: Dict[WebSocket, dict] = {}
        # Maps room_id → set of WebSockets
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

        # 1. Leave current room if in one
        if current_room_id and current_room_id in self._room_members:
            self._room_members[current_room_id].discard(ws)
            if not self._room_members[current_room_id]:
                del self._room_members[current_room_id]
            # Notify old room
            await self.broadcast_to_room(current_room_id, {
                "type": "user_left",
                "roomId": current_room_id,
                "username": username
            })

        # 2. Join target room
        client["room_id"] = target_room_id
        if target_room_id not in self._room_members:
            self._room_members[target_room_id] = set()
        self._room_members[target_room_id].add(ws)

        log.info("User %s switched to room: %s (%s)", username, room_info["name"], target_room_id)

        # 3. Send room info + room history + current user list to client
        history = db.get_room_messages(target_room_id)
        room_users = self.get_room_users(target_room_id)

        await self._send(ws, {
            "type": "room_entered",
            "room": room_info,
            "history": history,
            "users": [u for u in room_users if u != username]
        })

        # 4. Broadcast user_joined to new room
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

        # Save to SQLite DB
        msg_record = db.save_message(room_id, username, sender_id, text, now)

        # Broadcast to room members
        payload = {
            "type": "message",
            "roomId": room_id,
            "sender": username,
            "senderId": sender_id,
            "text": text,
            "timestamp": now
        }
        await self.broadcast_to_room(room_id, payload)


manager = RoomConnectionManager()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "total_clients": len(manager._clients)}


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
                
                # Send confirmation and automatically join the room
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
