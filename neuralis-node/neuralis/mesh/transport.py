"""
neuralis.mesh.transport
=======================
Encrypted P2P transport for Neuralis mesh connections.

This module handles the raw TCP connection layer:
- Dialing outbound connections to peers
- Accepting inbound connections
- The Noise-inspired handshake (simplified, using Ed25519 + AES-GCM)
- Framed message I/O over persistent streams

Encryption Protocol (NeuralisNoise)
------------------------------------
Full Noise Protocol Framework (noise-protocol.org) requires the `noiseprotocol`
library.  We implement a compatible subset using stdlib + cryptography:

    1. Initiator sends:  [ephemeral_pubkey (32 bytes)] + [static_pubkey_sig (64 bytes)]
    2. Responder sends:  [ephemeral_pubkey (32 bytes)] + [static_pubkey_sig (64 bytes)]
    3. Both sides perform X25519 ECDH on the ephemeral keys → shared_secret
    4. Session key = HKDF-SHA256(shared_secret, "neuralis-v1")
    5. All subsequent messages are AES-256-GCM encrypted with the session key
       and a per-message nonce (12 bytes, monotonically incrementing)

Wire frame format (after handshake):
    [4 bytes big-endian length] [N bytes AES-GCM ciphertext]
    where ciphertext = nonce(12) + tag(16) + plaintext

This gives us:
- Perfect forward secrecy (ephemeral keys discarded after handshake)
- Mutual authentication (both sides sign their ephemeral key with Ed25519)
- Replay protection (nonce counter)
- Message integrity (GCM authentication tag)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HANDSHAKE_TIMEOUT  = 10.0    # seconds
MAX_MESSAGE_SIZE   = 4 * 1024 * 1024   # 4 MB hard cap per message
FRAME_HEADER_SIZE  = 4       # big-endian uint32 length prefix
NONCE_SIZE         = 12      # AES-GCM nonce bytes
GCM_TAG_SIZE       = 16      # AES-GCM authentication tag bytes
HKDF_INFO          = b"neuralis-transport-v1"
PROTOCOL_MAGIC     = b"NRL\x02"   # 4-byte stream magic


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HandshakeError(Exception):
    """Raised when the Noise-like handshake fails."""

class TransportError(Exception):
    """Raised on framing or encryption errors."""

class PeerBannedError(Exception):
    """Raised when a peer is banned (signature forgery detected)."""


# ---------------------------------------------------------------------------
# Session — holds per-connection crypto state
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """
    Per-connection encrypted session state.

    remote_node_id     : NRL1... of the connected peer
    remote_public_key  : verified Ed25519 public key bytes of the peer
    session_key        : 32-byte AES-256 session key (from HKDF)
    send_nonce         : monotonically incrementing send nonce counter
    recv_nonce         : monotonically incrementing recv nonce counter
    established_at     : unix timestamp
    bytes_sent         : total plaintext bytes sent this session
    bytes_recv         : total plaintext bytes received this session
    """
    remote_node_id: str
    remote_public_key: bytes
    session_key: bytes
    send_nonce: int = 0
    recv_nonce: int = 0
    established_at: float = field(default_factory=time.time)
    bytes_sent: int = 0
    bytes_recv: int = 0

    def _aes(self) -> AESGCM:
        return AESGCM(self.session_key)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext → nonce + ciphertext (includes GCM tag)."""
        nonce = self.send_nonce.to_bytes(NONCE_SIZE, "big")
        self.send_nonce += 1
        ct = self._aes().encrypt(nonce, plaintext, None)
        self.bytes_sent += len(plaintext)
        return nonce + ct

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt nonce + ciphertext → plaintext.  Raises on auth failure."""
        if len(data) < NONCE_SIZE + GCM_TAG_SIZE:
            raise TransportError("Ciphertext too short")
        nonce = data[:NONCE_SIZE]
        ct = data[NONCE_SIZE:]

        # Replay protection: nonce must match expected counter
        expected = self.recv_nonce.to_bytes(NONCE_SIZE, "big")
        if not hmac.compare_digest(nonce, expected):
            raise TransportError(
                f"Nonce mismatch: expected {self.recv_nonce}, "
                f"got {int.from_bytes(nonce, 'big')}"
            )
        self.recv_nonce += 1

        try:
            pt = self._aes().decrypt(nonce, ct, None)
        except Exception as exc:
            raise TransportError(f"AES-GCM decryption failed: {exc}") from exc
        self.bytes_recv += len(pt)
        return pt

    def stats(self) -> dict:
        age = time.time() - self.established_at
        return {
            "remote_node_id": self.remote_node_id,
            "uptime_seconds": round(age, 1),
            "bytes_sent": self.bytes_sent,
            "bytes_recv": self.bytes_recv,
            "send_nonce": self.send_nonce,
        }


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

async def _perform_handshake_initiator(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    local_private_key: Ed25519PrivateKey,
    local_node_id: str,
) -> Session:
    """
    Perform the NeuralisNoise handshake as the INITIATOR (dialing side).

    Steps
    -----
    1. Send magic bytes
    2. Generate ephemeral X25519 keypair
    3. Sign our ephemeral public key with our Ed25519 identity key
    4. Send: [static_pubkey_bytes(32)] + [ephemeral_pubkey(32)] + [signature(64)]
    5. Receive: [remote_static_pubkey(32)] + [remote_ephemeral_pubkey(32)] + [sig(64)]
    6. Verify remote signature
    7. ECDH: ephemeral_private * remote_ephemeral_public → shared_secret
    8. Derive session key via HKDF
    """
    # ---- 1. Magic
    writer.write(PROTOCOL_MAGIC)
    await writer.drain()

    # ---- 2. Ephemeral keypair
    eph_private = X25519PrivateKey.generate()
    eph_public_bytes = eph_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    # ---- 3. Static public key bytes
    static_pub_bytes = local_private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    # ---- 4. Sign the ephemeral key with our identity key
    # We sign: MAGIC + static_pub + eph_pub to bind both keys together
    signed_material = PROTOCOL_MAGIC + static_pub_bytes + eph_public_bytes
    signature = local_private_key.sign(signed_material)

    # ---- 5. Send our hello
    hello = static_pub_bytes + eph_public_bytes + signature   # 32+32+64 = 128 bytes
    writer.write(struct.pack(">H", len(hello)))
    writer.write(hello)
    await writer.drain()

    # ---- 6. Receive remote hello
    try:
        length_bytes = await asyncio.wait_for(reader.readexactly(2), HANDSHAKE_TIMEOUT)
        length = struct.unpack(">H", length_bytes)[0]
        if length != 128:
            raise HandshakeError(f"Unexpected hello length: {length}")
        remote_hello = await asyncio.wait_for(reader.readexactly(128), HANDSHAKE_TIMEOUT)
    except asyncio.TimeoutError:
        raise HandshakeError("Handshake timed out waiting for remote hello")
    except asyncio.IncompleteReadError:
        raise HandshakeError("Connection closed during handshake")

    remote_static_pub_bytes  = remote_hello[:32]
    remote_eph_pub_bytes     = remote_hello[32:64]
    remote_signature         = remote_hello[64:128]

    # ---- 7. Verify remote signature
    remote_signed_material = PROTOCOL_MAGIC + remote_static_pub_bytes + remote_eph_pub_bytes
    try:
        remote_static_pub = Ed25519PublicKey.from_public_bytes(remote_static_pub_bytes)
        remote_static_pub.verify(remote_signature, remote_signed_material)
    except Exception as exc:
        raise HandshakeError(f"Remote signature verification failed: {exc}") from exc

    # ---- 8. ECDH
    remote_eph_pub = X25519PublicKey.from_public_bytes(remote_eph_pub_bytes)
    shared_secret = eph_private.exchange(remote_eph_pub)

    # ---- 9. Derive session key
    session_key = _derive_session_key(shared_secret, static_pub_bytes, remote_static_pub_bytes)

    # ---- 10. Derive remote node ID
    remote_node_id = _derive_node_id_from_pubkey(remote_static_pub_bytes)

    logger.debug("Handshake complete (initiator) with %s", remote_node_id[:20])
    return Session(
        remote_node_id=remote_node_id,
        remote_public_key=remote_static_pub_bytes,
        session_key=session_key,
    )


async def _perform_handshake_responder(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    local_private_key: Ed25519PrivateKey,
    local_node_id: str,
) -> Session:
    """
    Perform the NeuralisNoise handshake as the RESPONDER (accepting side).

    Mirror of the initiator, receive-first ordering.
    """
    # ---- 1. Verify magic
    try:
        magic = await asyncio.wait_for(reader.readexactly(4), HANDSHAKE_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        raise HandshakeError("Timeout waiting for magic bytes")
    if magic != PROTOCOL_MAGIC:
        raise HandshakeError(f"Bad protocol magic: {magic!r}")

    # ---- 2. Receive initiator hello
    try:
        length_bytes = await asyncio.wait_for(reader.readexactly(2), HANDSHAKE_TIMEOUT)
        length = struct.unpack(">H", length_bytes)[0]
        if length != 128:
            raise HandshakeError(f"Unexpected hello length: {length}")
        remote_hello = await asyncio.wait_for(reader.readexactly(128), HANDSHAKE_TIMEOUT)
    except asyncio.TimeoutError:
        raise HandshakeError("Handshake timed out")
    except asyncio.IncompleteReadError:
        raise HandshakeError("Connection closed during handshake")

    remote_static_pub_bytes  = remote_hello[:32]
    remote_eph_pub_bytes     = remote_hello[32:64]
    remote_signature         = remote_hello[64:128]

    # ---- 3. Verify remote signature
    remote_signed_material = PROTOCOL_MAGIC + remote_static_pub_bytes + remote_eph_pub_bytes
    try:
        remote_static_pub = Ed25519PublicKey.from_public_bytes(remote_static_pub_bytes)
        remote_static_pub.verify(remote_signature, remote_signed_material)
    except Exception as exc:
        raise HandshakeError(f"Remote signature verification failed: {exc}") from exc

    # ---- 4. Our ephemeral keypair
    eph_private = X25519PrivateKey.generate()
    eph_public_bytes = eph_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    static_pub_bytes = local_private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    # ---- 5. Sign and send our hello
    signed_material = PROTOCOL_MAGIC + static_pub_bytes + eph_public_bytes
    signature = local_private_key.sign(signed_material)
    hello = static_pub_bytes + eph_public_bytes + signature
    writer.write(struct.pack(">H", len(hello)))
    writer.write(hello)
    await writer.drain()

    # ---- 6. ECDH
    remote_eph_pub = X25519PublicKey.from_public_bytes(remote_eph_pub_bytes)
    shared_secret = eph_private.exchange(remote_eph_pub)

    # ---- 7. Derive session key (same derivation, same result on both sides)
    # We use sorted(pubkeys) to ensure both sides derive the same key
    session_key = _derive_session_key(shared_secret, remote_static_pub_bytes, static_pub_bytes)

    remote_node_id = _derive_node_id_from_pubkey(remote_static_pub_bytes)

    logger.debug("Handshake complete (responder) with %s", remote_node_id[:20])
    return Session(
        remote_node_id=remote_node_id,
        remote_public_key=remote_static_pub_bytes,
        session_key=session_key,
    )


def _derive_session_key(
    shared_secret: bytes,
    pubkey_a: bytes,
    pubkey_b: bytes,
) -> bytes:
    """
    Derive a 32-byte AES-256 session key from the X25519 shared secret.

    Uses HKDF-SHA256.  The salt is deterministic (XOR of the two static
    public keys) so both sides always derive the same key regardless of
    which was initiator/responder.
    """
    # Sort pubkeys for determinism (initiator/responder symmetry)
    keys = sorted([pubkey_a, pubkey_b])
    salt = bytes(a ^ b for a, b in zip(keys[0], keys[1]))

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
    )
    return hkdf.derive(shared_secret)


_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _base58_encode(data: bytes) -> str:
    count = sum(1 for b in data if b == 0)
    num = int.from_bytes(data, "big")
    result = []
    while num > 0:
        num, r = divmod(num, 58)
        result.append(_BASE58_ALPHABET[r:r+1])
    return (b"1" * count + b"".join(reversed(result))).decode()

def _derive_node_id_from_pubkey(pub_bytes: bytes) -> str:
    digest = hashlib.sha256(pub_bytes).digest()
    return "NRL1" + _base58_encode(digest)


# ---------------------------------------------------------------------------
# Framed I/O
# ---------------------------------------------------------------------------

async def send_frame(writer: asyncio.StreamWriter, session: Session, data: bytes) -> None:
    """
    Encrypt and send a framed message.

    Frame format: [4-byte big-endian length] [encrypted_data]
    """
    if len(data) > MAX_MESSAGE_SIZE:
        raise TransportError(f"Message too large: {len(data)} > {MAX_MESSAGE_SIZE}")
    encrypted = session.encrypt(data)
    frame = struct.pack(">I", len(encrypted)) + encrypted
    writer.write(frame)
    await writer.drain()


async def recv_frame(reader: asyncio.StreamReader, session: Session) -> bytes:
    """
    Receive and decrypt a framed message.

    Returns plaintext bytes.
    Raises TransportError on decryption failure, asyncio.IncompleteReadError on EOF.
    """
    try:
        header = await reader.readexactly(FRAME_HEADER_SIZE)
    except asyncio.IncompleteReadError:
        raise TransportError("Connection closed (EOF on frame header)")

    length = struct.unpack(">I", header)[0]
    if length > MAX_MESSAGE_SIZE + NONCE_SIZE + GCM_TAG_SIZE:
        raise TransportError(f"Frame too large: {length}")

    try:
        encrypted = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise TransportError("Connection closed (EOF reading frame body)")

    return session.decrypt(encrypted)


# ---------------------------------------------------------------------------
# PeerConnection — wraps a live connection to one peer
# ---------------------------------------------------------------------------

class PeerConnection:
    """
    A live, authenticated, encrypted connection to a single remote peer.

    Created by MeshHost after a successful handshake.  Provides:
    - send(data: bytes) — encrypt and send a raw message
    - recv() — decrypt and return next message (awaitable)
    - close() — terminate the connection

    The receive loop is managed by MeshHost, which calls recv() in a task.
    """

    def __init__(
        self,
        session: Session,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer_addr: str,
    ):
        self.session = session
        self._reader = reader
        self._writer = writer
        self.peer_addr = peer_addr
        self.remote_node_id = session.remote_node_id
        self._closed = False

    @property
    def is_alive(self) -> bool:
        return not self._closed and not self._writer.is_closing()

    async def send(self, data: bytes) -> None:
        """Encrypt and send a message.  Raises TransportError on failure."""
        if self._closed:
            raise TransportError("Cannot send on closed connection")
        try:
            await send_frame(self._writer, self.session, data)
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            self._closed = True
            raise TransportError(f"Send failed: {exc}") from exc

    async def recv(self) -> bytes:
        """Receive and decrypt the next message.  Raises TransportError on EOF."""
        if self._closed:
            raise TransportError("Cannot recv on closed connection")
        try:
            return await recv_frame(self._reader, self.session)
        except TransportError:
            self._closed = True
            raise
        except Exception as exc:
            self._closed = True
            raise TransportError(f"Recv failed: {exc}") from exc

    async def close(self) -> None:
        """Gracefully close the connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass

    def stats(self) -> dict:
        return {
            "peer_addr": self.peer_addr,
            "alive": self.is_alive,
            **self.session.stats(),
        }

    def __repr__(self) -> str:
        return (
            f"<PeerConnection {self.remote_node_id[:16]}… "
            f"@ {self.peer_addr} alive={self.is_alive}>"
        )


