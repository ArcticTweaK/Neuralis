"""
neuralis.crypto.signing
=======================
Clean Ed25519 signing and verification API for the Neuralis application layer.

This module wraps the raw cryptography primitives used in Module 1 (identity)
and Module 2 (transport) into a consistent, testable interface that the agent
runtime, protocol layer, and any future modules can use directly.

Design
------
- Signer  : holds a private key; signs arbitrary payloads; attaches metadata.
- Verifier: holds only a public key; verifies signatures; stateless.
- SignedPayload: wire-portable container (JSON-serialisable).

All signatures are over the *canonical bytes* of the payload:
    canonical = sha256( version || sender_id || timestamp_bytes || payload_bytes )

The double-hash (SHA-256 then Ed25519) is intentional — it bounds the data
fed to the signature primitive to a fixed 32-byte digest regardless of payload
size, which is the standard practice for Ed25519 in protocols.

Usage
-----
    signer   = Signer.from_node(node)
    signed   = signer.sign(b"hello mesh")
    verifier = Verifier.from_peer_card(peer_card)
    verifier.verify(signed)          # raises SignatureError on failure
    verifier.verify_bytes(sig, b"hello mesh", ts, sender_id)
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
from dataclasses import dataclass
from typing import Optional, Union

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

SIGNING_VERSION = 1
SIGNING_VERSION_BYTES = struct.pack(">B", SIGNING_VERSION)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SignatureError(Exception):
    """Raised when a signature is missing, malformed, or invalid."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonical_digest(
    payload: bytes,
    sender_id: str,
    timestamp: float,
) -> bytes:
    """
    Produce a 32-byte canonical digest that is signed/verified.

    canonical = SHA-256( version(1) || timestamp_be_f64(8) || sender_id_utf8 || payload )
    """
    ts_bytes = struct.pack(">d", timestamp)
    h = hashlib.sha256()
    h.update(SIGNING_VERSION_BYTES)
    h.update(ts_bytes)
    h.update(sender_id.encode("utf-8"))
    h.update(payload)
    return h.digest()


def _pubkey_to_hex(pub: Ed25519PublicKey) -> str:
    return pub.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ).hex()


def _pubkey_from_hex(hex_str: str) -> Ed25519PublicKey:
    try:
        raw = bytes.fromhex(hex_str)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise SignatureError(f"Invalid public key hex: {exc}") from exc


# ---------------------------------------------------------------------------
# SignedPayload — wire-portable signed container
# ---------------------------------------------------------------------------

@dataclass
class SignedPayload:
    """
    A signed, portable payload container.

    Fields
    ------
    payload   : raw bytes being signed
    sender_id : NRL1... node ID of the signer
    sender_pk : hex Ed25519 public key of the signer
    timestamp : unix float when signed
    signature : base64 Ed25519 signature over canonical digest
    version   : signing protocol version

    Wire format (JSON):
        {
            "v": 1,
            "sender_id": "NRL1...",
            "sender_pk": "aabb...",
            "timestamp": 1234567890.123,
            "payload":   "<base64>",
            "signature": "<base64>"
        }
    """
    payload:   bytes
    sender_id: str
    sender_pk: str      # hex
    timestamp: float
    signature: str      # base64
    version:   int = SIGNING_VERSION

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "v":         self.version,
            "sender_id": self.sender_id,
            "sender_pk": self.sender_pk,
            "timestamp": self.timestamp,
            "payload":   base64.b64encode(self.payload).decode(),
            "signature": self.signature,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), separators=(",", ":")).encode()

    @classmethod
    def from_dict(cls, d: dict) -> "SignedPayload":
        try:
            return cls(
                version=int(d["v"]),
                sender_id=str(d["sender_id"]),
                sender_pk=str(d["sender_pk"]),
                timestamp=float(d["timestamp"]),
                payload=base64.b64decode(d["payload"]),
                signature=str(d["signature"]),
            )
        except (KeyError, ValueError) as exc:
            raise SignatureError(f"Malformed SignedPayload: {exc}") from exc

    @classmethod
    def from_bytes(cls, data: bytes) -> "SignedPayload":
        try:
            return cls.from_dict(json.loads(data))
        except json.JSONDecodeError as exc:
            raise SignatureError(f"SignedPayload JSON decode error: {exc}") from exc


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------

