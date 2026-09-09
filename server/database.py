"""
database.py — Shared persistence layer for Group Chat with deduplication
========================================================================
Shared database used by all backend instances (Sys2, Sys3, Sys4).
Connects to PostgreSQL on Sys1 over internal lab network / SSH tunnel.
Falls back to SQLite for local development and unit tests when PostgreSQL is offline.

Every message carries a unique message_id (UUID). Retried / duplicate inserts
with the same message_id are deduplicated with ON CONFLICT DO NOTHING.
"""

import os
import uuid
import random
import string
import time
import sqlite3
import logging
from typing import List, Dict, Optional, Any

try:
    import crypto_utils as crypto
except ImportError:
    from server import crypto_utils as crypto

logger = logging.getLogger("Database")

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "chatdb")
DB_USER = os.environ.get("DB_USER", "chatuser")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "REDACTED")
USE_SQLITE_FLAG = os.environ.get("USE_SQLITE", "0") == "1"

_pg_pool = None
_use_sqlite = USE_SQLITE_FLAG
_sqlite_path = os.path.join(os.path.dirname(__file__), "chat_local.db")


def _get_pg_pool():
    global _pg_pool, _use_sqlite
    if _use_sqlite:
        return None
    if _pg_pool is None:
        try:
            import psycopg2
            import psycopg2.extras
            import psycopg2.pool
            _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=20,
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                connect_timeout=3,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            logger.info(f"Connected to PostgreSQL database at {DB_HOST}:{DB_PORT}/{DB_NAME}")
        except Exception as e:
            logger.warning(f"PostgreSQL connection failed ({e}). Falling back to local persistent SQLite.")
            _use_sqlite = True
            _pg_pool = None
    return _pg_pool


class _DBConnectionContext:
    def __init__(self):
        self.is_sqlite = False
        self.conn = None

    def __enter__(self):
        pool = _get_pg_pool()
        if pool is not None:
            self.is_sqlite = False
            self.conn = pool.getconn()
            return self
        else:
            self.is_sqlite = True
            self.conn = sqlite3.connect(_sqlite_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.is_sqlite:
            if exc_type is not None:
                self.conn.rollback()
            else:
                self.conn.commit()
            self.conn.close()
        else:
            pool = _get_pg_pool()
            if self.conn and pool:
                if exc_type is not None:
                    self.conn.rollback()
                pool.putconn(self.conn)
        return False

    def execute(self, query: str, params: tuple = ()) -> Any:
        cursor = self.conn.cursor()
        if self.is_sqlite:
            sqlite_query = query.replace("%s", "?")
            cursor.execute(sqlite_query, params)
        else:
            cursor.execute(query, params)
        return cursor

    def commit(self):
        self.conn.commit()


def get_db():
    return _DBConnectionContext()


def init_db() -> None:
    """Initialize database tables and ensure default General Room exists."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT UNIQUE,
                is_private INTEGER NOT NULL DEFAULT 0,
                created_at BIGINT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                ciphertext TEXT NOT NULL,
                nonce TEXT NOT NULL,
                signature TEXT NOT NULL,
                sender_public_key TEXT NOT NULL,
                timestamp BIGINT NOT NULL
            )
        """)

        cur = db.execute("SELECT id FROM rooms WHERE id = %s", ("global",))
        row = cur.fetchone()
        if not row:
            db.execute("""
                INSERT INTO rooms (id, name, code, is_private, created_at)
                VALUES ('global', 'General Room', NULL, 0, %s)
            """, (int(time.time() * 1000),))

        db.commit()


def _generate_room_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    clean_chars = ''.join(c for c in chars if c not in 'O0I1')
    return ''.join(random.choices(clean_chars, k=length))


