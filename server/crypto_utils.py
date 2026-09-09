"""
crypto_utils.py — Encryption, integrity and signing helpers
=============================================================
- AES-GCM  -> confidentiality + tamper detection
- Ed25519  -> per-sender signatures (authenticity)
"""

import os
import base64
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

KEY_FILE = Path(__file__).parent / "secret.key"


def _load_or_create_aes_key() -> bytes:
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = AESGCM.generate_key(bit_length=256)
    KEY_FILE.write_bytes(key)
    return key


_AES_KEY = _load_or_create_aes_key()
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


# ---- per-sender Ed25519 signing keys (kept in memory, keyed by username) ----
_user_keys = {}


def get_or_create_keypair(username: str):
    if username not in _user_keys:
        priv = Ed25519PrivateKey.generate()
        _user_keys[username] = (priv, priv.public_key())
    return _user_keys[username]


def sign_message(username: str, message: str) -> str:
    priv, _ = get_or_create_keypair(username)
    return base64.b64encode(priv.sign(message.encode())).decode()


def get_public_key_b64(username: str) -> str:
    _, pub = get_or_create_keypair(username)
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def verify_signature(public_key_b64: str, message: str, signature_b64: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(base64.b64decode(signature_b64), message.encode())
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False