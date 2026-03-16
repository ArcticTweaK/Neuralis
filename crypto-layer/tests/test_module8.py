"""
tests/test_module8.py
=====================
Full test suite for Module 8: crypto-layer

Tests cover:
- Signing:      Signer, Verifier, SignedPayload, canonical digest, sign_dict
- Exchange:     KeyExchange, SharedSecret, HKDF derivation, MITM detection
- Envelope:     seal_envelope, open_envelope, wrong recipient, tampering, replay, expiry
- KeyStore:     start/stop, generate, save/load, rotate_x25519_static, rotate_hmac
- Tokens:       issue_token, verify_token, expiry, audience, issuer, capability wildcards

Run with:
    PYTHONPATH=/home/arctic/Documents/Neuralis/neuralis-node:/home/arctic/Documents/Neuralis/crypto-layer \
      python3 -m pytest crypto-layer/tests/test_module8.py -v
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_repo  = Path(__file__).resolve().parent.parent.parent
_node  = _repo / "neuralis-node"
_crypt = _repo / "crypto-layer"
for _p in (_crypt, _node):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

from neuralis.crypto.signing  import Signer, Verifier, SignatureError, SignedPayload, _canonical_digest
from neuralis.crypto.exchange import KeyExchange, SharedSecret, ExchangeError
from neuralis.crypto.envelope import (
    SealedEnvelope, seal_envelope, open_envelope, EnvelopeError, _derive_envelope_key
)
from neuralis.crypto.keystore import CryptoKeyStore, KeyRecord, KeyRotationError
from neuralis.crypto.tokens   import (
    CapabilityToken, issue_token, verify_token, TokenError, SignedToken, _capability_matches
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path





def _make_node_id(pub_bytes: bytes) -> str:
    import hashlib
    digest = hashlib.sha256(pub_bytes).digest()
    return "NRL1" + digest.hex()[:16]


def make_identity():
    priv = Ed25519PrivateKey.generate()
    pub  = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    node_id = _make_node_id(pub)
    return priv, pub.hex(), node_id


def make_x25519():
    priv = X25519PrivateKey.generate()
    pub  = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return priv, pub


def make_mock_node(tmp_path: Path):
    """Create a minimal mock Node for keystore tests."""
    from neuralis.identity import NodeIdentity
    from neuralis.config   import NodeConfig

    key_dir  = tmp_path / "identity"
    identity = NodeIdentity.create_new(key_dir=key_dir)

    cfg = NodeConfig.defaults()
    cfg.identity.key_dir     = str(key_dir)
    cfg.storage.ipfs_repo_path = str(tmp_path / "ipfs")
    cfg.agents.agents_dir    = str(tmp_path / "agents")
    cfg.agents.models_dir    = str(tmp_path / "models")
    cfg.logging.log_dir      = str(tmp_path / "logs")

    node = MagicMock()
    node.identity             = identity
    node.config               = cfg
    node.register_subsystem   = MagicMock()
    node.on_shutdown          = MagicMock()
    return node


# ===========================================================================
# 1. Signing — _canonical_digest
# ===========================================================================

class TestCanonicalDigest:
    def test_deterministic(self):
        payload = b"hello mesh"
        ts      = 1234567890.0
        sid     = "NRL1abc"
        d1 = _canonical_digest(payload, sid, ts)
        d2 = _canonical_digest(payload, sid, ts)
        assert d1 == d2

    def test_differs_on_payload(self):
        ts  = 1234567890.0
        sid = "NRL1abc"
        assert _canonical_digest(b"a", sid, ts) != _canonical_digest(b"b", sid, ts)

    def test_differs_on_sender_id(self):
        ts = 1234567890.0
        assert _canonical_digest(b"x", "NRL1a", ts) != _canonical_digest(b"x", "NRL1b", ts)

    def test_differs_on_timestamp(self):
        assert _canonical_digest(b"x", "NRL1a", 1.0) != _canonical_digest(b"x", "NRL1a", 2.0)

    def test_returns_32_bytes(self):
        d = _canonical_digest(b"test", "NRL1", 0.0)
        assert len(d) == 32


# ===========================================================================
# 2. Signing — Signer and Verifier
# ===========================================================================

class TestSignerVerifier:
    def _make_pair(self):
        priv, pub_hex, node_id = make_identity()
        signer   = Signer(priv, node_id)
        verifier = Verifier.from_hex(pub_hex, expected_sender_id=node_id)
        return signer, verifier, node_id

    def test_sign_verify_round_trip(self):
        signer, verifier, _ = self._make_pair()
        signed = signer.sign(b"hello mesh")
        verifier.verify(signed)   # must not raise

    def test_sign_dict_round_trip(self):
        signer, verifier, _ = self._make_pair()
        data   = {"task": "search", "query": "neuralis p2p"}
        signed = signer.sign_dict(data)
        verifier.verify(signed)

    def test_tampered_payload_fails(self):
        signer, verifier, _ = self._make_pair()
        signed = signer.sign(b"original")
        tampered = SignedPayload(
            payload=b"tampered",
            sender_id=signed.sender_id,
            sender_pk=signed.sender_pk,
            timestamp=signed.timestamp,
            signature=signed.signature,
        )
        with pytest.raises(SignatureError):
            verifier.verify(tampered)

    def test_tampered_signature_fails(self):
        signer, verifier, _ = self._make_pair()
        signed = signer.sign(b"data")
        raw_sig = base64.b64decode(signed.signature)
        bad_sig = bytearray(raw_sig)
        bad_sig[0] ^= 0xFF
        tampered = SignedPayload(
            payload=signed.payload,
            sender_id=signed.sender_id,
            sender_pk=signed.sender_pk,
            timestamp=signed.timestamp,
            signature=base64.b64encode(bytes(bad_sig)).decode(),
        )
        with pytest.raises(SignatureError):
            verifier.verify(tampered)

    def test_wrong_sender_id_fails(self):
        priv, pub_hex, node_id = make_identity()
        signer   = Signer(priv, node_id)
        verifier = Verifier.from_hex(pub_hex, expected_sender_id="NRL1wrongnode")
        signed   = signer.sign(b"data")
        with pytest.raises(SignatureError, match="Sender ID mismatch"):
            verifier.verify(signed)

    def test_wrong_public_key_fails(self):
        signer, _, _ = self._make_pair()
        priv2, pub2_hex, nid2 = make_identity()
        verifier2 = Verifier.from_hex(pub2_hex)
        signed = signer.sign(b"data")
        with pytest.raises(SignatureError, match="Public key mismatch"):
            verifier2.verify(signed)

    def test_node_id_property(self):
        priv, _, node_id = make_identity()
        signer = Signer(priv, node_id)
        assert signer.node_id == node_id

    def test_public_key_hex_matches(self):
        priv, pub_hex, node_id = make_identity()
        signer = Signer(priv, node_id)
        assert signer.public_key_hex == pub_hex

    def test_verify_bytes(self):
        signer, verifier, node_id = self._make_pair()
        payload = b"raw bytes test"
        ts      = time.time()
        signed  = signer.sign(payload, timestamp=ts)
        verifier.verify_bytes(signed.signature, payload, ts, node_id)

    def test_verify_bytes_tampered_fails(self):
        signer, verifier, node_id = self._make_pair()
        payload = b"test"
        ts      = time.time()
        signed  = signer.sign(payload, timestamp=ts)
        with pytest.raises(SignatureError):
            verifier.verify_bytes(signed.signature, b"different", ts, node_id)

    def test_signed_payload_serialisation(self):
        signer, verifier, _ = self._make_pair()
        signed = signer.sign(b"serialise me")
        d    = signed.to_dict()
        wire = signed.to_bytes()
        restored = SignedPayload.from_bytes(wire)
        verifier.verify(restored)

    def test_empty_payload(self):
        signer, verifier, _ = self._make_pair()
        signed = signer.sign(b"")
        verifier.verify(signed)

    def test_large_payload(self):
        signer, verifier, _ = self._make_pair()
        payload = os.urandom(1024 * 100)   # 100 KB
        signed  = signer.sign(payload)
        verifier.verify(signed)

    def test_verify_raw(self):
        """Verifier.verify_raw checks a direct Ed25519 signature (no digest wrapping)."""
        priv, pub_hex, node_id = make_identity()
        verifier = Verifier.from_hex(pub_hex)
        data     = b"raw material"
        raw_sig  = priv.sign(data)
        verifier.verify_raw(raw_sig, data)

    def test_verify_raw_tampered_fails(self):
        priv, pub_hex, _ = make_identity()
        verifier = Verifier.from_hex(pub_hex)
        raw_sig  = priv.sign(b"data")
        with pytest.raises(SignatureError):
            verifier.verify_raw(raw_sig, b"tampered")


# ===========================================================================
# 3. Key Exchange
# ===========================================================================

class TestKeyExchange:
    def test_both_sides_derive_same_secret(self):
        kex_alice = KeyExchange(node_id="NRL1alice")
        kex_bob   = KeyExchange(node_id="NRL1bob")

        secret_alice = kex_alice.complete(kex_bob.public_key_bytes, "NRL1bob")
        secret_bob   = kex_bob.complete(kex_alice.public_key_bytes, "NRL1alice")

        assert secret_alice.key_bytes == secret_bob.key_bytes

    def test_shared_secret_is_32_bytes(self):
        kex_a = KeyExchange(node_id="NRL1a")
        kex_b = KeyExchange(node_id="NRL1b")
        secret = kex_a.complete(kex_b.public_key_bytes, "NRL1b")
        assert len(secret.key_bytes) == 32

    def test_different_pairs_produce_different_secrets(self):
        kex_a1 = KeyExchange(node_id="NRL1a")
        kex_b1 = KeyExchange(node_id="NRL1b")
        kex_a2 = KeyExchange(node_id="NRL1a")
        kex_b2 = KeyExchange(node_id="NRL1b")

        s1 = kex_a1.complete(kex_b1.public_key_bytes, "NRL1b")
        s2 = kex_a2.complete(kex_b2.public_key_bytes, "NRL1b")
        assert s1.key_bytes != s2.key_bytes   # ephemeral keys differ each time

    def test_used_exchange_raises(self):
        kex_a = KeyExchange(node_id="NRL1a")
        kex_b = KeyExchange(node_id="NRL1b")
        kex_a.complete(kex_b.public_key_bytes, "NRL1b")
        with pytest.raises(ExchangeError, match="already used"):
            kex_a.complete(kex_b.public_key_bytes, "NRL1b")

    def test_invalid_pubkey_length_raises(self):
        kex = KeyExchange(node_id="NRL1a")
        with pytest.raises(ExchangeError):
            kex.complete(b"\x00" * 16, "NRL1b")

    def test_public_key_bytes_is_32_bytes(self):
        kex = KeyExchange(node_id="NRL1a")
        assert len(kex.public_key_bytes) == 32

    def test_public_key_b64_decodable(self):
        kex = KeyExchange(node_id="NRL1a")
        decoded = base64.b64decode(kex.public_key_b64)
        assert len(decoded) == 32

    def test_exchange_id_is_hex(self):
        kex = KeyExchange(node_id="NRL1a")
        assert all(c in "0123456789abcdef" for c in kex.exchange_id)
        assert len(kex.exchange_id) == 32

    def test_shared_secret_node_ids(self):
        kex_a = KeyExchange(node_id="NRL1alice")
        kex_b = KeyExchange(node_id="NRL1bob")
        secret = kex_a.complete(kex_b.public_key_bytes, "NRL1bob")
        assert secret.local_node  == "NRL1alice"
        assert secret.remote_node == "NRL1bob"

    def test_shared_secret_is_not_expired_immediately(self):
        kex_a = KeyExchange(node_id="NRL1a")
        kex_b = KeyExchange(node_id="NRL1b")
        secret = kex_a.complete(kex_b.public_key_bytes, "NRL1b")
        assert not secret.is_expired()
        assert secret.is_expired(ttl_seconds=0.0)

    def test_sign_and_verify_public_key(self):
        priv_ed, pub_hex, node_id = make_identity()
        kex = KeyExchange(node_id=node_id)
        sig = kex.sign_public_key(lambda data: priv_ed.sign(data))
        kex.verify_remote_public_key(
            remote_pub_bytes=kex.public_key_bytes,
            remote_signature=sig,
            remote_pub_hex=pub_hex,
            remote_exchange_id=kex.exchange_id,
            remote_timestamp=kex._created_at,
        )  # must not raise

    def test_verify_remote_key_tampered_fails(self):
        priv_ed, pub_hex, node_id = make_identity()
        kex = KeyExchange(node_id=node_id)
        sig = kex.sign_public_key(lambda data: priv_ed.sign(data))

        # Tamper with the public key bytes
        bad_pub = bytearray(kex.public_key_bytes)
        bad_pub[0] ^= 0xFF
        with pytest.raises(ExchangeError, match="invalid"):
            kex.verify_remote_public_key(
                remote_pub_bytes=bytes(bad_pub),
                remote_signature=sig,
                remote_pub_hex=pub_hex,
                remote_exchange_id=kex.exchange_id,
                remote_timestamp=kex._created_at,
            )

    def test_static_derive_shared_secret(self):
        kex_a = KeyExchange(node_id="NRL1a")
        kex_b = KeyExchange(node_id="NRL1b")
        # Use static method
        secret = KeyExchange.derive_shared_secret(
            local_private_key=kex_a._private_key,
            remote_public_bytes=kex_b.public_key_bytes,
            local_node_id="NRL1a",
            remote_node_id="NRL1b",
        )
        assert len(secret.key_bytes) == 32

    def test_repr(self):
        kex = KeyExchange(node_id="NRL1a")
        r = repr(kex)
        assert "KeyExchange" in r
        assert "active" in r


# ===========================================================================
# 4. Sealed Envelopes
# ===========================================================================

class TestSealedEnvelope:
    def _make_parties(self):
        priv_a, pub_hex_a, nid_a = make_identity()
        priv_b, pub_hex_b, nid_b = make_identity()
        x25519_a_priv, x25519_a_pub = make_x25519()
        x25519_b_priv, x25519_b_pub = make_x25519()
        return (
            priv_a, pub_hex_a, nid_a, x25519_a_priv, x25519_a_pub,
            priv_b, pub_hex_b, nid_b, x25519_b_priv, x25519_b_pub,
        )

    def test_seal_open_round_trip(self):
        (priv_a, pub_hex_a, nid_a, x25519_a_priv, x25519_a_pub,
         priv_b, pub_hex_b, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        payload = b"secret agent task payload"
        env = seal_envelope(
            payload=payload,
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
        )
        plaintext = open_envelope(env, x25519_b_priv, nid_b)
        assert plaintext == payload

    def test_wrong_recipient_raises(self):
        (priv_a, pub_hex_a, nid_a, x25519_a_priv, x25519_a_pub,
         priv_b, pub_hex_b, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        env = seal_envelope(
            payload=b"secret",
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
        )
        with pytest.raises(EnvelopeError, match="for"):
            open_envelope(env, x25519_b_priv, "NRL1wrongnode")

    def test_wrong_x25519_key_raises(self):
        (priv_a, pub_hex_a, nid_a, x25519_a_priv, x25519_a_pub,
         priv_b, pub_hex_b, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        env = seal_envelope(
            payload=b"secret",
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
        )
        wrong_priv = X25519PrivateKey.generate()
        with pytest.raises(EnvelopeError):
            open_envelope(env, wrong_priv, nid_b)

    def test_tampered_ciphertext_raises(self):
        (priv_a, pub_hex_a, nid_a, _, _,
         _, _, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        env = seal_envelope(
            payload=b"data",
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
        )
        tampered_ct = bytearray(env.ciphertext)
        tampered_ct[-1] ^= 0xFF
        from dataclasses import replace
        bad_env = SealedEnvelope(
            envelope_id=env.envelope_id, sender_id=env.sender_id,
            sender_pk=env.sender_pk, recipient_id=env.recipient_id,
            timestamp=env.timestamp, eph_pub=env.eph_pub,
            nonce=env.nonce, ciphertext=bytes(tampered_ct),
            signature=env.signature,
        )
        with pytest.raises(EnvelopeError):
            open_envelope(bad_env, x25519_b_priv, nid_b)

    def test_expired_envelope_raises(self):
        (priv_a, pub_hex_a, nid_a, _, _,
         _, _, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        env = seal_envelope(
            payload=b"old message",
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
            timestamp=time.time() - 400,   # 400 seconds old
        )
        with pytest.raises(EnvelopeError, match="too old"):
            open_envelope(env, x25519_b_priv, nid_b, max_age_seconds=300)

    def test_future_timestamp_raises(self):
        (priv_a, pub_hex_a, nid_a, _, _,
         _, _, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        env = seal_envelope(
            payload=b"future msg",
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
            timestamp=time.time() + 120,   # 2 minutes in future
        )
        with pytest.raises(EnvelopeError, match="future"):
            open_envelope(env, x25519_b_priv, nid_b)

    def test_serialisation_round_trip(self):
        (priv_a, pub_hex_a, nid_a, _, _,
         _, _, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        env = seal_envelope(
            payload=b"wire test",
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
        )
        wire  = env.to_bytes()
        env2  = SealedEnvelope.from_bytes(wire)
        plain = open_envelope(env2, x25519_b_priv, nid_b)
        assert plain == b"wire test"

    def test_invalid_recipient_x25519_key_length_raises(self):
        priv_a, pub_hex_a, nid_a = make_identity()
        _, _, nid_b = make_identity()
        with pytest.raises(EnvelopeError, match="length"):
            seal_envelope(
                payload=b"x",
                sender_id=nid_a,
                sender_sign_fn=priv_a.sign,
                sender_pk_hex=pub_hex_a,
                recipient_id=nid_b,
                recipient_x25519_pub_bytes=b"\x00" * 16,
            )

    def test_large_payload(self):
        (priv_a, pub_hex_a, nid_a, _, _,
         _, _, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        payload = os.urandom(64 * 1024)
        env  = seal_envelope(
            payload=payload,
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
        )
        assert open_envelope(env, x25519_b_priv, nid_b) == payload

    def test_repr(self):
        (priv_a, pub_hex_a, nid_a, _, _,
         _, _, nid_b, x25519_b_priv, x25519_b_pub) = self._make_parties()

        env = seal_envelope(
            payload=b"repr test",
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=x25519_b_pub,
        )
        r = repr(env)
        assert "SealedEnvelope" in r


# ===========================================================================
# 5. CryptoKeyStore
# ===========================================================================

class TestCryptoKeyStore:
    @pytest.mark.asyncio
    async def test_start_generates_keys(self, tmp_dir):
        node = make_mock_node(tmp_dir)
        ks   = CryptoKeyStore(node)
        await ks.start()
        assert ks.x25519_static_pub_bytes is not None
        assert len(ks.x25519_static_pub_bytes) == 32
        assert ks.hmac_key is not None
        assert len(ks.hmac_key) == 32
        await ks.stop()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, tmp_dir):
        node = make_mock_node(tmp_dir)
        ks   = CryptoKeyStore(node)
        await ks.start()
        pub1 = ks.x25519_static_pub_bytes
        await ks.start()   # second call should be a no-op
        assert ks.x25519_static_pub_bytes == pub1
        await ks.stop()

    @pytest.mark.asyncio
    async def test_keys_persist_across_restart(self, tmp_dir):
        node = make_mock_node(tmp_dir)

        ks1 = CryptoKeyStore(node)
        await ks1.start()
        pub1  = ks1.x25519_static_pub_hex
        hmac1 = ks1.hmac_key
        await ks1.stop()

        # Reload from same path
        ks2 = CryptoKeyStore(node)
        await ks2.start()
        assert ks2.x25519_static_pub_hex == pub1
        assert ks2.hmac_key == hmac1
        await ks2.stop()

    @pytest.mark.asyncio
    async def test_rotate_x25519_static(self, tmp_dir):
        node = make_mock_node(tmp_dir)
        ks   = CryptoKeyStore(node)
        await ks.start()

        pub_before = ks.x25519_static_pub_hex
        record     = await ks.rotate_x25519_static()
        pub_after  = ks.x25519_static_pub_hex

        assert pub_before != pub_after
        assert record.key_type == "x25519_static"
        assert not record.is_retired
        await ks.stop()

    @pytest.mark.asyncio
    async def test_retired_key_still_available(self, tmp_dir):
        node = make_mock_node(tmp_dir)
        ks   = CryptoKeyStore(node)
        await ks.start()

        # Get old key ID
        old_record = next(
            r for r in ks._records.values()
            if r.key_type == "x25519_static" and not r.is_retired
        )
        old_id = old_record.key_id

        await ks.rotate_x25519_static()

        # Old key should now be retired but retrievable
        retired_priv = ks.get_retired_priv(old_id)
        assert retired_priv is not None

        await ks.stop()

    @pytest.mark.asyncio
    async def test_rotate_hmac(self, tmp_dir):
        node = make_mock_node(tmp_dir)
        ks   = CryptoKeyStore(node)
        await ks.start()

        hmac_before = ks.hmac_key
        await ks.rotate_hmac_key()
        hmac_after  = ks.hmac_key

        assert hmac_before != hmac_after
        await ks.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_keys(self, tmp_dir):
        node = make_mock_node(tmp_dir)
        ks   = CryptoKeyStore(node)
        await ks.start()
        await ks.stop()

        assert ks._x25519_static_priv is None
        assert ks._hmac_key is None

    @pytest.mark.asyncio
    async def test_status(self, tmp_dir):
        node = make_mock_node(tmp_dir)
        ks   = CryptoKeyStore(node)
        await ks.start()

        status = ks.status()
        assert status["running"] is True
        assert status["x25519_static_pub"] is not None
        assert status["active_keys"] > 0
        await ks.stop()

    @pytest.mark.asyncio
    async def test_can_open_envelope_with_stored_key(self, tmp_dir):
        """End-to-end: seal with keystore pub, open with keystore priv."""
        node_a = make_mock_node(tmp_dir / "a")
        node_b = make_mock_node(tmp_dir / "b")

        ks_a = CryptoKeyStore(node_a)
        ks_b = CryptoKeyStore(node_b)
        await ks_a.start()
        await ks_b.start()

        priv_a, pub_hex_a, nid_a = make_identity()
        nid_b = node_b.identity.node_id

        payload = b"agent task over mesh"
        env = seal_envelope(
            payload=payload,
            sender_id=nid_a,
            sender_sign_fn=priv_a.sign,
            sender_pk_hex=pub_hex_a,
            recipient_id=nid_b,
            recipient_x25519_pub_bytes=ks_b.x25519_static_pub_bytes,
        )
        plaintext = open_envelope(env, ks_b.x25519_static_priv, nid_b)
        assert plaintext == payload

        await ks_a.stop()
        await ks_b.stop()


# ===========================================================================
# 6. Capability Tokens
# ===========================================================================

class TestCapabilityTokens:
    def _make_token(self, capability="agent:invoke:search", ttl=300, **kwargs):
        hmac_key = os.urandom(32)
        signed   = issue_token(
            issuer_id="NRL1issuer",
            subject_id="NRL1subject",
            audience_id="NRL1audience",
            capability=capability,
            hmac_key=hmac_key,
            ttl_seconds=ttl,
            **kwargs,
        )
        return signed, hmac_key

    def test_issue_and_verify(self):
        signed, hmac_key = self._make_token()
        token = verify_token(signed.wire, hmac_key=hmac_key)
        assert token.capability == "agent:invoke:search"

    def test_verify_with_expected_audience(self):
        signed, hmac_key = self._make_token()
        token = verify_token(
            signed.wire, hmac_key=hmac_key,
            expected_audience="NRL1audience",
        )
        assert token is not None

    def test_wrong_audience_fails(self):
        signed, hmac_key = self._make_token()
        with pytest.raises(TokenError, match="audience"):
            verify_token(signed.wire, hmac_key=hmac_key, expected_audience="NRL1wrong")

    def test_wrong_issuer_fails(self):
        signed, hmac_key = self._make_token()
        with pytest.raises(TokenError, match="issuer"):
            verify_token(signed.wire, hmac_key=hmac_key, expected_issuer="NRL1wrong")

    def test_tampered_signature_fails(self):
        signed, hmac_key = self._make_token()
        parts = signed.wire.split(".")
        parts[2] = parts[2][:-4] + "AAAA"
        bad_wire = ".".join(parts)
        with pytest.raises(TokenError, match="invalid"):
            verify_token(bad_wire, hmac_key=hmac_key)

    def test_wrong_hmac_key_fails(self):
        signed, _       = self._make_token()
        wrong_key       = os.urandom(32)
        with pytest.raises(TokenError):
            verify_token(signed.wire, hmac_key=wrong_key)

    def test_expired_token_fails(self):
        hmac_key = os.urandom(32)
        token = CapabilityToken(
            token_id=os.urandom(16).hex(),
            issuer_id="NRL1a",
            subject_id="NRL1b",
            audience_id="NRL1b",
            issued_at=time.time() - 600,
            expires_at=time.time() - 300,   # expired 5 min ago
            capability="agent:invoke:*",
        )
        wire = token.to_wire(hmac_key)
        with pytest.raises(TokenError, match="expired"):
            verify_token(wire, hmac_key=hmac_key)

    def test_required_capability_exact(self):
        signed, hmac_key = self._make_token(capability="agent:invoke:search")
        verify_token(signed.wire, hmac_key=hmac_key, required_capability="agent:invoke:search")

    def test_required_capability_wildcard_fails(self):
        signed, hmac_key = self._make_token(capability="agent:invoke:search")
        with pytest.raises(TokenError, match="capability"):
            verify_token(signed.wire, hmac_key=hmac_key, required_capability="agent:invoke:summarize")

    def test_wildcard_token_satisfies_specific(self):
        signed, hmac_key = self._make_token(capability="agent:invoke:*")
        verify_token(signed.wire, hmac_key=hmac_key, required_capability="agent:invoke:search")
        verify_token(signed.wire, hmac_key=hmac_key, required_capability="agent:invoke:summarize")

    def test_star_token_satisfies_everything(self):
        signed, hmac_key = self._make_token(capability="*")
        verify_token(signed.wire, hmac_key=hmac_key, required_capability="agent:invoke:search")
        verify_token(signed.wire, hmac_key=hmac_key, required_capability="content:read:bafkreiabc")

    def test_token_ttl_remaining(self):
        signed, _ = self._make_token(ttl=300)
        assert 298 < signed.token.ttl_remaining <= 300

    def test_token_is_not_expired(self):
        signed, _ = self._make_token(ttl=300)
        assert not signed.token.is_expired

    def test_token_with_scope(self):
        hmac_key = os.urandom(32)
        signed   = issue_token(
            issuer_id="NRL1a", subject_id="NRL1b", audience_id="NRL1b",
            capability="content:read:bafkreiabc",
            hmac_key=hmac_key,
            scope={"cid": "bafkreiabc", "max_reads": 10},
        )
        token = verify_token(signed.wire, hmac_key=hmac_key)
        assert token.scope["cid"] == "bafkreiabc"
        assert token.scope["max_reads"] == 10

    def test_token_wire_has_three_parts(self):
        signed, _ = self._make_token()
        assert len(signed.wire.split(".")) == 3

    def test_token_from_wire_round_trip(self):
        signed, hmac_key = self._make_token()
        token = CapabilityToken.from_wire(signed.wire)
        assert token.capability == "agent:invoke:search"
        assert token.issuer_id == "NRL1issuer"

    def test_malformed_wire_raises(self):
        with pytest.raises(TokenError):
            CapabilityToken.from_wire("notavalidtoken")

    def test_capability_matches_helper(self):
        assert _capability_matches("agent:invoke:search", "agent:invoke:search")
        assert _capability_matches("agent:invoke:*", "agent:invoke:search")
        assert _capability_matches("*", "anything:at:all")
        assert not _capability_matches("agent:invoke:search", "agent:invoke:summarize")
        assert not _capability_matches("agent:invoke:search", "agent:invoke:*")

    def test_repr(self):
        signed, _ = self._make_token()
        r = repr(signed.token)
        assert "CapabilityToken" in r
        assert "agent:invoke:search" in r


# ===========================================================================
# 7. Integration — full crypto pipeline
# ===========================================================================

class TestIntegration:
    @pytest.mark.asyncio
    async def test_keystore_seal_open_with_token(self, tmp_dir):
        """
        Full pipeline:
        1. Node A issues a capability token to Node B
        2. Node B seals a task envelope for Node A's agent
        3. Node A opens the envelope and verifies the token inside
        """
        node_a = make_mock_node(tmp_dir / "node_a")
        node_b = make_mock_node(tmp_dir / "node_b")
        ks_a   = CryptoKeyStore(node_a)
        ks_b   = CryptoKeyStore(node_b)
        await ks_a.start()
        await ks_b.start()

        nid_a = node_a.identity.node_id
        nid_b = node_b.identity.node_id

        # 1. Node A issues a token to Node B
        signed_token = issue_token(
            issuer_id=nid_a,
            subject_id=nid_b,
            audience_id=nid_a,
            capability="agent:invoke:search",
            hmac_key=ks_a.hmac_key,
            ttl_seconds=60,
        )

        # 2. Node B seals a task for Node A, embedding the token
        task_payload = json.dumps({
            "task": "search",
            "query": "neuralis p2p mesh",
            "token": signed_token.wire,
        }).encode()

        env = seal_envelope(
            payload=task_payload,
            sender_id=nid_b,
            sender_sign_fn=node_b.identity.sign,
            sender_pk_hex=node_b.identity.public_key_hex(),
            recipient_id=nid_a,
            recipient_x25519_pub_bytes=ks_a.x25519_static_pub_bytes,
        )

        # 3. Node A opens the envelope
        plaintext = open_envelope(env, ks_a.x25519_static_priv, nid_a)
        task      = json.loads(plaintext)
        assert task["query"] == "neuralis p2p mesh"

        # 4. Node A verifies the token embedded in the task
        token = verify_token(
            task["token"],
            hmac_key=ks_a.hmac_key,
            expected_audience=nid_a,
            expected_issuer=nid_a,
            required_capability="agent:invoke:search",
        )
        assert token.subject_id == nid_b

        await ks_a.stop()
        await ks_b.stop()

    def test_signer_verifier_cross_nodes(self):
        """Two nodes: A signs, B verifies using A's peer card."""
        priv_a, pub_hex_a, nid_a = make_identity()
        signer_a = Signer(priv_a, nid_a)
        peer_card = {"public_key": pub_hex_a, "node_id": nid_a}
        verifier  = Verifier.from_peer_card(peer_card)

        payload = json.dumps({"type": "AGENT_MSG", "data": "hello"}).encode()
        signed  = signer_a.sign(payload)
        verifier.verify(signed)   # must not raise

    def test_key_exchange_then_seal(self):
        """
        Use KeyExchange to establish a shared key, then use it to verify
        that both parties computed the same encryption material.
        """
        priv_a, pub_hex_a, nid_a = make_identity()
        _, _, nid_b = make_identity()
        kex_a = KeyExchange(node_id=nid_a)
        kex_b = KeyExchange(node_id=nid_b)

        secret_a = kex_a.complete(kex_b.public_key_bytes, nid_b)
        secret_b = kex_b.complete(kex_a.public_key_bytes, nid_a)

        assert secret_a.key_bytes == secret_b.key_bytes
        assert len(secret_a.key_bytes) == 32
