"""
neuralis.mesh.peers
===================
Peer data structures for the Neuralis mesh.

A Peer represents a remote Neuralis node that we know about — either discovered
via mDNS, announced via DHT, manually bootstrapped, or learned from a peer card
gossip message.

PeerStore is the in-memory registry of all known/connected peers.  It is the
single source of truth for peer state within the mesh-transport module.

PeerCard is the wire-format representation of a peer's identity, received as
part of the mesh handshake.  It is always verified against the peer's public
key before being accepted into the store.

MessageEnvelope is the signed, typed wrapper around every message sent over
the mesh.  Every inter-node message — regardless of payload type — is wrapped
in an envelope, signed by the sender, and verified by the receiver.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PeerStatus(str, Enum):
    DISCOVERED  = "DISCOVERED"   # known address, never connected
    CONNECTING  = "CONNECTING"   # dial in progress
    CONNECTED   = "CONNECTED"    # active stream open
    HANDSHAKING = "HANDSHAKING"  # connected, waiting for peer card
    VERIFIED    = "VERIFIED"     # peer card received and signature valid
    DEGRADED    = "DEGRADED"     # connected but slow / partial failure
    DISCONNECTED = "DISCONNECTED" # was connected, now gone
    BANNED      = "BANNED"       # signature forgery or protocol violation


class MessageType(str, Enum):
    # Handshake / identity
    PEER_CARD      = "PEER_CARD"       # initial identity broadcast
    PEER_CARD_ACK  = "PEER_CARD_ACK"   # acknowledge receipt of peer card
    # Discovery
    PEER_LIST      = "PEER_LIST"       # share list of known peers
    PEER_LIST_REQ  = "PEER_LIST_REQ"   # request peer list
    # Liveness
    PING           = "PING"            # heartbeat probe
    PONG           = "PONG"            # heartbeat response
    # Content routing (stub — expanded in Module 3)
    CONTENT_ANNOUNCE = "CONTENT_ANNOUNCE"
    CONTENT_REQUEST  = "CONTENT_REQUEST"
    CONTENT_RESPONSE = "CONTENT_RESPONSE"
    # Agent messaging (stub — expanded in Module 4/5)
    AGENT_MSG      = "AGENT_MSG"
    # Disconnect signaling
    GOODBYE        = "GOODBYE"


# ---------------------------------------------------------------------------
# PeerInfo
# ---------------------------------------------------------------------------

@dataclass
class PeerInfo:
    """
    Everything the local node knows about a remote peer.

    Fields
    ------
    node_id         : NRL1... identifier (derived from public key)
    peer_id         : 12D3KooW... libp2p-style peer identifier
    public_key_hex  : hex-encoded raw Ed25519 public key (32 bytes → 64 chars)
    addresses       : known multiaddrs for this peer
    alias           : optional human-readable name from their peer card
    status          : current connection state
    first_seen      : unix timestamp of discovery
    last_seen       : unix timestamp of last successful contact
    last_ping_ms    : round-trip time of most recent ping, or None
    failed_attempts : consecutive failed connection attempts
    """
    node_id: str
    peer_id: str
    public_key_hex: str
    addresses: List[str] = field(default_factory=list)
    alias: Optional[str] = None
    status: PeerStatus = PeerStatus.DISCOVERED
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_ping_ms: Optional[float] = None
    failed_attempts: int = 0

    def public_key_bytes(self) -> bytes:
        return bytes.fromhex(self.public_key_hex)

    def touch(self) -> None:
        """Update last_seen to now."""
        self.last_seen = time.time()

    def mark_connected(self) -> None:
        self.status = PeerStatus.CONNECTED
        self.failed_attempts = 0
        self.touch()

    def mark_verified(self) -> None:
        self.status = PeerStatus.VERIFIED
        self.touch()

    def mark_disconnected(self) -> None:
        self.status = PeerStatus.DISCONNECTED
        self.touch()

    def mark_failed(self) -> None:
        self.failed_attempts += 1
        self.touch()

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "peer_id": self.peer_id,
            "public_key": self.public_key_hex,
            "addresses": self.addresses,
            "alias": self.alias,
            "status": self.status.value,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_ping_ms": self.last_ping_ms,
            "failed_attempts": self.failed_attempts,
        }

    @classmethod
    def from_peer_card(cls, card: dict) -> "PeerInfo":
        """Construct a PeerInfo from a verified peer card dict."""
        return cls(
            node_id=card["node_id"],
            peer_id=card["peer_id"],
            public_key_hex=card["public_key"],
            alias=card.get("alias"),
            addresses=card.get("addresses", []),
            status=PeerStatus.DISCOVERED,
        )

    def __repr__(self) -> str:
        return (
            f"<PeerInfo {self.node_id[:16]}… "
            f"status={self.status.value} "
            f"ping={self.last_ping_ms}ms>"
        )


# ---------------------------------------------------------------------------
# PeerStore
# ---------------------------------------------------------------------------

class PeerStore:
    """
    In-memory registry of all known peers.

    Thread-safety: this implementation is NOT thread-safe by itself.
    The MeshHost wraps all mutations in asyncio tasks on a single event loop,
    so concurrent access is serialised naturally.  If you add threading later,
    wrap mutations with asyncio.Lock.

    Key lookups
    -----------
    Peers are indexed by node_id (primary) and peer_id (secondary).
    Both indices are kept in sync on every mutation.
    """

    def __init__(self):
        self._by_node_id: Dict[str, PeerInfo] = {}
        self._by_peer_id: Dict[str, str] = {}   # peer_id → node_id

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_or_update(self, peer: PeerInfo) -> PeerInfo:
        """
        Insert a new peer or update an existing one.

        If a peer with the same node_id exists, its addresses, alias,
        and last_seen are merged.  Status is only updated if the new
        status is 'more advanced' (e.g. VERIFIED > CONNECTED > DISCOVERED).
        """
        existing = self._by_node_id.get(peer.node_id)
        if existing is None:
            self._by_node_id[peer.node_id] = peer
            self._by_peer_id[peer.peer_id] = peer.node_id
            return peer

        # Merge addresses
        known = set(existing.addresses)
        for addr in peer.addresses:
            if addr not in known:
                existing.addresses.append(addr)

        # Update alias if we now have one
        if peer.alias and not existing.alias:
            existing.alias = peer.alias

        # Advance status (never go backwards unless explicit disconnect)
        _STATUS_RANK = {
            PeerStatus.DISCOVERED: 0,
            PeerStatus.CONNECTING: 1,
            PeerStatus.CONNECTED: 2,
            PeerStatus.HANDSHAKING: 3,
            PeerStatus.VERIFIED: 4,
            PeerStatus.DEGRADED: 2,
            PeerStatus.DISCONNECTED: 0,
            PeerStatus.BANNED: -1,
        }
        if _STATUS_RANK.get(peer.status, 0) > _STATUS_RANK.get(existing.status, 0):
            existing.status = peer.status

        existing.touch()
        return existing

    def remove(self, node_id: str) -> Optional[PeerInfo]:
        peer = self._by_node_id.pop(node_id, None)
        if peer:
            self._by_peer_id.pop(peer.peer_id, None)
        return peer

    def ban(self, node_id: str) -> None:
        peer = self._by_node_id.get(node_id)
        if peer:
            peer.status = PeerStatus.BANNED

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_by_node_id(self, node_id: str) -> Optional[PeerInfo]:
        return self._by_node_id.get(node_id)

    def get_by_peer_id(self, peer_id: str) -> Optional[PeerInfo]:
        node_id = self._by_peer_id.get(peer_id)
        if node_id:
            return self._by_node_id.get(node_id)
        return None

    def all_peers(self) -> List[PeerInfo]:
        return list(self._by_node_id.values())

    def connected_peers(self) -> List[PeerInfo]:
        return [
            p for p in self._by_node_id.values()
            if p.status in (PeerStatus.CONNECTED, PeerStatus.VERIFIED)
        ]

    def verified_peers(self) -> List[PeerInfo]:
        return [
            p for p in self._by_node_id.values()
            if p.status == PeerStatus.VERIFIED
        ]

    def count(self) -> int:
        return len(self._by_node_id)

    def connected_count(self) -> int:
        return len(self.connected_peers())

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._by_node_id

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:
        return (
            f"<PeerStore total={self.count()} "
            f"connected={self.connected_count()}>"
        )


# ---------------------------------------------------------------------------
# MessageEnvelope
# ---------------------------------------------------------------------------

@dataclass
class MessageEnvelope:
    """
    Signed wrapper around every inter-node message.

    Every message sent across the mesh — regardless of type or payload —
    is wrapped in an envelope.  The envelope is signed by the sender's
    Ed25519 private key.  The receiver must verify the signature before
    processing the payload.

    Wire format (JSON):
    {
        "v":         1,                  // protocol version
        "type":      "PING",             // MessageType
        "sender_id": "NRL1...",          // sender node_id
        "sender_pk": "aabbcc...",        // sender public key hex
        "msg_id":    "sha256hex",        // sha256(sender_id+type+ts+payload)
        "timestamp": 1234567890.123,     // unix float
        "ttl":       8,                  // hops remaining (decremented on relay)
        "payload":   { ... },            // message-type-specific content
        "signature": "base64..."         // Ed25519 sig over canonical JSON
    }

    The signature covers all fields EXCEPT "signature" itself, using
    canonical JSON (sorted keys, no whitespace).
    """

    version: int
    type: MessageType
    sender_id: str
    sender_pk: str          # hex
    msg_id: str
    timestamp: float
    ttl: int
    payload: dict
    signature: str          # base64

    CURRENT_VERSION = 1
    DEFAULT_TTL = 8

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        msg_type: MessageType,
        payload: dict,
        sender_id: str,
        sender_pk_hex: str,
        sign_fn,                    # callable: (bytes) -> bytes
        ttl: int = DEFAULT_TTL,
    ) -> "MessageEnvelope":
        """
        Build and sign a new outbound message envelope.

        Parameters
        ----------
        msg_type    : the MessageType enum value
        payload     : dict — message-specific content (must be JSON-serialisable)
        sender_id   : NRL1... node_id of the local node
        sender_pk_hex : hex public key of the local node
        sign_fn     : callable that takes bytes and returns a 64-byte Ed25519 sig
        ttl         : hop count (default 8)
        """
        timestamp = time.time()

        # Derive a deterministic message ID
        id_material = f"{sender_id}:{msg_type.value}:{timestamp}:{json.dumps(payload, sort_keys=True)}"
        msg_id = hashlib.sha256(id_material.encode()).hexdigest()[:32]

        envelope_data = {
            "v": cls.CURRENT_VERSION,
            "type": msg_type.value,
            "sender_id": sender_id,
            "sender_pk": sender_pk_hex,
            "msg_id": msg_id,
            "timestamp": timestamp,
            "ttl": ttl,
            "payload": payload,
        }

        # Sign the canonical representation (sorted keys, compact)
        canonical = json.dumps(envelope_data, sort_keys=True, separators=(",", ":")).encode()
        raw_sig = sign_fn(canonical)
        signature = base64.b64encode(raw_sig).decode()

        return cls(
            version=cls.CURRENT_VERSION,
            type=msg_type,
            sender_id=sender_id,
            sender_pk=sender_pk_hex,
            msg_id=msg_id,
            timestamp=timestamp,
            ttl=ttl,
            payload=payload,
            signature=signature,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Serialise to UTF-8 JSON bytes for transmission."""
        return json.dumps(self.to_dict(), separators=(",", ":")).encode()

    def to_dict(self) -> dict:
        return {
            "v": self.version,
            "type": self.type.value,
            "sender_id": self.sender_id,
            "sender_pk": self.sender_pk,
            "msg_id": self.msg_id,
            "timestamp": self.timestamp,
            "ttl": self.ttl,
            "payload": self.payload,
            "signature": self.signature,
        }

    @classmethod
    def from_bytes(cls, data: bytes) -> "MessageEnvelope":
        """Deserialise from wire bytes.  Raises ValueError on malformed input."""
        try:
            d = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Malformed envelope bytes: {exc}") from exc
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: dict) -> "MessageEnvelope":
        required = {"v", "type", "sender_id", "sender_pk", "msg_id",
                    "timestamp", "ttl", "payload", "signature"}
        missing = required - d.keys()
        if missing:
            raise ValueError(f"Envelope missing fields: {missing}")
        try:
            msg_type = MessageType(d["type"])
        except ValueError:
            raise ValueError(f"Unknown message type: {d['type']}")
        return cls(
            version=d["v"],
            type=msg_type,
            sender_id=d["sender_id"],
            sender_pk=d["sender_pk"],
            msg_id=d["msg_id"],
            timestamp=d["timestamp"],
            ttl=d["ttl"],
            payload=d["payload"],
            signature=d["signature"],
        )

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self) -> bool:
        """
        Verify the envelope's signature against the embedded public key.

        Returns True if valid, False if invalid.  Never raises.

        The verifier reconstructs the exact canonical JSON that was signed
        (all fields except "signature"), then checks the Ed25519 signature.
        """
        try:
            envelope_data = {
                "v": self.version,
                "type": self.type.value,
                "sender_id": self.sender_id,
                "sender_pk": self.sender_pk,
                "msg_id": self.msg_id,
                "timestamp": self.timestamp,
                "ttl": self.ttl,
                "payload": self.payload,
            }
            canonical = json.dumps(
                envelope_data, sort_keys=True, separators=(",", ":")
            ).encode()
            pub_bytes = bytes.fromhex(self.sender_pk)
            sig_bytes = base64.b64decode(self.signature)

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
            pub.verify(sig_bytes, canonical)
            return True
        except Exception:
            return False

    def is_expired(self, max_age_seconds: float = 30.0) -> bool:
        """True if this message is older than max_age_seconds."""
        return (time.time() - self.timestamp) > max_age_seconds

    def decrement_ttl(self) -> "MessageEnvelope":
        """Return a copy with TTL decremented by 1."""
        import dataclasses
        return dataclasses.replace(self, ttl=self.ttl - 1)

    def __repr__(self) -> str:
        return (
            f"<MessageEnvelope {self.type.value} "
            f"from={self.sender_id[:16]}… "
            f"id={self.msg_id[:8]}>"
        )
