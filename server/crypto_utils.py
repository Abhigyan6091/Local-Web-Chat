"""
crypto_utils.py — Encryption, integrity and signing helpers
===========================================================
- AES-GCM  -> confidentiality + tamper detection for messages at rest
- Ed25519  -> per-sender signatures (authenticity)

Distributed note
----------------
Every backend (Sys2/Sys3/Sys4) shares one cluster secret, supplied via the
CHAT_SECRET environment variable (or server/secret.key as a fallback). Both the
AES-GCM data key and each user's Ed25519 signing key are *derived* from that
secret with HKDF, so any node can decrypt and verify a message written by any
other node. Without this, a message encrypted on Sys2 would be unreadable on
Sys3 and the shared feed would break.
"""

import os
import base64
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.exceptions import InvalidSignature

KEY_FILE = Path(__file__).parent / "secret.key"


def _load_cluster_secret() -> bytes:
    """Cluster-wide root secret. Identical on every backend node."""
    env_secret = os.environ.get("CHAT_SECRET", "").strip()
    if env_secret:
        return env_secret.encode()
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    # Only reached in standalone local development.
    secret = AESGCM.generate_key(bit_length=256)
    KEY_FILE.write_bytes(secret)
    return secret


_ROOT_SECRET = _load_cluster_secret()


def _derive(info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=b"local-web-chat/v3",
        info=info,
    ).derive(_ROOT_SECRET)


_AES_KEY = _derive(b"aes-gcm-message-key")
_aesgcm = AESGCM(_AES_KEY)


def encrypt_message(plaintext: str):
    """Returns (ciphertext_b64, nonce_b64)."""
    nonce = os.urandom(12)
    ciphertext = _aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(ciphertext).decode(), base64.b64encode(nonce).decode()


def decrypt_message(ciphertext_b64: str, nonce_b64: str):
    """Returns plaintext, or None if the row was tampered with."""
    try:
        ciphertext = base64.b64decode(ciphertext_b64)
        nonce = base64.b64decode(nonce_b64)
        return _aesgcm.decrypt(nonce, ciphertext, None).decode()
    except Exception:
        return None


# ---- per-sender Ed25519 signing keys, derived deterministically -------------
# Derived (not randomly generated) so that the key for a given username is the
# same on Sys2, Sys3 and Sys4. A signature produced on one node therefore
# verifies on every other node.
_user_keys = {}
_user_keys_lock = threading.Lock()


def get_or_create_keypair(username: str):
    with _user_keys_lock:
        entry = _user_keys.get(username)
        if entry is None:
            seed = _derive(b"ed25519-signing-key/" + username.encode(), 32)
            priv = Ed25519PrivateKey.from_private_bytes(seed)
            pub = priv.public_key()
            pub_b64 = base64.b64encode(
                pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            ).decode()
            entry = (priv, pub, pub_b64)
            _user_keys[username] = entry
        return entry[0], entry[1]


def sign_message(username: str, message: str) -> str:
    priv, _ = get_or_create_keypair(username)
    return base64.b64encode(priv.sign(message.encode())).decode()


def get_public_key_b64(username: str) -> str:
    get_or_create_keypair(username)
    return _user_keys[username][2]


def sign_and_pubkey(username: str, message: str):
    """Single-lookup helper used on the hot path: returns (signature_b64, pubkey_b64)."""
    priv, _ = get_or_create_keypair(username)
    return base64.b64encode(priv.sign(message.encode())).decode(), _user_keys[username][2]


def verify_signature(public_key_b64: str, message: str, signature_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(base64.b64decode(signature_b64), message.encode())
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False
