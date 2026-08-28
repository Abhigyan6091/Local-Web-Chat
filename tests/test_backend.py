import pytest
from server import database as db
from server import crypto_utils as crypto

def test_database_initialization():
    db.init_db()
    rooms = db.get_all_rooms()
    assert len(rooms) >= 1
    room_ids = [r["id"] for r in rooms]
    assert "global" in room_ids

def test_crypto_encryption_and_decryption():
    plaintext = "Hello from distributed messaging load balancer!"
    ciphertext, nonce = crypto.encrypt_message(plaintext)
    assert ciphertext != plaintext
    
    decrypted = crypto.decrypt_message(ciphertext, nonce)
    assert decrypted == plaintext

def test_crypto_signing_and_verification():
    username = "Alice"
    message = "Test signature"
    sig = crypto.sign_message(username, message)
    pub_key = crypto.get_public_key_b64(username)
    
    assert crypto.verify_signature(pub_key, message, sig) is True
    assert crypto.verify_signature(pub_key, "Tampered text", sig) is False

def test_save_and_retrieve_message():
    db.init_db()
    msg = db.save_message(room_id="global", sender="Tester", sender_id="1234", text="Integration test message", timestamp=1000)
    assert msg["id"] is not None
    assert msg["verified"] is True
    
    history = db.get_room_messages("global")
    found = any(m["text"] == "Integration test message" for m in history)
    assert found is True
