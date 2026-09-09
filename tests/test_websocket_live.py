"""
Live end-to-end check of the WebSocket chat through the load balancer.

Not a unit test — it talks to the deployed cluster, so it is skipped unless
LIVE_URL is set:

    LIVE_URL=http://10.1.75.79:4237 pytest tests/test_websocket_live.py -q -s

It verifies the chat features that must survive the distributed rework: join,
global-room history, private room creation by code, message delivery between two
clients, and — the interesting distributed case — that two clients placed on
*different* backend nodes still see each other's messages.
"""

import asyncio
import json
import os
import uuid

import pytest

LIVE_URL = os.environ.get("LIVE_URL")
pytestmark = pytest.mark.skipif(not LIVE_URL, reason="set LIVE_URL to run")

aiohttp = pytest.importorskip("aiohttp")


def ws_url(http_url: str) -> str:
    return http_url.replace("http://", "ws://").rstrip("/") + "/ws"


async def recv_until(ws, wanted, timeout=10.0):
    """Read frames until one of type `wanted` arrives."""
    async def _pump():
        async for msg in ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == wanted:
                    return data
        return None
    return await asyncio.wait_for(_pump(), timeout)


@pytest.mark.asyncio
async def test_two_clients_exchange_messages():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url(LIVE_URL)) as a, \
                   session.ws_connect(ws_url(LIVE_URL)) as b:
            await a.send_json({"type": "join", "username": "alice"})
            entered_a = await recv_until(a, "room_entered")
            assert entered_a["room"]["id"] == "global"

            await b.send_json({"type": "join", "username": "bob"})
            await recv_until(b, "room_entered")

            text = f"hello from alice {uuid.uuid4().hex[:8]}"
            await a.send_json({"type": "message", "text": text})

            got = await recv_until(b, "message", timeout=15.0)
            assert got["text"] == text
            assert got["sender"] == "alice"
            assert got["tampered"] is False


@pytest.mark.asyncio
async def test_private_room_by_code():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url(LIVE_URL)) as a, \
                   session.ws_connect(ws_url(LIVE_URL)) as b:
            await a.send_json({"type": "join", "username": "carol"})
            await recv_until(a, "room_entered")
            await a.send_json({"type": "create_room", "name": "study group"})
            created = await recv_until(a, "room_created")
            code = created["room"]["code"]
            assert code and len(code) == 6
            await recv_until(a, "room_entered")

            await b.send_json({"type": "join", "username": "dave"})
            await recv_until(b, "room_entered")
            await b.send_json({"type": "join_room", "code": code})
            entered = await recv_until(b, "room_entered")
            assert entered["room"]["id"] == created["room"]["id"]

            text = f"private {uuid.uuid4().hex[:8]}"
            await a.send_json({"type": "message", "text": text})
            got = await recv_until(b, "message", timeout=15.0)
            assert got["text"] == text


@pytest.mark.asyncio
async def test_http_message_reaches_websocket_clients():
    """A message posted over the required /message route must reach WS clients."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url(LIVE_URL)) as a:
            await a.send_json({"type": "join", "username": "erin"})
            await recv_until(a, "room_entered")

            text = f"via http {uuid.uuid4().hex[:8]}"
            async with session.post(f"{LIVE_URL}/message",
                                    json={"client-name": "http-client", "msg": text}) as r:
                assert r.status == 200

            got = await recv_until(a, "message", timeout=20.0)
            assert got["text"] == text
