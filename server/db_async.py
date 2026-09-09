"""
db_async.py — asyncpg persistence layer shared by every backend node
====================================================================
All three backends (Sys2, Sys3, Sys4) talk to ONE PostgreSQL instance hosted on
Sys1, so every node reads and writes exactly the same chat data.

Guarantees provided here
------------------------
* Persistence      : PostgreSQL on disk; survives backend restarts.
* Unique message id: `message_id TEXT PRIMARY KEY` (UUIDv4 when the client does
                     not supply one).
* Exactly-once     : `INSERT ... ON CONFLICT (message_id) DO NOTHING`. A retried
                     or replayed message with an id already stored is a no-op,
                     and the API reports `duplicate: true` instead of inserting
                     a second row.
* Encryption       : ciphertext + nonce + Ed25519 signature are what actually
                     hit the disk; plaintext is never stored.

Feed cache
----------
`/feed` must return every message, which is an O(N) response. Re-reading and
re-decrypting the whole table per request does not scale, so each worker keeps
an append-only cache of already-serialised messages and only pulls rows it has
not seen yet (`seq > watermark`). The watermark is deliberately rewound by
`SEQ_LOOKBACK` because BIGSERIAL values are handed out before commit and can
therefore commit slightly out of order; the `_seen` id set makes the re-read
idempotent.
"""

from __future__ import annotations

import os
import json
import time
import uuid
import random
import string
import gzip
import asyncio
import logging
from typing import Any, Dict, List, Optional

import asyncpg

try:
    import crypto_utils as crypto
except ImportError:  # pragma: no cover - import style depends on entrypoint
    from server import crypto_utils as crypto

log = logging.getLogger("db")

DB_HOST = os.environ.get("DB_HOST", "172.17.0.38")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "chatdb")
DB_USER = os.environ.get("DB_USER", "chatuser")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

POOL_MIN = int(os.environ.get("DB_POOL_MIN", "8"))
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "40"))

# How far to rewind the incremental feed watermark on every sync (see docstring).
# BIGSERIAL numbers are handed out before commit, so a row can commit just after
# a higher-numbered one. That window is sub-millisecond and bounded by the number
# of concurrent writers (three nodes x a small pool), so a couple of hundred rows
# is a generous safety margin — and, unlike a large value, it keeps each sync a
# short indexed range scan instead of a full table read.
SEQ_LOOKBACK = int(os.environ.get("FEED_SEQ_LOOKBACK", "256"))

# Minimum spacing between database syncs. Without this every concurrent /feed
# request would issue its own query; with it the workers coalesce onto one query
# per window, bounding database load at the cost of at most this much staleness
# for messages written on *other* nodes (a node's own writes are added locally
# and are visible immediately).
SYNC_MIN_INTERVAL = float(os.environ.get("FEED_SYNC_MIN_MS", "20")) / 1000.0

NODE_ID = os.environ.get("NODE_ID", "Sys?")
GLOBAL_ROOM = "global"

_pool: Optional[asyncpg.Pool] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    code        TEXT UNIQUE,
    is_private  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    seq               BIGSERIAL,
    message_id        TEXT PRIMARY KEY,
    room_id           TEXT NOT NULL,
    sender            TEXT NOT NULL,
    sender_id         TEXT NOT NULL,
    ciphertext        TEXT NOT NULL,
    nonce             TEXT NOT NULL,
    signature         TEXT NOT NULL,
    sender_public_key TEXT NOT NULL,
    timestamp         BIGINT NOT NULL,
    origin_node       TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_seq      ON messages (seq);
CREATE INDEX IF NOT EXISTS idx_messages_room_seq ON messages (room_id, seq);

INSERT INTO rooms (id, name, code, is_private, created_at)
VALUES ('global', 'General Room', NULL, FALSE, 0)
ON CONFLICT (id) DO NOTHING;
"""


async def init_pool() -> asyncpg.Pool:
    """Create the connection pool and make sure the schema exists."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
        min_size=POOL_MIN, max_size=POOL_MAX,
        command_timeout=15.0,
        max_inactive_connection_lifetime=300.0,
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
    log.info("PostgreSQL pool ready at %s:%s/%s (min=%d max=%d)",
             DB_HOST, DB_PORT, DB_NAME, POOL_MIN, POOL_MAX)
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ── Rooms ────────────────────────────────────────────────────────────────────

def _generate_room_code(length: int = 6) -> str:
    chars = [c for c in (string.ascii_uppercase + string.digits) if c not in "O0I1"]
    return "".join(random.choices(chars, k=length))


async def create_room(name: str, is_private: bool = True) -> Dict[str, Any]:
    room_id = f"room_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    now = int(time.time() * 1000)
    async with pool().acquire() as conn:
        for _ in range(5):
            code = _generate_room_code() if is_private else None
            try:
                await conn.execute(
                    "INSERT INTO rooms (id, name, code, is_private, created_at) VALUES ($1,$2,$3,$4,$5)",
                    room_id, name.strip(), code, is_private, now,
                )
                break
            except asyncpg.UniqueViolationError:
                continue  # room code collision, try another
        else:
            raise RuntimeError("could not allocate a unique room code")
    return {"id": room_id, "name": name.strip(), "code": code,
            "is_private": is_private, "created_at": now}