def create_room(name: str, is_private: bool = True) -> Dict[str, Any]:
    room_id = f"room_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    code = _generate_room_code() if is_private else None
    now = int(time.time() * 1000)

    with get_db() as db:
        db.execute("""
            INSERT INTO rooms (id, name, code, is_private, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (room_id, name.strip(), code, 1 if is_private else 0, now))
        db.commit()

    return {
        "id": room_id,
        "name": name.strip(),
        "code": code,
        "is_private": is_private,
        "created_at": now
    }


def get_room_by_id(room_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as db:
        cur = db.execute("SELECT * FROM rooms WHERE id = %s", (room_id,))
        row = cur.fetchone()
        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "code": row["code"],
                "is_private": bool(row["is_private"]),
                "created_at": row["created_at"]
            }
    return None


def get_room_by_code(code: str) -> Optional[Dict[str, Any]]:
    clean_code = code.strip().upper()
    with get_db() as db:
        cur = db.execute("SELECT * FROM rooms WHERE UPPER(code) = %s", (clean_code,))
        row = cur.fetchone()
        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "code": row["code"],
                "is_private": bool(row["is_private"]),
                "created_at": row["created_at"]
            }
    return None


def get_all_rooms() -> List[Dict[str, Any]]:
    with get_db() as db:
        cur = db.execute("SELECT * FROM rooms ORDER BY created_at ASC")
        rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "code": r["code"],
                "is_private": bool(r["is_private"]),
                "created_at": r["created_at"]
            }
            for r in rows
        ]


def save_message(
    room_id: str,
    sender: str,
    sender_id: str,
    text: str,
    timestamp: int,
    message_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Encrypts + signs message and persists to shared DB.
    Deduplicates on message_id (UUID): If duplicate message_id is sent,
    the insert is skipped and existing message is returned.
    """
    msg_id = str(message_id) if message_id else str(uuid.uuid4())

    ciphertext, nonce = crypto.encrypt_message(text)
    signature = crypto.sign_message(sender, text)
    public_key = crypto.get_public_key_b64(sender)

    with get_db() as db:
        # Check if already exists for deduplication
        cur = db.execute("SELECT message_id, room_id, sender, sender_id, timestamp FROM messages WHERE message_id = %s", (msg_id,))
        existing = cur.fetchone()
        if existing:
            return {
                "id": str(existing["message_id"]),
                "room_id": existing["room_id"],
                "sender": existing["sender"],
                "senderId": existing["sender_id"],
                "text": text,
                "timestamp": existing["timestamp"],
                "verified": True,
                "tampered": False,
                "duplicate": True,
            }

        db.execute("""
            INSERT INTO messages
                (message_id, room_id, sender, sender_id, ciphertext, nonce, signature, sender_public_key, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (msg_id, room_id, sender, sender_id, ciphertext, nonce, signature, public_key, timestamp))
        db.commit()

    return {
        "id": msg_id,
        "room_id": room_id,
        "sender": sender,
        "senderId": sender_id,
        "text": text,
        "timestamp": timestamp,
        "verified": True,
        "tampered": False,
        "duplicate": False,
    }


def get_room_messages(room_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    with get_db() as db:
        cur = db.execute("""
            SELECT message_id, room_id, sender, sender_id, ciphertext, nonce, signature, sender_public_key, timestamp
            FROM messages
            WHERE room_id = %s
            ORDER BY timestamp ASC
            LIMIT %s
        """, (room_id, limit))
        rows = cur.fetchall()

    result = []
    for r in rows:
        plaintext = crypto.decrypt_message(r["ciphertext"], r["nonce"])
        tampered = plaintext is None
        verified = crypto.verify_signature(r["sender_public_key"], plaintext, r["signature"]) if not tampered else False

        result.append({
            "type": "message",
            "id": str(r["message_id"]),
            "roomId": r["room_id"],
            "sender": r["sender"],
            "senderId": r["sender_id"],
            "text": plaintext if not tampered else "⚠️ [TAMPERED — cannot decrypt]",
            "timestamp": r["timestamp"],
            "verified": verified,
            "tampered": tampered,
        })
    return result


def get_all_messages(limit: int = 500) -> List[Dict[str, Any]]:
    """Used by /feed route — returns all messages ordered chronologically."""
    with get_db() as db:
        cur = db.execute("""
            SELECT message_id, room_id, sender, sender_id, ciphertext, nonce, signature, sender_public_key, timestamp
            FROM messages
            ORDER BY timestamp ASC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()

    result = []
    for r in rows:
        plaintext = crypto.decrypt_message(r["ciphertext"], r["nonce"])
        tampered = plaintext is None

        result.append({
            "id": str(r["message_id"]),
            "roomId": r["room_id"],
            "client-name": r["sender"],
            "msg": plaintext if not tampered else "⚠️ [TAMPERED — cannot decrypt]",
            "timestamp": r["timestamp"],
            "tampered": tampered,
        })
    return result