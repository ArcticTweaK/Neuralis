"""
neuralis.crypto.envelope
========================
Sealed envelopes for encrypted, authenticated agent-to-agent messaging.

A SealedEnvelope encrypts a payload so that ONLY the intended recipient
can decrypt it, and is signed by the sender so the recipient can verify
authenticity.  This is the application-layer equivalent of NaCl's box primitive.

Cryptographic construction
--------------------------
Sealing (sender side):
    1.  Generate ephemeral X25519 keypair.
    2.  ECDH(eph_priv, recipient_x25519_pub) → shared_secret
    3.  Derive enc_key via HKDF-SHA256(shared_secret, sender_id || recipient_id)
    4.  Encrypt payload with AES-256-GCM(enc_key, random_nonce)
    5.  Sign the ciphertext + header with sender's Ed25519 key.
    6.  Serialize everything into SealedEnvelope.

Opening (recipient side):
    1.  ECDH(recipient_x25519_priv, eph_pub_from_envelope) → shared_secret
    2.  Re-derive enc_key (same HKDF params → same key)
    3.  Verify sender's Ed25519 signature over ciphertext + header.
    4.  Decrypt AES-256-GCM → plaintext.

Properties
----------
- Confidentiality: only recipient can decrypt (asymmetric encryption)
- Authenticity:    only stated sender could have signed (Ed25519)
- Integrity:       AES-GCM tag + Ed25519 sig — any tampering is detected
- Forward secrecy: ephemeral X25519 key discarded after sealing; past
                   envelopes cannot be decrypted even if node keys are stolen
- Replay resistance: envelope_id (random 16 bytes) + timestamp field

Usage
-----
    # Sender
    env = seal_envelope(
        payload        = b"agent task payload",
        sender_id      = node.identity.node_id,
        sender_sign_fn = node.identity.sign,
        recipient_id   = peer_node_id,
        recipient_x25519_pub_bytes = peer_x25519_pub,
    )
    wire_bytes = env.to_bytes()

    # Recipient
    env = SealedEnvelope.from_bytes(wire_bytes)
    plaintext = open_envelope(
        envelope           = env,
        recipient_x25519_priv = my_x25519_priv,
        recipient_id       = my_node_id,
    )
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_INFO_ENVELOPE = b"neuralis-sealed-envelope-v1"
NONCE_SIZE         = 12   # AES-GCM
GCM_TAG_SIZE       = 16


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EnvelopeError(Exception):
    """Raised when sealing, opening, or deserialising an envelope fails."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_envelope_key(
    shared_secret: bytes,
    sender_id: str,
    recipient_id: str,
) -> bytes:
    """HKDF-SHA256 envelope key derivation with domain separation."""
    # Salt encodes the two parties — prevents key reuse across different pairs
    salt = hashlib.sha256(f"{sender_id}:{recipient_id}".encode()).digest()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO_ENVELOPE,
    )
    return hkdf.derive(shared_secret)


def _sign_envelope_header(
    sign_fn: Callable[[bytes], bytes],
    envelope_id: str,
    sender_id: str,
    recipient_id: str,
    timestamp: float,
    eph_pub_bytes: bytes,
    ciphertext: bytes,
) -> bytes:
    """
    Produce Ed25519 signature over all envelope fields except the signature itself.

    Signed material:
        envelope_id(16) || sender_id_hash(32) || recipient_id_hash(32)
        || timestamp_be_f64(8) || eph_pub(32) || ciphertext_hash(32)
    """
    ts_bytes        = struct.pack(">d", timestamp)
    sender_hash     = hashlib.sha256(sender_id.encode()).digest()
    recipient_hash  = hashlib.sha256(recipient_id.encode()).digest()
    ct_hash         = hashlib.sha256(ciphertext).digest()
    eid_bytes       = bytes.fromhex(envelope_id)

    material = eid_bytes + sender_hash + recipient_hash + ts_bytes + eph_pub_bytes + ct_hash
    return sign_fn(material)


# ---------------------------------------------------------------------------
# SealedEnvelope — wire format
# ---------------------------------------------------------------------------