def _room_row(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "code": row["code"],
            "is_private": bool(row["is_private"]), "created_at": row["created_at"]}


async def get_room_by_id(room_id: str) -> Optional[Dict[str, Any]]:
    async with pool().acquire() as conn:
        return _room_row(await conn.fetchrow("SELECT * FROM rooms WHERE id = $1", room_id))


async def get_room_by_code(code: str) -> Optional[Dict[str, Any]]:
    async with pool().acquire() as conn:
        return _room_row(await conn.fetchrow(
            "SELECT * FROM rooms WHERE UPPER(code) = $1", code.strip().upper()))


async def get_all_rooms() -> List[Dict[str, Any]]:
    async with pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM rooms ORDER BY created_at ASC")
    return [_room_row(r) for r in rows]


# ── Messages ─────────────────────────────────────────────────────────────────

INSERT_SQL = """
INSERT INTO messages
    (message_id, room_id, sender, sender_id, ciphertext, nonce,
     signature, sender_public_key, timestamp, origin_node)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
ON CONFLICT (message_id) DO NOTHING
RETURNING seq
"""


async def save_message(
    room_id: str,
    sender: str,
    sender_id: str,
    text: str,
    timestamp: int,
    message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Encrypt, sign and persist one message. Idempotent on `message_id`."""
    msg_id = str(message_id) if message_id else str(uuid.uuid4())
    ciphertext, nonce = crypto.encrypt_message(text)
    signature, public_key = crypto.sign_and_pubkey(sender, text)

    async with pool().acquire() as conn:
        seq = await conn.fetchval(
            INSERT_SQL, msg_id, room_id, sender, sender_id, ciphertext, nonce,
            signature, public_key, timestamp, NODE_ID,
        )

    duplicate = seq is None
    return {
        "id": msg_id,
        "seq": seq,
        "room_id": room_id,
        "sender": sender,
        "senderId": sender_id,
        "text": text,
        "timestamp": timestamp,
        "verified": True,
        "tampered": False,
        "duplicate": duplicate,
    }


def _decode_row(r) -> Dict[str, Any]:
    plaintext = crypto.decrypt_message(r["ciphertext"], r["nonce"])
    tampered = plaintext is None
    verified = (not tampered) and crypto.verify_signature(
        r["sender_public_key"], plaintext, r["signature"])
    return {
        "type": "message",
        "id": r["message_id"],
        "message_id": r["message_id"],
        "roomId": r["room_id"],
        "client-name": r["sender"],
        "sender": r["sender"],
        "senderId": r["sender_id"],
        "msg": plaintext if not tampered else "[TAMPERED - cannot decrypt]",
        "text": plaintext if not tampered else "[TAMPERED - cannot decrypt]",
        "timestamp": r["timestamp"],
        "verified": verified,
        "tampered": tampered,
    }


async def get_room_messages(room_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT message_id, room_id, sender, sender_id, ciphertext, nonce,
                      signature, sender_public_key, timestamp
               FROM messages WHERE room_id = $1 ORDER BY seq ASC LIMIT $2""",
            room_id, limit)
    return [_decode_row(r) for r in rows]


# ── Incremental feed cache ───────────────────────────────────────────────────

SYNC_SQL = """
SELECT seq, message_id, room_id, sender, sender_id, ciphertext, nonce,
       signature, sender_public_key, timestamp
FROM messages
WHERE seq > $1
ORDER BY seq ASC
"""


