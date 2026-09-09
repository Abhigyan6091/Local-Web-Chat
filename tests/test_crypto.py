"""
Tests for the security layer.

The important distributed property here is that the keys are *derived* from one
cluster secret rather than generated per process: Sys2, Sys3 and Sys4 must all
produce and verify the same keys, otherwise a message written by one node is
unreadable on the others.
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


def load_crypto(secret):
    """Import a fresh copy of crypto_utils as if on a node holding `secret`."""
    os.environ["CHAT_SECRET"] = secret
    import crypto_utils
    return importlib.reload(crypto_utils)


@pytest.fixture
def crypto():
    return load_crypto("unit-test-cluster-secret")


def test_encrypt_decrypt_roundtrip(crypto):
    ciphertext, nonce = crypto.encrypt_message("hello group chat")
    assert "hello" not in ciphertext          # plaintext never hits the wire
    assert crypto.decrypt_message(ciphertext, nonce) == "hello group chat"


def test_nonce_is_unique_per_message(crypto):
    a = crypto.encrypt_message("same text")
    b = crypto.encrypt_message("same text")
    assert a[1] != b[1] and a[0] != b[0]


def test_tampered_ciphertext_fails_to_decrypt(crypto):
    ciphertext, nonce = crypto.encrypt_message("original message")
    body = list(ciphertext)
    body[0] = "A" if body[0] != "A" else "B"
    assert crypto.decrypt_message("".join(body), nonce) is None


def test_signature_verifies_and_detects_edits(crypto):
    sig, pub = crypto.sign_and_pubkey("alice", "meet at 5")
    assert crypto.verify_signature(pub, "meet at 5", sig) is True
    assert crypto.verify_signature(pub, "meet at 6", sig) is False


def test_keys_are_identical_across_nodes_sharing_a_secret():
    """A signature made on one node must verify on another."""
    node_a = load_crypto("shared-cluster-secret")
    sig, pub = node_a.sign_and_pubkey("bob", "written on Sys2")
    ciphertext, nonce = node_a.encrypt_message("written on Sys2")

    node_b = load_crypto("shared-cluster-secret")
    assert node_b.get_public_key_b64("bob") == pub
    assert node_b.decrypt_message(ciphertext, nonce) == "written on Sys2"
    assert node_b.verify_signature(pub, "written on Sys2", sig) is True


def test_a_different_secret_cannot_read_the_data():
    node_a = load_crypto("cluster-secret-one")
    ciphertext, nonce = node_a.encrypt_message("confidential")
    # Read the value out before reloading: importlib.reload mutates the module
    # in place, so `node_a` and `outsider` are the same object afterwards.
    pub_a = node_a.get_public_key_b64("bob")

    outsider = load_crypto("cluster-secret-two")
    assert outsider.decrypt_message(ciphertext, nonce) is None
    assert outsider.get_public_key_b64("bob") != pub_a
