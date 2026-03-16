"""
neuralis.crypto.keystore
========================
Persistent cryptographic key storage and rotation for Neuralis.

Manages the node's long-term and ephemeral cryptographic keys beyond the
identity keypair (Module 1).  Specifically:

Keys managed
------------
1. x25519_static   — Static X25519 key used for sealed envelope decryption.
                     Nodes advertise their X25519 public key in peer cards so
                     others can seal envelopes for them.
2. x25519_sessions — Short-lived X25519 keys for individual key exchanges.
                     Keyed by peer node_id; rotated on demand.
3. hmac_keys       — HMAC-SHA256 keys for capability token signing.
                     Rotated on a configurable schedule.

Storage format
--------------
All keys are stored in ~/.neuralis/crypto/keys.json.
The file is encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256)
with a key derived from the node's Ed25519 private key bytes.
This means the crypto keystore is protected by the same root of trust as
the node identity — no separate passphrase required.

Key rotation
------------
x25519_static keys can be rotated at any time.  When rotated, the old key
is kept in a "retired" list for a TTL period (default 24h) so in-flight
sealed envelopes can still be opened.

Usage
-----
    ks = CryptoKeyStore(node)
    await ks.start()

    # Get this node's X25519 public key to advertise to peers
    pub_bytes = ks.x25519_static_pub_bytes

    # Decrypt a sealed envelope using the static X25519 private key
    from neuralis.crypto.envelope import open_envelope
    plaintext = open_envelope(env, ks.x25519_static_priv, node.identity.node_id)

    # Rotate the static X25519 key
    await ks.rotate_x25519_static()
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

CRYPTO_DIR = "crypto"
KEYS_FILE = "keys.json"
KEYS_VERSION = 1
HKDF_INFO_STORE = b"neuralis-keystore-protection-v1"
RETIRED_KEY_TTL = 24 * 3600.0  # seconds to keep retired keys


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KeyRotationError(Exception):
    """Raised when key rotation fails."""


# ---------------------------------------------------------------------------
# KeyRecord — metadata for a stored key
# ---------------------------------------------------------------------------


@dataclass
class KeyRecord:
    """
    Metadata entry for a stored key.

    Attributes
    ----------
    key_id      : random 16-byte hex identifier
    key_type    : "x25519_static" | "x25519_session" | "hmac"
    created_at  : unix timestamp
    rotated_at  : unix timestamp of last rotation, or None
    retired_at  : unix timestamp when this key was retired, or None
    public_hex  : hex public key bytes (for asymmetric keys), or None
    """

    key_id: str
    key_type: str
    created_at: float
    rotated_at: Optional[float] = None
    retired_at: Optional[float] = None
    public_hex: Optional[str] = None

    @property
    def is_retired(self) -> bool:
        return self.retired_at is not None

    @property
    def is_expired_retired(self) -> bool:
        if not self.retired_at:
            return False
        return (time.time() - self.retired_at) > RETIRED_KEY_TTL

    def to_dict(self) -> dict:
        return {
            "key_id": self.key_id,
            "key_type": self.key_type,
            "created_at": self.created_at,
            "rotated_at": self.rotated_at,
            "retired_at": self.retired_at,
            "public_hex": self.public_hex,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KeyRecord":
        return cls(
            key_id=d["key_id"],
            key_type=d["key_type"],
            created_at=float(d["created_at"]),
            rotated_at=d.get("rotated_at"),
            retired_at=d.get("retired_at"),
            public_hex=d.get("public_hex"),
        )


# ---------------------------------------------------------------------------
# CryptoKeyStore
# ---------------------------------------------------------------------------


class CryptoKeyStore:
    """
    Manages the node's cryptographic keys beyond the identity keypair.

    Parameters
    ----------
    node : neuralis.node.Node — provides identity (for storage key derivation)
           and config (for storage path).
    """

    def __init__(self, node):
        self._node = node
        self._identity = node.identity
        self._config = node.config

        crypto_path = Path(node.config.identity.key_dir).parent / CRYPTO_DIR
        self._store_path = crypto_path / KEYS_FILE
        self._fernet = self._make_fernet()

        # In-memory key objects (raw cryptographic keys)
        self._x25519_static_priv: Optional[X25519PrivateKey] = None
        self._retired_x25519_privs: List[tuple] = (
            []
        )  # [(key_id, X25519PrivateKey, retired_at)]
        self._hmac_key: Optional[bytes] = None

        # Key records (metadata only — stored to disk)
        self._records: Dict[str, KeyRecord] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load or generate all managed keys. Register with node."""
        if self._running:
            return

        self._store_path.parent.mkdir(parents=True, exist_ok=True)

        if self._store_path.exists():
            self._load()
        else:
            self._generate_all()
            self._save()

        self._running = True
        self._node.register_subsystem("crypto", self)
        self._node.on_shutdown(self.stop)

        logger.info(
            "CryptoKeyStore started | x25519_static=%s | hmac_key=%s",
            self.x25519_static_pub_hex[:16] + "…",
            "present",
        )

    async def stop(self) -> None:
        """Flush to disk and clear in-memory keys."""
        if not self._running:
            return
        self._save()
        self._x25519_static_priv = None
        self._hmac_key = None
        self._retired_x25519_privs.clear()
        self._running = False
        logger.info("CryptoKeyStore stopped")

    # ------------------------------------------------------------------
    # X25519 static key — advertised to peers for sealed envelopes
    # ------------------------------------------------------------------

    @property
    def x25519_static_priv(self) -> X25519PrivateKey:
        if not self._x25519_static_priv:
            raise KeyRotationError("X25519 static key not loaded")
        return self._x25519_static_priv

    @property
    def x25519_static_pub_bytes(self) -> bytes:
        return self.x25519_static_priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def x25519_static_pub_hex(self) -> str:
        return self.x25519_static_pub_bytes.hex()

    @property
    def x25519_static_pub_b64(self) -> str:
        return base64.b64encode(self.x25519_static_pub_bytes).decode()

    async def rotate_x25519_static(self) -> KeyRecord:
        """
        Rotate the static X25519 key.

        The old key is moved to the retired list (kept for RETIRED_KEY_TTL seconds
        so in-flight sealed envelopes can still be opened).

        Returns the new KeyRecord.
        """
        if not self._running:
            raise KeyRotationError("CryptoKeyStore not started")

        # Retire current key
        old_record = next(
            (
                r
                for r in self._records.values()
                if r.key_type == "x25519_static" and not r.is_retired
            ),
            None,
        )
        if old_record and self._x25519_static_priv:
            old_record.retired_at = time.time()
            self._retired_x25519_privs.append(
                (old_record.key_id, self._x25519_static_priv, old_record.retired_at)
            )
            logger.info("Retired X25519 static key %s", old_record.key_id[:8])

        # Generate new key
        new_priv = X25519PrivateKey.generate()
        new_pub = new_priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        key_id = os.urandom(16).hex()
        record = KeyRecord(
            key_id=key_id,
            key_type="x25519_static",
            created_at=time.time(),
            public_hex=new_pub.hex(),
        )
        self._records[key_id] = record
        self._x25519_static_priv = new_priv

        # Prune fully expired retired keys
        self._prune_retired()
        self._save()

        logger.info("Rotated X25519 static key → %s", key_id[:8])
        return record

    def get_retired_priv(self, key_id: str) -> Optional[X25519PrivateKey]:
        """Return a retired X25519 private key by ID (for opening old envelopes)."""
        for kid, priv, _ in self._retired_x25519_privs:
            if kid == key_id:
                return priv
        return None

    # ------------------------------------------------------------------
    # HMAC key — used for capability token signing
    # ------------------------------------------------------------------

    @property
    def hmac_key(self) -> bytes:
        if not self._hmac_key:
            raise KeyRotationError("HMAC key not loaded")
        return self._hmac_key

    async def rotate_hmac_key(self) -> None:
        """Rotate the HMAC signing key. Invalidates all previously issued tokens."""
        self._hmac_key = os.urandom(32)
        old = next(
            (
                r
                for r in self._records.values()
                if r.key_type == "hmac" and not r.is_retired
            ),
            None,
        )
        if old:
            old.retired_at = time.time()

        key_id = os.urandom(16).hex()
        self._records[key_id] = KeyRecord(
            key_id=key_id,
            key_type="hmac",
            created_at=time.time(),
        )
        self._save()
        logger.info("HMAC key rotated — all existing tokens are now invalid")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        active = [r.to_dict() for r in self._records.values() if not r.is_retired]
        retired = [r.to_dict() for r in self._records.values() if r.is_retired]
        return {
            "running": self._running,
            "x25519_static_pub": self.x25519_static_pub_hex if self._running else None,
            "active_keys": len(active),
            "retired_keys": len(retired),
            "records": active,
        }

    # ------------------------------------------------------------------
    # Internal — Fernet protection
    # ------------------------------------------------------------------

    def _make_fernet(self) -> Fernet:
        """
        Derive a Fernet key from the node's Ed25519 private key bytes.

        This binds the crypto keystore to the node identity — the store
        cannot be read without the identity private key.
        """
        raw_priv = self._identity._private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"neuralis-keystore-fernet-salt-v1",
            info=HKDF_INFO_STORE,
        )
        fernet_key = base64.urlsafe_b64encode(hkdf.derive(raw_priv))
        return Fernet(fernet_key)

    # ------------------------------------------------------------------
    # Internal — generation
    # ------------------------------------------------------------------

    def _generate_all(self) -> None:
        """Generate all keys from scratch (first boot)."""
        # X25519 static
        priv = X25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        key_id = os.urandom(16).hex()
        self._records[key_id] = KeyRecord(
            key_id=key_id,
            key_type="x25519_static",
            created_at=time.time(),
            public_hex=pub.hex(),
        )
        self._x25519_static_priv = priv

        # HMAC key
        hmac_id = os.urandom(16).hex()
        self._hmac_key = os.urandom(32)
        self._records[hmac_id] = KeyRecord(
            key_id=hmac_id,
            key_type="hmac",
            created_at=time.time(),
        )

        logger.info("CryptoKeyStore: generated fresh key material")

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Encrypt and write keys to disk."""
        payload = {
            "version": KEYS_VERSION,
            "records": {k: v.to_dict() for k, v in self._records.items()},
            "keys": {},
        }

        # Serialise private key bytes (never send public — we re-derive)
        if self._x25519_static_priv:
            raw = self._x25519_static_priv.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            # Find active x25519_static record
            active_id = next(
                (
                    k
                    for k, r in self._records.items()
                    if r.key_type == "x25519_static" and not r.is_retired
                ),
                None,
            )
            if active_id:
                payload["keys"][active_id] = base64.b64encode(raw).decode()

        # Retired X25519 keys
        for kid, priv, _ in self._retired_x25519_privs:
            raw = priv.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            payload["keys"][kid] = base64.b64encode(raw).decode()

        # HMAC key
        if self._hmac_key:
            hmac_id = next(
                (
                    k
                    for k, r in self._records.items()
                    if r.key_type == "hmac" and not r.is_retired
                ),
                None,
            )
            if hmac_id:
                payload["keys"][hmac_id] = base64.b64encode(self._hmac_key).decode()

        raw_json = json.dumps(payload, separators=(",", ":")).encode()
        encrypted = self._fernet.encrypt(raw_json)
        self._store_path.write_bytes(encrypted)

    def _load(self) -> None:
        """Decrypt and load keys from disk."""
        try:
            encrypted = self._store_path.read_bytes()
            raw_json = self._fernet.decrypt(encrypted)
            data = json.loads(raw_json)
        except Exception as exc:
            raise KeyRotationError(f"Failed to load crypto keystore: {exc}") from exc

        self._records = {
            k: KeyRecord.from_dict(v) for k, v in data.get("records", {}).items()
        }

        raw_keys = data.get("keys", {})

        # Load X25519 static (active)
        active_x25519 = next(
            (
                r
                for r in self._records.values()
                if r.key_type == "x25519_static" and not r.is_retired
            ),
            None,
        )
        if active_x25519 and active_x25519.key_id in raw_keys:
            raw = base64.b64decode(raw_keys[active_x25519.key_id])
            self._x25519_static_priv = X25519PrivateKey.from_private_bytes(raw)

        # Load retired X25519 keys
        retired_records = [
            r
            for r in self._records.values()
            if r.key_type == "x25519_static"
            and r.is_retired
            and not r.is_expired_retired
        ]
        for rec in retired_records:
            if rec.key_id in raw_keys:
                raw = base64.b64decode(raw_keys[rec.key_id])
                priv = X25519PrivateKey.from_private_bytes(raw)
                self._retired_x25519_privs.append((rec.key_id, priv, rec.retired_at))

        # Load HMAC key
        active_hmac = next(
            (
                r
                for r in self._records.values()
                if r.key_type == "hmac" and not r.is_retired
            ),
            None,
        )
        if active_hmac and active_hmac.key_id in raw_keys:
            self._hmac_key = base64.b64decode(raw_keys[active_hmac.key_id])

        if not self._x25519_static_priv or not self._hmac_key:
            logger.warning("CryptoKeyStore: missing keys on load — regenerating")
            self._generate_all()
            self._save()

    def _prune_retired(self) -> None:
        """Remove retired keys that have passed their TTL."""
        now = time.time()
        self._retired_x25519_privs = [
            (kid, priv, retired_at)
            for kid, priv, retired_at in self._retired_x25519_privs
            if (now - retired_at) <= RETIRED_KEY_TTL
        ]
        # Also mark expired retired records
        for rec in self._records.values():
            if rec.is_expired_retired:
                logger.debug("Pruned expired retired key %s", rec.key_id[:8])