class Signer:
    """
    Signs payloads using an Ed25519 private key.

    Typical construction:
        signer = Signer.from_node(node)
        signer = Signer(private_key, node_id)
    """

    def __init__(self, private_key: Ed25519PrivateKey, node_id: str):
        self._private_key = private_key
        self._node_id     = node_id
        self._pub_hex     = _pubkey_to_hex(private_key.public_key())

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_node(cls, node) -> "Signer":
        """
        Construct from a running neuralis.node.Node.

        The node's Ed25519 private key is accessed via node.identity.
        """
        identity = node.identity
        # NodeIdentity stores the raw private key — access it directly
        private_key = identity._private_key
        return cls(private_key, identity.node_id)

    @classmethod
    def from_private_key_bytes(cls, raw_bytes: bytes, node_id: str) -> "Signer":
        """Construct from 32-byte raw Ed25519 private key bytes."""
        pk = Ed25519PrivateKey.from_private_bytes(raw_bytes)
        return cls(pk, node_id)

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def sign(self, payload: bytes, timestamp: Optional[float] = None) -> SignedPayload:
        """
        Sign raw bytes. Returns a SignedPayload ready for transmission.

        Parameters
        ----------
        payload   : arbitrary bytes to sign
        timestamp : unix float; defaults to now
        """
        ts = timestamp if timestamp is not None else time.time()
        digest = _canonical_digest(payload, self._node_id, ts)
        raw_sig = self._private_key.sign(digest)
        return SignedPayload(
            payload=payload,
            sender_id=self._node_id,
            sender_pk=self._pub_hex,
            timestamp=ts,
            signature=base64.b64encode(raw_sig).decode(),
        )

    def sign_dict(self, data: dict, timestamp: Optional[float] = None) -> SignedPayload:
        """Sign a JSON-serialisable dict (canonical JSON bytes)."""
        payload_bytes = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return self.sign(payload_bytes, timestamp)

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def public_key_hex(self) -> str:
        return self._pub_hex


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

class Verifier:
    """
    Verifies Ed25519 signatures against a known public key.

    Stateless — holds only the public key and the expected sender_id.
    """

    def __init__(self, public_key: Ed25519PublicKey, expected_sender_id: Optional[str] = None):
        self._public_key         = public_key
        self._expected_sender_id = expected_sender_id
        self._pub_hex            = _pubkey_to_hex(public_key)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_peer_card(cls, peer_card: dict) -> "Verifier":
        """
        Construct from a peer card dict (as returned by NodeIdentity.to_peer_card()).

        Expects keys: "public_key" (hex), "node_id".
        """
        pub = _pubkey_from_hex(peer_card["public_key"])
        return cls(pub, expected_sender_id=peer_card.get("node_id"))

    @classmethod
    def from_hex(cls, hex_pub: str, expected_sender_id: Optional[str] = None) -> "Verifier":
        """Construct from a hex-encoded public key."""
        pub = _pubkey_from_hex(hex_pub)
        return cls(pub, expected_sender_id)

    @classmethod
    def from_node(cls, node) -> "Verifier":
        """Construct a verifier for the local node (self-verification)."""
        pub = node.identity._private_key.public_key()
        return cls(pub, expected_sender_id=node.identity.node_id)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, signed: SignedPayload) -> None:
        """
        Verify a SignedPayload. Raises SignatureError on any failure.

        Checks:
        1. Version is known
        2. sender_pk matches this verifier's public key
        3. sender_id matches expected (if set)
        4. Signature is valid over canonical digest
        """
        if signed.version != SIGNING_VERSION:
            raise SignatureError(f"Unknown signing version: {signed.version}")

        if signed.sender_pk != self._pub_hex:
            raise SignatureError(
                f"Public key mismatch: expected {self._pub_hex[:16]}…, "
                f"got {signed.sender_pk[:16]}…"
            )

        if self._expected_sender_id and signed.sender_id != self._expected_sender_id:
            raise SignatureError(
                f"Sender ID mismatch: expected {self._expected_sender_id}, "
                f"got {signed.sender_id}"
            )

        digest = _canonical_digest(signed.payload, signed.sender_id, signed.timestamp)
        try:
            raw_sig = base64.b64decode(signed.signature)
            self._public_key.verify(raw_sig, digest)
        except InvalidSignature:
            raise SignatureError("Ed25519 signature verification failed")
        except Exception as exc:
            raise SignatureError(f"Signature verification error: {exc}") from exc

    def verify_bytes(
        self,
        signature_b64: str,
        payload: bytes,
        timestamp: float,
        sender_id: str,
    ) -> None:
        """
        Verify a raw signature without a SignedPayload wrapper.
        Useful when verifying signatures embedded in other message formats.
        """
        digest = _canonical_digest(payload, sender_id, timestamp)
        try:
            raw_sig = base64.b64decode(signature_b64)
            self._public_key.verify(raw_sig, digest)
        except InvalidSignature:
            raise SignatureError("Ed25519 signature verification failed")
        except Exception as exc:
            raise SignatureError(f"Signature verification error: {exc}") from exc

    def verify_raw(self, signature_bytes: bytes, data: bytes) -> None:
        """
        Verify a raw Ed25519 signature directly over data (no digest wrapping).
        Used for verifying legacy/external signatures (e.g. peer cards from Module 2).
        """
        try:
            self._public_key.verify(signature_bytes, data)
        except InvalidSignature:
            raise SignatureError("Raw Ed25519 signature verification failed")
        except Exception as exc:
            raise SignatureError(f"Raw signature error: {exc}") from exc

    @property
    def public_key_hex(self) -> str:
        return self._pub_hex
