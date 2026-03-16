"""
neuralis.identity
=================
Node identity management for Neuralis.

Every Neuralis node has a single, persistent, self-sovereign identity rooted
in an Ed25519 keypair.  The public key IS the node's identity — there is no
central authority, no registration, no certificate chain.

Key artefacts
-------------
- Private key  : Ed25519PrivateKey  (never leaves the machine)
- Public key   : Ed25519PublicKey   (shared freely; used for peer verification)
- Node ID      : base58-encoded SHA-256(public_key_bytes)  (human-readable addr)
- Peer ID      : libp2p-compatible multihash (for DHT / mDNS interop)

All key material is stored encrypted-at-rest using a Fernet key derived from a
local machine secret (see KeyStore).  If no machine secret is supplied the
store falls back to a randomly generated session key — suitable for ephemeral
testing nodes.

Usage
-----
    from neuralis.identity import NodeIdentity

    identity = NodeIdentity.load_or_create()   # idempotent
    print(identity.node_id)                    # NRL1abc...
    sig = identity.sign(b"hello mesh")
    assert identity.verify(sig, b"hello mesh")
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NODE_ID_PREFIX = "NRL1"
DEFAULT_KEY_DIR = Path.home() / ".neuralis" / "identity"
PRIVATE_KEY_FILE = "node.key.enc"  # encrypted private key bytes
PUBKEY_FILE = "node.pub"  # plain public key (not secret)
META_FILE = "node.meta.json"  # human-readable metadata
SALT_FILE = "node.salt"  # PBKDF2 salt for key derivation


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IdentityError(Exception):
    """Raised when identity material is missing, corrupt, or invalid."""


class SignatureError(IdentityError):
    """Raised when a signature verification fails."""


# ---------------------------------------------------------------------------
# KeyStore  — handles encrypted persistence of the private key
# ---------------------------------------------------------------------------


class KeyStore:
    """
    Encrypts and persists an Ed25519 private key on disk.

    The private key is encrypted with Fernet (AES-128-CBC + HMAC-SHA256).
    The Fernet key itself is derived via PBKDF2-HMAC-SHA256 from a *machine
    secret* (env var NEURALIS_MACHINE_SECRET) mixed with a per-install salt.

    If NEURALIS_MACHINE_SECRET is not set, a random 32-byte secret is
    generated and stored alongside the key material in `node.salt`.
    THIS IS LESS SECURE — suitable only for development nodes.
    """

    def __init__(self, key_dir: Path = DEFAULT_KEY_DIR):
        self.key_dir = key_dir
        self.key_dir.mkdir(parents=True, exist_ok=True)
        self._fernet: Optional[Fernet] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_salt(self) -> bytes:
        salt_path = self.key_dir / SALT_FILE
        if salt_path.exists():
            return salt_path.read_bytes()
        salt = os.urandom(32)
        salt_path.write_bytes(salt)
        salt_path.chmod(0o600)
        logger.debug("Generated new PBKDF2 salt at %s", salt_path)
        return salt

    def _derive_fernet_key(self, machine_secret: bytes, salt: bytes) -> Fernet:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480_000,  # NIST 2023 recommendation for SHA-256
        )
        key_material = kdf.derive(machine_secret)
        fernet_key = base64.urlsafe_b64encode(key_material)
        return Fernet(fernet_key)

    def _fernet_instance(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        salt = self._get_or_create_salt()
        raw_secret = os.environ.get("NEURALIS_MACHINE_SECRET")
        if raw_secret:
            machine_secret = raw_secret.encode()
            logger.info("KeyStore: using NEURALIS_MACHINE_SECRET for key derivation")
        else:
            # Fallback: read/create a random local secret next to the salt
            secret_path = self.key_dir / "node.secret"
            if secret_path.exists():
                machine_secret = secret_path.read_bytes()
            else:
                machine_secret = os.urandom(32)
                secret_path.write_bytes(machine_secret)
                secret_path.chmod(0o600)
                logger.warning(
                    "KeyStore: no NEURALIS_MACHINE_SECRET set — "
                    "using randomly generated local secret at %s. "
                    "Set the env var for production nodes.",
                    secret_path,
                )
        self._fernet = self._derive_fernet_key(machine_secret, salt)
        return self._fernet

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_private_key(self, private_key: Ed25519PrivateKey) -> None:
        """Encrypt and persist a private key to disk."""
        raw_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        encrypted = self._fernet_instance().encrypt(raw_bytes)
        key_path = self.key_dir / PRIVATE_KEY_FILE
        key_path.write_bytes(encrypted)
        key_path.chmod(0o600)
        logger.debug("Private key saved (encrypted) to %s", key_path)

    def load_private_key(self) -> Ed25519PrivateKey:
        """Decrypt and return the private key from disk."""
        key_path = self.key_dir / PRIVATE_KEY_FILE
        if not key_path.exists():
            raise IdentityError(f"No private key found at {key_path}")
        encrypted = key_path.read_bytes()
        try:
            raw_bytes = self._fernet_instance().decrypt(encrypted)
        except Exception as exc:
            raise IdentityError(
                "Failed to decrypt private key — wrong machine secret?"
            ) from exc
        return Ed25519PrivateKey.from_private_bytes(raw_bytes)

    def save_public_key(self, public_key: Ed25519PublicKey) -> None:
        """Persist the raw public key bytes (not secret)."""
        pub_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        pub_path = self.key_dir / PUBKEY_FILE
        pub_path.write_bytes(pub_bytes)
        pub_path.chmod(0o644)

    def load_public_key(self) -> Ed25519PublicKey:
        """Load the public key from disk."""
        pub_path = self.key_dir / PUBKEY_FILE
        if not pub_path.exists():
            raise IdentityError(f"No public key found at {pub_path}")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey as PK,
        )

        return (
            serialization.load_der_public_key(
                serialization.Encoding.DER  # re-derive via raw path below
            )
            if False
            else _load_raw_ed25519_public_key(pub_path.read_bytes())
        )

    def key_exists(self) -> bool:
        return (self.key_dir / PRIVATE_KEY_FILE).exists()


def _load_raw_ed25519_public_key(raw: bytes) -> Ed25519PublicKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    # cryptography lib requires going through load_der_public_key or
    # reconstructing via private key; for raw bytes we use this path:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519.Ed25519PublicKey.from_public_bytes(raw)


# ---------------------------------------------------------------------------
# NodeIdentity
# ---------------------------------------------------------------------------


@dataclass
class NodeIdentity:
    """
    The complete identity of a Neuralis node.

    Attributes
    ----------
    node_id     : Human-readable node address (NRL1 + base58(sha256(pubkey)))
    peer_id     : libp2p-compatible peer identifier string
    public_key  : Ed25519 public key object
    created_at  : Unix timestamp of identity creation
    alias       : Optional human-chosen display name

    The private key is intentionally NOT stored on this dataclass.
    It is accessed only through the KeyStore when signing is required.
    """

    node_id: str
    peer_id: str
    public_key: Ed25519PublicKey
    created_at: float
    alias: Optional[str] = None
    _private_key: Optional[Ed25519PrivateKey] = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def create_new(
        cls,
        key_dir: Path = DEFAULT_KEY_DIR,
        alias: Optional[str] = None,
    ) -> "NodeIdentity":
        """
        Generate a brand-new Ed25519 identity and persist it.

        This is called exactly once per node installation.
        Subsequent starts use `load_or_create()`.
        """
        logger.info("Generating new Neuralis node identity…")
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        node_id = _derive_node_id(public_key)
        peer_id = _derive_peer_id(public_key)
        created_at = time.time()

        store = KeyStore(key_dir)
        store.save_private_key(private_key)
        store.save_public_key(public_key)

        identity = cls(
            node_id=node_id,
            peer_id=peer_id,
            public_key=public_key,
            created_at=created_at,
            alias=alias,
            _private_key=private_key,
        )
        identity._save_meta(key_dir)

        logger.info("New node identity created: %s", node_id)
        return identity

    @classmethod
    def load(cls, key_dir: Path = DEFAULT_KEY_DIR) -> "NodeIdentity":
        """
        Load an existing identity from disk.

        Raises IdentityError if no identity is found.
        """
        store = KeyStore(key_dir)
        if not store.key_exists():
            raise IdentityError(
                f"No identity found in {key_dir}. Run create_new() first."
            )

        private_key = store.load_private_key()
        public_key = private_key.public_key()

        meta = _load_meta(key_dir)
        node_id = meta.get("node_id") or _derive_node_id(public_key)
        peer_id = meta.get("peer_id") or _derive_peer_id(public_key)

        identity = cls(
            node_id=node_id,
            peer_id=peer_id,
            public_key=public_key,
            created_at=meta.get("created_at", 0.0),
            alias=meta.get("alias"),
            _private_key=private_key,
        )
        logger.info("Loaded node identity: %s", node_id)
        return identity

    @classmethod
    def load_or_create(
        cls,
        key_dir: Path = DEFAULT_KEY_DIR,
        alias: Optional[str] = None,
    ) -> "NodeIdentity":
        """
        Idempotent entry point — load if exists, create if not.

        This is what node startup code should always call.
        """
        store = KeyStore(key_dir)
        if store.key_exists():
            return cls.load(key_dir)
        return cls.create_new(key_dir, alias=alias)

    # ------------------------------------------------------------------
    # Cryptographic operations
    # ------------------------------------------------------------------

    def sign(self, data: bytes) -> bytes:
        """
        Sign arbitrary bytes with the node's private key.

        Returns raw 64-byte Ed25519 signature.
        Raises IdentityError if the private key is not available in memory
        (e.g. this is a remote peer's identity reconstructed from their pubkey).
        """
        if self._private_key is None:
            raise IdentityError(
                "Cannot sign: private key not loaded for this identity."
            )
        return self._private_key.sign(data)

    def verify(self, signature: bytes, data: bytes) -> bool:
        """
        Verify a signature against this node's public key.

        Returns True on success, False on invalid signature (never raises).
        Use this for verifying messages claimed to come from THIS node.
        For remote peers, use NodeIdentity.verify_with_pubkey().
        """
        try:
            self.public_key.verify(signature, data)
            return True
        except Exception:
            return False

    @staticmethod
    def verify_with_pubkey(
        public_key_bytes: bytes,
        signature: bytes,
        data: bytes,
    ) -> bool:
        """
        Verify a signature from a REMOTE peer given their raw public key bytes.

        This is the primary method for verifying inter-node messages.
        """
        try:
            pub = _load_raw_ed25519_public_key(public_key_bytes)
            pub.verify(signature, data)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Serialisation helpers (for broadcasting to peers)
    # ------------------------------------------------------------------

    def public_key_bytes(self) -> bytes:
        """Return raw 32-byte Ed25519 public key."""
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_key_hex(self) -> str:
        return self.public_key_bytes().hex()

    def to_peer_card(self) -> dict:
        """
        Minimal public representation of this node — safe to broadcast.

        The peer card is gossiped across the mesh so other nodes can
        verify messages and address communications.
        """
        return {
            "node_id": self.node_id,
            "peer_id": self.peer_id,
            "public_key": self.public_key_hex(),
            "alias": self.alias,
            "created_at": self.created_at,
        }

    def signed_peer_card(self) -> dict:
        """
        Peer card with a self-signature — proves key ownership.

        Recipients can verify: verify_with_pubkey(card["public_key"],
                                                  card["signature"],
                                                  card_payload_bytes)
        """
        card = self.to_peer_card()
        # Canonical payload = deterministic JSON (sorted keys, no whitespace)
        payload = json.dumps(card, sort_keys=True, separators=(",", ":")).encode()
        signature = self.sign(payload)
        return {**card, "signature": base64.b64encode(signature).decode()}

    # ------------------------------------------------------------------
    # Metadata persistence
    # ------------------------------------------------------------------

    def _save_meta(self, key_dir: Path) -> None:
        meta = {
            "node_id": self.node_id,
            "peer_id": self.peer_id,
            "alias": self.alias,
            "created_at": self.created_at,
            "neuralis_version": "0.1.0",
        }
        meta_path = key_dir / META_FILE
        meta_path.write_text(json.dumps(meta, indent=2))
        meta_path.chmod(0o644)

    def set_alias(self, alias: str, key_dir: Path = DEFAULT_KEY_DIR) -> None:
        """Update the human-readable alias and persist it."""
        self.alias = alias
        self._save_meta(key_dir)

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        alias_part = f" ({self.alias})" if self.alias else ""
        return f"<NodeIdentity {self.node_id}{alias_part}>"

    def __repr__(self) -> str:
        return self.__str__()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_encode(data: bytes) -> str:
    """Minimal Base58 encoder (Bitcoin alphabet)."""
    count = 0
    for byte in data:
        if byte == 0:
            count += 1
        else:
            break
    num = int.from_bytes(data, "big")
    result = []
    while num > 0:
        num, remainder = divmod(num, 58)
        result.append(_BASE58_ALPHABET[remainder : remainder + 1])
    return (b"1" * count + b"".join(reversed(result))).decode()


def _derive_node_id(public_key: Ed25519PublicKey) -> str:
    """
    Derive a human-readable node ID from a public key.

    Format: NRL1<base58(sha256(raw_pubkey_bytes))>
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw).digest()
    return NODE_ID_PREFIX + _base58_encode(digest)


def _derive_peer_id(public_key: Ed25519PublicKey) -> str:
    """
    Derive a libp2p-compatible Peer ID.

    libp2p Peer IDs for Ed25519 keys are:
        base58btc( multihash( identity, pubkey_protobuf ) )

    We use a simplified but compatible form:
        "12D3KooW" prefix + base58(sha256(pubkey))   [development mode]

    Full libp2p protobuf encoding is handled in the mesh-transport module
    where py-libp2p is available.  This string form is used for display
    and config only.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw).digest()
    return "12D3KooW" + _base58_encode(digest)[:32]


def _load_meta(key_dir: Path) -> dict:
    meta_path = key_dir / META_FILE
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        logger.warning("Corrupt node.meta.json at %s — ignoring", meta_path)
        return {}