# ---------------------------------------------------------------------------
# dial / accept — top-level connection factories
# ---------------------------------------------------------------------------

async def dial(
    host: str,
    port: int,
    local_private_key: Ed25519PrivateKey,
    local_node_id: str,
    timeout: float = 15.0,
) -> PeerConnection:
    """
    Dial an outbound connection to a peer and complete the handshake.

    Parameters
    ----------
    host             : IP or hostname to connect to
    port             : TCP port
    local_private_key: our Ed25519 identity key
    local_node_id    : our NRL1... node ID
    timeout          : total connection + handshake timeout in seconds

    Returns
    -------
    PeerConnection  — ready to send/recv

    Raises
    ------
    HandshakeError  — if handshake fails
    TransportError  — if connection fails
    asyncio.TimeoutError — if timeout expires
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise TransportError(f"Failed to connect to {host}:{port}: {exc}") from exc

    peer_addr = f"{host}:{port}"
    logger.debug("TCP connected to %s — starting handshake", peer_addr)

    try:
        session = await asyncio.wait_for(
            _perform_handshake_initiator(reader, writer, local_private_key, local_node_id),
            timeout=HANDSHAKE_TIMEOUT,
        )
    except (HandshakeError, asyncio.TimeoutError):
        writer.close()
        raise

    logger.info("Connected to %s (node=%s)", peer_addr, session.remote_node_id[:20])
    return PeerConnection(session, reader, writer, peer_addr)


async def accept(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    local_private_key: Ed25519PrivateKey,
    local_node_id: str,
) -> PeerConnection:
    """
    Accept an inbound connection and complete the handshake.

    Called by asyncio.start_server's client_connected_cb.

    Returns
    -------
    PeerConnection  — ready to send/recv

    Raises
    ------
    HandshakeError  — if handshake fails
    """
    peer_addr = "{}:{}".format(*writer.get_extra_info("peername", ("?", 0)))
    logger.debug("Inbound TCP from %s — starting handshake", peer_addr)

    session = await asyncio.wait_for(
        _perform_handshake_responder(reader, writer, local_private_key, local_node_id),
        timeout=HANDSHAKE_TIMEOUT,
    )

    logger.info("Accepted from %s (node=%s)", peer_addr, session.remote_node_id[:20])
    return PeerConnection(session, reader, writer, peer_addr)
