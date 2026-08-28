"""
database.py — SQLite persistence layer for Group Chat
=====================================================
Stores persistent rooms and chat message history.
Each backend instance uses its own DB file (chat_PORT.db) to avoid
SQLite write contention when multiple backends share the same directory.
"""

import os
import sqlite3
import random
import string
import time
from pathlib import Path
from typing import List, Dict, Optional
try:
    import crypto_utils as crypto
except ImportError:
    from server import crypto_utils as crypto

# Use a per-port DB file so 3 backend instances don't fight over one SQLite file.
# The uvicorn port is passed in via BACKEND_PORT env var (set in start.bat / package.json).
_port = os.environ.get("BACKEND_PORT", "")
_db_filename = f"chat_{_port}.db" if _port else "chat.db"
DB_PATH = Path(__file__).parent / _db_filename


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize database tables and ensure default General Room exists."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Rooms table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT UNIQUE,
                is_private INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        
        # Messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                ciphertext TEXT NOT NULL,
                nonce TEXT NOT NULL,
                signature TEXT NOT NULL,
                sender_public_key TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
            )
        """)
        
        # Ensure default Global Room ('global') exists
        cursor.execute("SELECT id FROM rooms WHERE id = 'global'")
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO rooms (id, name, code, is_private, created_at)
                VALUES ('global', 'General Room', NULL, 0, ?)
            """, (int(time.time() * 1000),))
        
        conn.commit()


def _generate_room_code(length: int = 6) -> str:
    """Generate a unique 6-character room code (e.g. X8K9P2)."""
    chars = string.ascii_uppercase + string.digits
    # Exclude ambiguous characters O, 0, I, 1
    clean_chars = ''.join(c for c in chars if c not in 'O0I1')
    return ''.join(random.choices(clean_chars, k=length))


def create_room(name: str, is_private: bool = True) -> Dict:
    """Create a new room in DB. Private rooms get a unique 6-character room code."""
    room_id = f"room_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    code = _generate_room_code() if is_private else None
    now = int(time.time() * 1000)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO rooms (id, name, code, is_private, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (room_id, name.strip(), code, 1 if is_private else 0, now))
        conn.commit()

    return {
        "id": room_id,
        "name": name.strip(),
        "code": code,
        "is_private": is_private,
        "created_at": now
    }


def get_room_by_id(room_id: str) -> Optional[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "code": row["code"],
                "is_private": bool(row["is_private"]),
                "created_at": row["created_at"]
            }
    return None


def get_room_by_code(code: str) -> Optional[Dict]:
    clean_code = code.strip().upper()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rooms WHERE UPPER(code) = ?", (clean_code,))
        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "code": row["code"],
                "is_private": bool(row["is_private"]),
                "created_at": row["created_at"]
            }
    return None


def get_all_rooms() -> List[Dict]:
    """Get all public rooms and persistent rooms."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rooms ORDER BY created_at ASC")
        rows = cursor.fetchall()
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


def save_message(room_id: str, sender: str, sender_id: str, text: str, timestamp: int) -> Dict:
    """Encrypts + signs the message, then stores it. Never writes plaintext to disk."""
    ciphertext, nonce = crypto.encrypt_message(text)
    signature = crypto.sign_message(sender, text)
    public_key = crypto.get_public_key_b64(sender)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO messages (room_id, sender, sender_id, ciphertext, nonce, signature, sender_public_key, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (room_id, sender, sender_id, ciphertext, nonce, signature, public_key, timestamp))
        conn.commit()
        msg_id = cursor.lastrowid

    return {
        "id": msg_id,
        "room_id": room_id,
        "sender": sender,
        "senderId": sender_id,
        "text": text,          # plaintext only used for the live broadcast
        "timestamp": timestamp,
        "verified": True,
        "tampered": False,
    }


def get_room_messages(room_id: str, limit: int = 100) -> List[Dict]:
    """Retrieve history: decrypt ciphertext + verify signature for every row."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, room_id, sender, sender_id, ciphertext, nonce, signature, sender_public_key, timestamp
            FROM messages
            WHERE room_id = ?
            ORDER BY id ASC
            LIMIT ?
        """, (room_id, limit))
        rows = cursor.fetchall()

    result = []
    for r in rows:
        plaintext = crypto.decrypt_message(r["ciphertext"], r["nonce"])
        tampered = plaintext is None
        verified = crypto.verify_signature(r["sender_public_key"], plaintext, r["signature"]) if not tampered else False

        result.append({
            "type": "message",
            "id": r["id"],
            "roomId": r["room_id"],
            "sender": r["sender"],
            "senderId": r["sender_id"],
            "text": plaintext if not tampered else "⚠️ [TAMPERED — cannot decrypt]",
            "timestamp": r["timestamp"],
            "verified": verified,
            "tampered": tampered,
        })
    return result