@dataclass
class SealedEnvelope:
    """
    An encrypted, signed message from one Neuralis node to another.

    Wire format (JSON, base64-encoded binary fields):
    {
        "v":            1,
        "envelope_id":  "<hex 32 chars>",
        "sender_id":    "NRL1...",
        "sender_pk":    "<hex Ed25519 pub>",
        "recipient_id": "NRL1...",
        "timestamp":    1234567890.123,
        "eph_pub":      "<b64 X25519 eph pub>",
        "nonce":        "<b64 12 bytes>",
        "ciphertext":   "<b64 AES-GCM output>",
        "signature":    "<b64 Ed25519 sig>"
    }
    """
    envelope_id:   str       # hex 16 bytes
    sender_id:     str
    sender_pk:     str       # hex Ed25519
    recipient_id:  str
    timestamp:     float
    eph_pub:       bytes     # 32-byte X25519 ephemeral public key
    nonce:         bytes     # 12-byte AES-GCM nonce
    ciphertext:    bytes     # AES-GCM ciphertext (includes tag)
    signature:     bytes     # 64-byte Ed25519 signature
    version:       int = 1

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "v":            self.version,
            "envelope_id":  self.envelope_id,
            "sender_id":    self.sender_id,
            "sender_pk":    self.sender_pk,
            "recipient_id": self.recipient_id,
            "timestamp":    self.timestamp,
            "eph_pub":      base64.b64encode(self.eph_pub).decode(),
            "nonce":        base64.b64encode(self.nonce).decode(),
            "ciphertext":   base64.b64encode(self.ciphertext).decode(),
            "signature":    base64.b64encode(self.signature).decode(),
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), separators=(",", ":")).encode()

    @classmethod
    def from_dict(cls, d: dict) -> "SealedEnvelope":
        try:
            return cls(
                version=int(d["v"]),
                envelope_id=str(d["envelope_id"]),
                sender_id=str(d["sender_id"]),
                sender_pk=str(d["sender_pk"]),
                recipient_id=str(d["recipient_id"]),
                timestamp=float(d["timestamp"]),
                eph_pub=base64.b64decode(d["eph_pub"]),
                nonce=base64.b64decode(d["nonce"]),
                ciphertext=base64.b64decode(d["ciphertext"]),
                signature=base64.b64decode(d["signature"]),
            )
        except (KeyError, ValueError) as exc:
            raise EnvelopeError(f"Malformed SealedEnvelope: {exc}") from exc

    @classmethod
    def from_bytes(cls, data: bytes) -> "SealedEnvelope":
        try:
            return cls.from_dict(json.loads(data))
        except json.JSONDecodeError as exc:
            raise EnvelopeError(f"SealedEnvelope JSON decode error: {exc}") from exc

    def __repr__(self) -> str:
        return (
            f"<SealedEnvelope {self.envelope_id[:8]}… "
            f"from={self.sender_id[:12]}… to={self.recipient_id[:12]}…>"
        )


# ---------------------------------------------------------------------------
# seal_envelope — encrypt and sign
# ---------------------------------------------------------------------------

