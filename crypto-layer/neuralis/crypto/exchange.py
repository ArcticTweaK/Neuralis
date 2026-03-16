"""
neuralis.crypto.exchange
========================
Application-layer key exchange for Neuralis.

Provides a clean X25519 ECDH interface for establishing shared secrets between
any two nodes — independent of the transport session keys in Module 2.

Use cases
---------
- Agent-to-agent encrypted direct messaging (sealed envelopes)
- One-time key exchange for capability token encryption
- Re-keying on demand without a full TCP reconnect

Protocol
--------
1.  Alice generates an ephemeral X25519 keypair.
2.  Alice sends her ephemeral public key to Bob (over the mesh).
3.  Bob generates his own ephemeral keypair.
4.  Bob computes SharedSecret = ECDH(bob_priv, alice_pub), derives key via HKDF.
5.  Bob sends his ephemeral public key back to Alice.
6.  Alice computes SharedSecret = ECDH(alice_priv, bob_pub), derives same key.
7.  Both sides now hold an identical 32-byte shared secret.
8.  Both sides sign their ephemeral public key with their identity key to prove
    authenticity — preventing MITM.

The shared secret is NOT the raw ECDH output — it is always passed through
HKDF-SHA256 with a context string that includes both node IDs, making it
domain-separated and safe to use directly as an AES-256 key.

Usage
-----
    # On Alice's side:
    kex = KeyExchange(node_id="NRL1alice...")
    pub_bytes = kex.public_key_bytes    # send this to Bob
    signature = kex.sign_public_key(identity.sign)

    # On Bob's side:
    secret = KeyExchange.derive_shared_secret(
        local_private_key=bob_kex._private_key,
        remote_public_bytes=pub_bytes,
        local_node_id="NRL1bob...",
        remote_node_id="NRL1alice...",
    )
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_INFO_EXCHANGE = b"neuralis-key-exchange-v1"
EXCHANGE_TIMEOUT   = 30.0    # seconds before a pending exchange expires


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ExchangeError(Exception):
    """Raised when a key exchange fails or is invalid."""


# ---------------------------------------------------------------------------
# SharedSecret — result of a completed key exchange
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SharedSecret:
    """
    The result of a completed X25519 key exchange.

    Attributes
    ----------
    key_bytes    : 32-byte AES-256 key derived from the ECDH output
    local_node   : NRL1... ID of the local party
    remote_node  : NRL1... ID of the remote party
    established_at : unix timestamp of derivation
    exchange_id  : unique ID for this exchange instance (random 16 bytes hex)

    The key is derived as:
        HKDF-SHA256(
            ikm  = X25519(local_priv, remote_pub),
            salt = SHA-256(sorted_node_ids),
            info = "neuralis-key-exchange-v1"
        )
    """
    key_bytes:      bytes
    local_node:     str
    remote_node:    str
    established_at: float
    exchange_id:    str

    def __repr__(self) -> str:
        age = time.time() - self.established_at
        return (
            f"<SharedSecret {self.exchange_id[:8]}… "
            f"local={self.local_node[:12]}… remote={self.remote_node[:12]}… "
            f"age={age:.1f}s>"
        )

    def is_expired(self, ttl_seconds: float = 3600.0) -> bool:
        return (time.time() - self.established_at) > ttl_seconds

    def to_dict(self) -> dict:
        """Serialise (without key_bytes — never send the key)."""
        return {
            "exchange_id":    self.exchange_id,
            "local_node":     self.local_node,
            "remote_node":    self.remote_node,
            "established_at": self.established_at,
        }


# ---------------------------------------------------------------------------
# KeyExchange — manages one side of an ephemeral ECDH exchange
# ---------------------------------------------------------------------------

class KeyExchange:
    """
    One side of an ephemeral X25519 key exchange.

    Create one per exchange, use it once, discard.

    Parameters
    ----------
    node_id     : NRL1... ID of the local node
    exchange_id : optional; generated randomly if not provided
    """

    def __init__(self, node_id: str, exchange_id: Optional[str] = None):
        self._private_key  = X25519PrivateKey.generate()
        self._node_id      = node_id
        self._exchange_id  = exchange_id or os.urandom(16).hex()
        self._created_at   = time.time()
        self._used         = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte X25519 public key bytes — safe to send to remote."""
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def public_key_b64(self) -> str:
        """Base64-encoded public key for JSON transport."""
        return base64.b64encode(self.public_key_bytes).decode()

    @property
    def exchange_id(self) -> str:
        return self._exchange_id

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def is_expired(self) -> bool:
        return (time.time() - self._created_at) > EXCHANGE_TIMEOUT

    # ------------------------------------------------------------------
    # Sign public key with identity key (proves authenticity)
    # ------------------------------------------------------------------

    def sign_public_key(self, sign_fn) -> bytes:
        """
        Sign our X25519 public key with the Ed25519 identity key.

        sign_fn : callable (bytes) -> bytes  — e.g. node.identity.sign

        The signed material is:
            exchange_id_bytes(16) || x25519_pub_bytes(32) || timestamp_be_f64(8)

        Returns 64-byte Ed25519 signature.
        """
        ts_bytes = struct.pack(">d", self._created_at)
        material = bytes.fromhex(self._exchange_id) + self.public_key_bytes + ts_bytes
        return sign_fn(material)

    def verify_remote_public_key(
        self,
        remote_pub_bytes: bytes,
        remote_signature: bytes,
        remote_pub_hex: str,
        remote_exchange_id: str,
        remote_timestamp: float,
    ) -> None:
        """
        Verify the remote side's signature over their X25519 public key.

        Raises ExchangeError if the signature is invalid.

        Parameters
        ----------
        remote_pub_bytes    : 32-byte X25519 public key
        remote_signature    : 64-byte Ed25519 signature
        remote_pub_hex      : hex Ed25519 identity public key of remote
        remote_exchange_id  : hex exchange ID from remote
        remote_timestamp    : timestamp when remote generated their keypair
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        try:
            ed_pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(remote_pub_hex))
        except Exception as exc:
            raise ExchangeError(f"Invalid remote Ed25519 public key: {exc}") from exc

        ts_bytes = struct.pack(">d", remote_timestamp)
        material = bytes.fromhex(remote_exchange_id) + remote_pub_bytes + ts_bytes

        try:
            ed_pub.verify(remote_signature, material)
        except InvalidSignature:
            raise ExchangeError("Remote X25519 public key signature is invalid — possible MITM")
        except Exception as exc:
            raise ExchangeError(f"Signature verification error: {exc}") from exc

    # ------------------------------------------------------------------
    # Complete the exchange — derive shared secret
    # ------------------------------------------------------------------

    def complete(self, remote_pub_bytes: bytes, remote_node_id: str) -> SharedSecret:
        """
        Complete the key exchange given the remote party's X25519 public key.

        Returns a SharedSecret with a 32-byte AES-256 key.

        Raises ExchangeError if already used or expired.
        """
        if self._used:
            raise ExchangeError("KeyExchange instance already used — create a new one")
        if self.is_expired:
            raise ExchangeError("KeyExchange has expired — create a new one")
        if len(remote_pub_bytes) != 32:
            raise ExchangeError(f"Invalid X25519 public key length: {len(remote_pub_bytes)}")

        self._used = True

        try:
            remote_pub = X25519PublicKey.from_public_bytes(remote_pub_bytes)
        except Exception as exc:
            raise ExchangeError(f"Invalid remote X25519 public key: {exc}") from exc

        raw_secret = self._private_key.exchange(remote_pub)
        key_bytes  = self._derive_key(raw_secret, self._node_id, remote_node_id)

        return SharedSecret(
            key_bytes=key_bytes,
            local_node=self._node_id,
            remote_node=remote_node_id,
            established_at=time.time(),
            exchange_id=self._exchange_id,
        )

    # ------------------------------------------------------------------
    # Static: derive from raw private key (for re-derivation)
    # ------------------------------------------------------------------

    @staticmethod
    def derive_shared_secret(
        local_private_key: X25519PrivateKey,
        remote_public_bytes: bytes,
        local_node_id: str,
        remote_node_id: str,
        exchange_id: Optional[str] = None,
    ) -> SharedSecret:
        """
        Derive a SharedSecret from a raw X25519 private key and remote public bytes.

        Use this when you already have the private key object (e.g. re-deriving
        from a stored key).
        """
        if len(remote_public_bytes) != 32:
            raise ExchangeError(f"Invalid X25519 public key length: {len(remote_public_bytes)}")

        try:
            remote_pub = X25519PublicKey.from_public_bytes(remote_public_bytes)
        except Exception as exc:
            raise ExchangeError(f"Invalid remote X25519 public key: {exc}") from exc

        raw_secret = local_private_key.exchange(remote_pub)
        key_bytes  = KeyExchange._derive_key(raw_secret, local_node_id, remote_node_id)

        return SharedSecret(
            key_bytes=key_bytes,
            local_node=local_node_id,
            remote_node=remote_node_id,
            established_at=time.time(),
            exchange_id=exchange_id or os.urandom(16).hex(),
        )

    # ------------------------------------------------------------------
    # Internal HKDF derivation
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_key(raw_secret: bytes, node_a: str, node_b: str) -> bytes:
        """
        HKDF-SHA256 key derivation.
        Salt = SHA-256(sorted([node_a, node_b])) — deterministic regardless of order.
        """
        sorted_ids = sorted([node_a, node_b])
        salt = hashlib.sha256("|".join(sorted_ids).encode()).digest()

        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=HKDF_INFO_EXCHANGE,
        )
        return hkdf.derive(raw_secret)

    def __repr__(self) -> str:
        status = "used" if self._used else ("expired" if self.is_expired else "active")
        return f"<KeyExchange {self._exchange_id[:8]}… node={self._node_id[:12]}… {status}>"