class FeedCache:
    """Append-only, per-worker cache of the shared feed.

    Holds each message pre-serialised as JSON bytes so that answering `/feed`
    is a concatenation rather than a full table scan plus N decryptions.
    """

    def __init__(self) -> None:
        self._items: List[bytes] = []
        self._seen: set[str] = set()
        self._watermark: int = 0
        self._max_seq: int = 0
        self._lock = asyncio.Lock()
        self._body = bytearray()      # b'{...},{...}'  (no enclosing brackets)
        self._cached: Optional[bytes] = None
        self._cached_gzip: Optional[bytes] = None
        self._cached_count: int = 0
        self.syncs = 0
        self.syncs_skipped = 0
        self.rows_pulled = 0
        self.evicted = 0
        self._last_sync = 0.0
        # Ceiling on cached messages so a long run cannot exhaust the 512 MB
        # container. Evicted messages stay in PostgreSQL.
        self.max_items = int(os.environ.get("FEED_MAX_ITEMS", "150000"))

    async def sync(self, force: bool = False) -> int:
        """Pull rows this worker has not yet cached. Returns how many were new."""
        now = time.monotonic()
        if not force and (now - self._last_sync) < SYNC_MIN_INTERVAL:
            self.syncs_skipped += 1
            return 0
        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            now = time.monotonic()
            if not force and (now - self._last_sync) < SYNC_MIN_INTERVAL:
                self.syncs_skipped += 1
                return 0
            self._last_sync = now
            wm = self._watermark
            async with pool().acquire() as conn:
                rows = await conn.fetch(SYNC_SQL, wm)
            self.syncs += 1
            if not rows:
                return 0
            self.rows_pulled += len(rows)

            added = 0
            for r in rows:
                mid = r["message_id"]
                if mid in self._seen:
                    continue
                self._seen.add(mid)
                blob = json.dumps(_decode_row(r), separators=(",", ":")).encode()
                self._items.append(blob)
                if self._body:
                    self._body += b","
                self._body += blob
                added += 1
                if r["seq"] > self._max_seq:
                    self._max_seq = r["seq"]

            # Rewind so rows that commit out of sequence order are still picked up.
            self._watermark = max(0, self._max_seq - SEQ_LOOKBACK)
            if added:
                self._evict_if_needed()
                self._cached = self._cached_gzip = None
            return added

    def _evict_if_needed(self) -> None:
        """Drop the oldest messages once the cache exceeds `max_items`."""
        overflow = len(self._items) - self.max_items
        if overflow <= 0:
            return
        drop = overflow + self.max_items // 10   # trim 10% extra so this is rare
        drop = min(drop, len(self._items))
        for blob in self._items[:drop]:
            try:
                self._seen.discard(json.loads(blob)["id"])
            except Exception:
                pass
        del self._items[:drop]
        self._body = bytearray(b",".join(self._items))
        self.evicted += drop

    def item_count(self) -> int:
        return len(self._items)

    async def items_since(self, index: int):
        """(new_index, items appended since `index`) — used by the WS fan-out."""
        await self.sync()
        async with self._lock:
            if index > len(self._items):
                index = len(self._items)
            return len(self._items), list(self._items[index:])

    async def response_bytes(self, node_id: str, gzipped: bool = False) -> bytes:
        """Full `/feed` payload, rebuilt only when the cache actually changed.

        `/feed` returns the entire conversation, so the response grows without
        bound. The gzip variant is built once per change and reused, so clients
        that advertise `Accept-Encoding: gzip` (which every mainstream HTTP
        client does) get a roughly 10x smaller body for no per-request CPU.
        """
        await self.sync()
        async with self._lock:
            if self._cached is None:
                count = len(self._items)
                head = ('{"status":"ok","count":%d,"node_id":"%s","messages":['
                        % (count, node_id)).encode()
                self._cached = head + bytes(self._body) + b"]}"
                self._cached_count = count
                self._cached_gzip = None
            if not gzipped:
                return self._cached
            if self._cached_gzip is None:
                self._cached_gzip = gzip.compress(self._cached, compresslevel=1)
            return self._cached_gzip

    async def response_limited(self, node_id: str, limit: int) -> bytes:
        """Most recent `limit` messages (used by `/feed?limit=N`)."""
        await self.sync()
        async with self._lock:
            items = self._items[-limit:] if limit > 0 else []
            head = ('{"status":"ok","count":%d,"node_id":"%s","messages":['
                    % (len(items), node_id)).encode()
            return head + b",".join(items) + b"]}"

    async def add_local(self, record: Dict[str, Any]) -> None:
        """Fast-path insert of a message this worker just wrote itself."""
        mid = record["id"]
        async with self._lock:
            if mid in self._seen:
                return
            self._seen.add(mid)
            blob = json.dumps({
                "type": "message",
                "id": mid,
                "message_id": mid,
                "roomId": record["room_id"],
                "client-name": record["sender"],
                "sender": record["sender"],
                "senderId": record["senderId"],
                "msg": record["text"],
                "text": record["text"],
                "timestamp": record["timestamp"],
                "verified": True,
                "tampered": False,
            }, separators=(",", ":")).encode()
            self._items.append(blob)
            if self._body:
                self._body += b","
            self._body += blob
            if record.get("seq") and record["seq"] > self._max_seq:
                self._max_seq = record["seq"]
                self._watermark = max(0, self._max_seq - SEQ_LOOKBACK)
            self._evict_if_needed()
            self._cached = self._cached_gzip = None

    def stats(self) -> Dict[str, Any]:
        return {"cached_messages": len(self._items), "max_seq": self._max_seq,
                "watermark": self._watermark, "syncs": self.syncs,
                "rows_pulled": self.rows_pulled, "evicted": self.evicted,
                "syncs_skipped": self.syncs_skipped, "max_items": self.max_items,
                "sync_min_interval_ms": round(SYNC_MIN_INTERVAL * 1000, 1),
                "seq_lookback": SEQ_LOOKBACK}


feed_cache = FeedCache()


async def count_messages() -> int:
    async with pool().acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM messages")