def seal_envelope(
    payload: bytes,
    sender_id: str,
    sender_sign_fn: Callable[[bytes], bytes],
    sender_pk_hex: str,
    recipient_id: str,
    recipient_x25519_pub_bytes: bytes,
    envelope_id: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> SealedEnvelope:
    """
    Seal a payload for a specific recipient.

    Parameters
    ----------
    payload                    : bytes to encrypt
    sender_id                  : NRL1... node ID of the sender
    sender_sign_fn             : callable (bytes) -> bytes — Ed25519 sign
    sender_pk_hex              : hex Ed25519 public key of sender
    recipient_id               : NRL1... node ID of recipient
    recipient_x25519_pub_bytes : 32-byte X25519 public key of recipient
    envelope_id                : optional; random 16 bytes hex if not given
    timestamp                  : optional; defaults to now

    Returns
    -------
    SealedEnvelope ready for serialisation and transmission.
    """
    if len(recipient_x25519_pub_bytes) != 32:
        raise EnvelopeError(
            f"Invalid recipient X25519 public key length: {len(recipient_x25519_pub_bytes)}"
        )

    eid = envelope_id or os.urandom(16).hex()
    ts  = timestamp if timestamp is not None else time.time()

    # 1. Ephemeral X25519 keypair
    eph_priv = X25519PrivateKey.generate()
    eph_pub_bytes = eph_priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    # 2. ECDH → shared secret
    try:
        recipient_x25519_pub = X25519PublicKey.from_public_bytes(recipient_x25519_pub_bytes)
    except Exception as exc:
        raise EnvelopeError(f"Invalid recipient X25519 key: {exc}") from exc

    shared_secret = eph_priv.exchange(recipient_x25519_pub)

    # 3. Derive encryption key
    enc_key = _derive_envelope_key(shared_secret, sender_id, recipient_id)

    # 4. Encrypt with AES-256-GCM
    nonce = os.urandom(NONCE_SIZE)
    aesgcm = AESGCM(enc_key)
    # Additional data = envelope_id + sender_id + recipient_id (integrity-binds the header)
    aad = f"{eid}:{sender_id}:{recipient_id}".encode()
    ciphertext = aesgcm.encrypt(nonce, payload, aad)

    # 5. Sign
    signature = _sign_envelope_header(
        sign_fn=sender_sign_fn,
        envelope_id=eid,
        sender_id=sender_id,
        recipient_id=recipient_id,
        timestamp=ts,
        eph_pub_bytes=eph_pub_bytes,
        ciphertext=ciphertext,
    )

    return SealedEnvelope(
        envelope_id=eid,
        sender_id=sender_id,
        sender_pk=sender_pk_hex,
        recipient_id=recipient_id,
        timestamp=ts,
        eph_pub=eph_pub_bytes,
        nonce=nonce,
        ciphertext=ciphertext,
        signature=signature,
    )


# ---------------------------------------------------------------------------
# open_envelope — verify and decrypt
# ---------------------------------------------------------------------------

def open_envelope(
    envelope: SealedEnvelope,
    recipient_x25519_priv: X25519PrivateKey,
    recipient_id: str,
    max_age_seconds: float = 300.0,
) -> bytes:
    """
    Open (verify + decrypt) a SealedEnvelope.

    Parameters
    ----------
    envelope               : SealedEnvelope to open
    recipient_x25519_priv  : recipient's X25519 private key
    recipient_id           : expected recipient NRL1... node ID
    max_age_seconds        : reject envelopes older than this (replay protection)

    Returns
    -------
    Plaintext bytes.

    Raises
    ------
    EnvelopeError on any failure (wrong recipient, bad signature, decryption fail, too old).
    """
    # 1. Recipient check
    if envelope.recipient_id != recipient_id:
        raise EnvelopeError(
            f"Envelope is for {envelope.recipient_id}, not {recipient_id}"
        )

    # 2. Timestamp / replay check
    age = time.time() - envelope.timestamp
    if age > max_age_seconds:
        raise EnvelopeError(
            f"Envelope is too old: {age:.1f}s > {max_age_seconds}s max"
        )
    if age < -60.0:
        raise EnvelopeError(f"Envelope timestamp is in the future: {-age:.1f}s")

    # 3. Verify sender's Ed25519 signature
    try:
        sender_ed_pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(envelope.sender_pk))
    except Exception as exc:
        raise EnvelopeError(f"Invalid sender Ed25519 public key: {exc}") from exc

    expected_sig_material = _sign_envelope_header.__wrapped__ if hasattr(_sign_envelope_header, '__wrapped__') else None
    # Re-derive the signed material
    ts_bytes       = struct.pack(">d", envelope.timestamp)
    sender_hash    = hashlib.sha256(envelope.sender_id.encode()).digest()
    recipient_hash = hashlib.sha256(envelope.recipient_id.encode()).digest()
    ct_hash        = hashlib.sha256(envelope.ciphertext).digest()
    eid_bytes      = bytes.fromhex(envelope.envelope_id)
    material       = eid_bytes + sender_hash + recipient_hash + ts_bytes + envelope.eph_pub + ct_hash

    try:
        sender_ed_pub.verify(envelope.signature, material)
    except InvalidSignature:
        raise EnvelopeError("Envelope signature verification failed — possible tampering or wrong sender key")
    except Exception as exc:
        raise EnvelopeError(f"Signature verification error: {exc}") from exc

    # 4. ECDH with ephemeral key
    try:
        eph_pub = X25519PublicKey.from_public_bytes(envelope.eph_pub)
    except Exception as exc:
        raise EnvelopeError(f"Invalid ephemeral public key: {exc}") from exc

    shared_secret = recipient_x25519_priv.exchange(eph_pub)

    # 5. Derive same enc_key
    enc_key = _derive_envelope_key(shared_secret, envelope.sender_id, envelope.recipient_id)

    # 6. Decrypt AES-256-GCM
    aesgcm = AESGCM(enc_key)
    aad = f"{envelope.envelope_id}:{envelope.sender_id}:{envelope.recipient_id}".encode()
    try:
        plaintext = aesgcm.decrypt(envelope.nonce, envelope.ciphertext, aad)
    except Exception as exc:
        raise EnvelopeError(f"AES-GCM decryption failed: {exc}") from exc

    return plaintext
