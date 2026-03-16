"""
neuralis.mesh.host
==================
MeshHost — the central coordinator for Neuralis P2P mesh networking.

MeshHost owns the full lifecycle of mesh participation:
    - Starts the TCP listener (accepting inbound connections)
    - Starts the DiscoveryEngine (mDNS + bootstrap)
    - Dials outbound connections to discovered peers
    - Manages the PeerStore (all known/connected peers)
    - Runs the per-peer receive loops (decrypts + dispatches messages)
    - Sends signed MessageEnvelopes to individual peers or all peers
    - Runs the heartbeat (PING/PONG) to detect dead connections
    - Enforces max_peers and reconnection backoff

MeshHost is the object registered as a subsystem on the Node:
    node.register_subsystem("mesh", mesh_host)

Usage
-----
    from neuralis.mesh.host import MeshHost

    mesh = MeshHost(node)
    await mesh.start()
    # node is now live on the mesh

    await mesh.broadcast(MessageType.PING, {})
    await mesh.send_to(peer_node_id, MessageType.AGENT_MSG, {"task": "..."})

    await mesh.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional, Set

from neuralis.mesh.discovery import DiscoveryEngine, PeerAnnouncement
from neuralis.mesh.peers import (
    MessageEnvelope,
    MessageType,
    PeerInfo,
    PeerStatus,
    PeerStore,
)
from neuralis.mesh.transport import (
    HandshakeError,
    PeerConnection,
    TransportError,
    accept,
    dial,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PING_INTERVAL        = 30.0   # seconds between heartbeat pings
PING_TIMEOUT         = 10.0   # seconds to wait for PONG
RECONNECT_BASE_DELAY = 5.0    # seconds before first reconnect attempt
RECONNECT_MAX_DELAY  = 120.0  # cap on exponential backoff
MAX_RECONNECT_TRIES  = 5      # give up after this many failures


# ---------------------------------------------------------------------------
# MessageHandler type
# ---------------------------------------------------------------------------

# A handler is a coroutine function:  async def handler(envelope, peer_info) -> None
MessageHandler = Callable[[MessageEnvelope, Optional[PeerInfo]], None]


# ---------------------------------------------------------------------------
# MeshHost
# ---------------------------------------------------------------------------

class MeshHost:
    """
    Central P2P mesh coordinator for a Neuralis node.

    Parameters
    ----------
    node    : neuralis.node.Node  — the running node (provides identity + config)

    Attributes
    ----------
    peer_store          : PeerStore — all known peers
    connections         : dict[node_id → PeerConnection] — live connections
    """

    def __init__(self, node):
        self._node = node
        self._identity = node.identity
        self._config = node.config

        self.peer_store = PeerStore()
        self.connections: Dict[str, PeerConnection] = {}

        # Message handlers registered by subsystems
        self._handlers: Dict[MessageType, List[MessageHandler]] = {}

        # Internal state
        self._server: Optional[asyncio.AbstractServer] = None
        self._discovery: Optional[DiscoveryEngine] = None
        self._tasks: List[asyncio.Task] = []
        self._pending_pings: Dict[str, asyncio.Future] = {}
        self._running = False
        self._dialing: Set[str] = set()   # node_ids currently being dialed

        # Register default message handlers
        self._register_default_handlers()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start the mesh host:
        1. Bind TCP listener
        2. Start DiscoveryEngine
        3. Start heartbeat loop
        """
        if self._running:
            return
        self._running = True

        cfg = self._config.network
        listen_addrs = cfg.listen_addresses

        # Parse port from first listen address
        port = _parse_port_from_multiaddr(listen_addrs[0]) if listen_addrs else 7101
        host = "0.0.0.0"

        # ---- TCP Server
        try:
            self._server = await asyncio.start_server(
                self._handle_inbound,
                host=host,
                port=port,
            )
            logger.info("MeshHost listening on %s:%d", host, port)
        except OSError as exc:
            logger.error("Failed to bind TCP listener on %s:%d: %s", host, port, exc)
            raise

        # ---- Discovery Engine
        self._discovery = DiscoveryEngine(
            node_id=self._identity.node_id,
            peer_id=self._identity.peer_id,
            public_key_hex=self._identity.public_key_hex(),
            listen_port=port,
            bootstrap_peers=cfg.bootstrap_peers,
            enable_mdns=cfg.enable_mdns,
            on_peer_discovered=self._on_peer_discovered,
            mdns_interval=cfg.mdns_interval_secs,
            alias=self._identity.alias,
        )
        await self._discovery.start()

        # ---- Background tasks
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

        # Register with node
        self._node.register_subsystem("mesh", self)
        self._node.on_shutdown(self.stop)

        logger.info(
            "MeshHost started | node=%s | mdns=%s | dht=%s",
            self._identity.node_id[:20],
            cfg.enable_mdns,
            cfg.enable_dht,
        )

    async def stop(self) -> None:
        """Gracefully shut down all connections and background tasks."""
        if not self._running:
            return
        self._running = False

        logger.info("MeshHost stopping…")

        # Send GOODBYE to all connected peers
        try:
            await self.broadcast(MessageType.GOODBYE, {"reason": "node_shutdown"})
        except Exception:
            pass

        # Stop discovery
        if self._discovery:
            await self._discovery.stop()

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        # Close all connections
        for conn in list(self.connections.values()):
            await conn.close()
        self.connections.clear()

        # Close server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        logger.info("MeshHost stopped")

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_to(
        self,
        node_id: str,
        msg_type: MessageType,
        payload: dict,
    ) -> bool:
        """
        Send a signed message to a specific peer by node_id.

        Returns True if sent, False if peer not connected.
        """
        conn = self.connections.get(node_id)
        if not conn or not conn.is_alive:
            logger.debug("send_to %s: not connected", node_id[:16])
            return False

        envelope = MessageEnvelope.create(
            msg_type=msg_type,
            payload=payload,
            sender_id=self._identity.node_id,
            sender_pk_hex=self._identity.public_key_hex(),
            sign_fn=self._identity.sign,
        )
        try:
            await conn.send(envelope.to_bytes())
            return True
        except TransportError as exc:
            logger.warning("send_to %s failed: %s", node_id[:16], exc)
            await self._handle_disconnection(node_id)
            return False

    async def broadcast(
        self,
        msg_type: MessageType,
        payload: dict,
        exclude: Optional[Set[str]] = None,
    ) -> int:
        """
        Send a signed message to ALL connected peers.

        Parameters
        ----------
        msg_type  : message type
        payload   : message payload
        exclude   : optional set of node_ids to skip

        Returns
        -------
        int  — number of peers successfully sent to
        """
        if not self.connections:
            return 0

        envelope = MessageEnvelope.create(
            msg_type=msg_type,
            payload=payload,
            sender_id=self._identity.node_id,
            sender_pk_hex=self._identity.public_key_hex(),
            sign_fn=self._identity.sign,
        )
        envelope_bytes = envelope.to_bytes()
        exclude = exclude or set()
        sent = 0

        for node_id, conn in list(self.connections.items()):
            if node_id in exclude or not conn.is_alive:
                continue
            try:
                await conn.send(envelope_bytes)
                sent += 1
            except TransportError as exc:
                logger.debug("broadcast to %s failed: %s", node_id[:16], exc)
                await self._handle_disconnection(node_id)

        return sent

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def on_message(
        self,
        msg_type: MessageType,
        handler: MessageHandler,
    ) -> None:
        """
        Register a handler for a specific message type.

        Multiple handlers can be registered for the same type.
        They are called in registration order.

        Example
        -------
            mesh.on_message(MessageType.AGENT_MSG, my_agent_handler)
        """
        if msg_type not in self._handlers:
            self._handlers[msg_type] = []
        self._handlers[msg_type].append(handler)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a status dict for the Canvas API."""
        return {
            "running": self._running,
            "listen_addresses": self._config.network.listen_addresses,
            "peer_count": self.peer_store.count(),
            "connected_count": len(self.connections),
            "verified_count": len(self.peer_store.verified_peers()),
            "peers": [p.to_dict() for p in self.peer_store.connected_peers()],
        }

    # ------------------------------------------------------------------
    # Inbound connection handling
    # ------------------------------------------------------------------

    async def _handle_inbound(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """asyncio.start_server callback — handles one inbound TCP connection."""
        peer_addr = "{}:{}".format(*writer.get_extra_info("peername", ("?", 0)))

        # Enforce max_peers
        if len(self.connections) >= self._config.network.max_peers:
            logger.warning("Max peers reached — rejecting inbound from %s", peer_addr)
            writer.close()
            return

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as _K
            # Get private key from identity's KeyStore
            from neuralis.identity import KeyStore
            store = KeyStore(self._config.key_dir)
            private_key = store.load_private_key()

            conn = await accept(reader, writer, private_key, self._identity.node_id)
        except HandshakeError as exc:
            logger.warning("Inbound handshake failed from %s: %s", peer_addr, exc)
            writer.close()
            return
        except Exception as exc:
            logger.error("Inbound connection error from %s: %s", peer_addr, exc)
            writer.close()
            return

        await self._register_connection(conn, source="inbound")

    # ------------------------------------------------------------------
    # Outbound connection handling
    # ------------------------------------------------------------------

    def _on_peer_discovered(self, announcement: PeerAnnouncement) -> None:
        """Callback from DiscoveryEngine — schedule a dial attempt."""
        # Skip self
        if announcement.node_id == self._identity.node_id:
            return
        # Skip already connected
        if announcement.node_id in self.connections:
            return
        # Skip already known and connected peers
        existing = self.peer_store.get_by_node_id(announcement.node_id)
        if existing and existing.status == PeerStatus.VERIFIED:
            return
        # Schedule dial
        asyncio.create_task(self._dial_announcement(announcement))

    async def _dial_announcement(self, announcement: PeerAnnouncement) -> None:
        """Attempt to connect to a discovered peer."""
        node_id = announcement.node_id or "unknown"

        # Prevent duplicate dials
        if node_id in self._dialing:
            return
        self._dialing.add(node_id)

        try:
            for addr in announcement.addresses:
                host, port = _parse_host_port(addr)
                if not host or not port:
                    continue

                # Enforce max_peers
                if len(self.connections) >= self._config.network.max_peers:
                    logger.debug("Max peers reached — skipping dial to %s", addr)
                    return

                try:
                    from neuralis.identity import KeyStore
                    store = KeyStore(self._config.key_dir)
                    private_key = store.load_private_key()

                    conn = await dial(
                        host=host,
                        port=port,
                        local_private_key=private_key,
                        local_node_id=self._identity.node_id,
                        timeout=self._config.network.connection_timeout,
                    )
                    await self._register_connection(conn, source="outbound")
                    return   # success

                except (TransportError, HandshakeError, asyncio.TimeoutError) as exc:
                    logger.debug("Dial to %s failed: %s", addr, exc)
                    # Update peer store
                    if announcement.node_id:
                        peer = self.peer_store.get_by_node_id(announcement.node_id)
                        if peer:
                            peer.mark_failed()
        finally:
            self._dialing.discard(node_id)

    async def _register_connection(
        self,
        conn: PeerConnection,
        source: str,
    ) -> None:
        """Register a live connection, send our peer card, start recv loop."""
        node_id = conn.remote_node_id

        # Deduplicate — if we already have a connection, close the new one
        if node_id in self.connections and self.connections[node_id].is_alive:
            logger.debug("Duplicate connection from %s — closing new one", node_id[:16])
            await conn.close()
            return

        self.connections[node_id] = conn

        # Update peer store
        peer = PeerInfo(
            node_id=node_id,
            peer_id="",
            public_key_hex=conn.session.remote_public_key.hex(),
            addresses=[conn.peer_addr],
        )
        peer.mark_connected()
        self.peer_store.add_or_update(peer)

        logger.info("Peer connected (%s): %s", source, node_id[:20])

        # Send our peer card
        await self._send_peer_card(node_id)

        # Start receive loop
        task = asyncio.create_task(self._recv_loop(node_id))
        self._tasks.append(task)

    async def _send_peer_card(self, node_id: str) -> None:
        """Send our signed peer card to a newly connected peer."""
        card = self._identity.signed_peer_card()
        await self.send_to(node_id, MessageType.PEER_CARD, card)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self, node_id: str) -> None:
        """
        Per-peer receive loop.  Runs as an asyncio task for each connection.
        Decrypts incoming frames and dispatches to message handlers.
        """
        conn = self.connections.get(node_id)
        if not conn:
            return

        logger.debug("Recv loop started for %s", node_id[:16])

        while self._running and conn.is_alive:
            try:
                raw = await conn.recv()
            except TransportError as exc:
                logger.info("Peer %s disconnected: %s", node_id[:16], exc)
                break
            except Exception as exc:
                logger.error("Recv error from %s: %s", node_id[:16], exc)
                break

            try:
                envelope = MessageEnvelope.from_bytes(raw)
            except ValueError as exc:
                logger.warning("Malformed envelope from %s: %s", node_id[:16], exc)
                continue

            # Verify signature
            if not envelope.verify():
                logger.warning(
                    "SIGNATURE FAILURE from %s — banning peer", node_id[:16]
                )
                self.peer_store.ban(node_id)
                break

            # Drop expired messages
            if envelope.is_expired():
                logger.debug("Dropped expired message from %s", node_id[:16])
                continue

            # Dispatch to handlers
            await self._dispatch(envelope)

        await self._handle_disconnection(node_id)

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, envelope: MessageEnvelope) -> None:
        """Dispatch a verified envelope to registered handlers."""
        peer = self.peer_store.get_by_node_id(envelope.sender_id)
        handlers = self._handlers.get(envelope.type, [])

        for handler in handlers:
            try:
                result = handler(envelope, peer)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error(
                    "Handler error for %s from %s: %s",
                    envelope.type.value, envelope.sender_id[:16], exc
                )

    # ------------------------------------------------------------------
    # Default handlers
    # ------------------------------------------------------------------

    def _register_default_handlers(self) -> None:
        self.on_message(MessageType.PEER_CARD, self._handle_peer_card)
        self.on_message(MessageType.PEER_CARD_ACK, self._handle_peer_card_ack)
        self.on_message(MessageType.PING, self._handle_ping)
        self.on_message(MessageType.PONG, self._handle_pong)
        self.on_message(MessageType.PEER_LIST, self._handle_peer_list)
        self.on_message(MessageType.PEER_LIST_REQ, self._handle_peer_list_req)
        self.on_message(MessageType.GOODBYE, self._handle_goodbye)

    async def _handle_peer_card(
        self,
        envelope: MessageEnvelope,
        peer: Optional[PeerInfo],
    ) -> None:
        """Receive and verify a peer's identity card."""
        card = envelope.payload
        # Verify the card's self-signature
        import base64, json
        try:
            sig_bytes = base64.b64decode(card.get("signature", ""))
            payload_fields = {k: v for k, v in card.items() if k != "signature"}
            payload_bytes = json.dumps(
                payload_fields, sort_keys=True, separators=(",", ":")
            ).encode()
            pub_bytes = bytes.fromhex(card["public_key"])
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
            pub.verify(sig_bytes, payload_bytes)
        except Exception as exc:
            logger.warning("Peer card signature invalid from %s: %s",
                           envelope.sender_id[:16], exc)
            return

        # Update peer store
        updated_peer = PeerInfo.from_peer_card(card)
        updated_peer.mark_verified()
        self.peer_store.add_or_update(updated_peer)

        logger.info(
            "Peer verified: %s (alias=%s)",
            card["node_id"][:20], card.get("alias", "(none)")
        )

        # Acknowledge
        await self.send_to(
            envelope.sender_id,
            MessageType.PEER_CARD_ACK,
            {"node_id": self._identity.node_id},
        )

        # Share our peer list with the new verified peer
        await self.send_to(
            envelope.sender_id,
            MessageType.PEER_LIST,
            {"peers": [p.to_dict() for p in self.peer_store.verified_peers()
                       if p.node_id != envelope.sender_id]},
        )

    async def _handle_peer_card_ack(
        self,
        envelope: MessageEnvelope,
        peer: Optional[PeerInfo],
    ) -> None:
        """Remote peer acknowledged our card."""
        logger.debug("Peer card ACK from %s", envelope.sender_id[:16])

    async def _handle_ping(
        self,
        envelope: MessageEnvelope,
        peer: Optional[PeerInfo],
    ) -> None:
        """Respond to a PING with a PONG."""
        await self.send_to(
            envelope.sender_id,
            MessageType.PONG,
            {"echo_id": envelope.msg_id, "ts": time.time()},
        )

    async def _handle_pong(
        self,
        envelope: MessageEnvelope,
        peer: Optional[PeerInfo],
    ) -> None:
        """Receive a PONG — resolve the pending ping future."""
        echo_id = envelope.payload.get("echo_id")
        future = self._pending_pings.get(envelope.sender_id)
        if future and not future.done():
            future.set_result(time.time())

        # Update peer RTT
        if peer:
            sent_ts = envelope.payload.get("ts", time.time())
            peer.last_ping_ms = round((time.time() - sent_ts) * 1000, 2)
            peer.touch()

    async def _handle_peer_list(
        self,
        envelope: MessageEnvelope,
        peer: Optional[PeerInfo],
    ) -> None:
        """
        Receive a peer list from a connected peer — attempt new connections.
        This is how the mesh grows beyond direct discovery.
        """
        peers_data = envelope.payload.get("peers", [])
        for peer_dict in peers_data:
            node_id = peer_dict.get("node_id", "")
            if not node_id or node_id == self._identity.node_id:
                continue
            if node_id in self.connections:
                continue
            if len(self.connections) >= self._config.network.max_peers:
                break
            # Create announcement from gossip data
            announcement = PeerAnnouncement(
                source="gossip",
                node_id=node_id,
                peer_id=peer_dict.get("peer_id", ""),
                public_key=peer_dict.get("public_key", ""),
                addresses=peer_dict.get("addresses", []),
                alias=peer_dict.get("alias"),
            )
            asyncio.create_task(self._dial_announcement(announcement))

    async def _handle_peer_list_req(
        self,
        envelope: MessageEnvelope,
        peer: Optional[PeerInfo],
    ) -> None:
        """Respond to a peer list request."""
        await self.send_to(
            envelope.sender_id,
            MessageType.PEER_LIST,
            {"peers": [p.to_dict() for p in self.peer_store.verified_peers()
                       if p.node_id != envelope.sender_id]},
        )

    async def _handle_goodbye(
        self,
        envelope: MessageEnvelope,
        peer: Optional[PeerInfo],
    ) -> None:
        """Peer is cleanly disconnecting."""
        reason = envelope.payload.get("reason", "unknown")
        logger.info("Peer %s said goodbye (reason=%s)", envelope.sender_id[:16], reason)
        await self._handle_disconnection(envelope.sender_id)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """
        Periodic PING to all connected peers.
        Disconnects peers that don't respond within PING_TIMEOUT.
        """
        while self._running:
            await asyncio.sleep(PING_INTERVAL)
            if not self.connections:
                continue

            dead = []
            for node_id in list(self.connections.keys()):
                conn = self.connections.get(node_id)
                if not conn or not conn.is_alive:
                    dead.append(node_id)
                    continue

                # Create a future to await the PONG
                future: asyncio.Future = asyncio.get_event_loop().create_future()
                self._pending_pings[node_id] = future

                sent_ok = await self.send_to(
                    node_id, MessageType.PING,
                    {"ts": time.time()},
                )
                if not sent_ok:
                    dead.append(node_id)
                    continue

                try:
                    await asyncio.wait_for(future, timeout=PING_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("Peer %s timed out — disconnecting", node_id[:16])
                    dead.append(node_id)
                finally:
                    self._pending_pings.pop(node_id, None)

            for node_id in dead:
                await self._handle_disconnection(node_id)

    # ------------------------------------------------------------------
    # Disconnection handling
    # ------------------------------------------------------------------

    async def _handle_disconnection(self, node_id: str) -> None:
        """Clean up after a peer disconnects."""
        conn = self.connections.pop(node_id, None)
        if conn:
            await conn.close()

        peer = self.peer_store.get_by_node_id(node_id)
        if peer:
            peer.mark_disconnected()

        logger.info("Peer disconnected: %s", node_id[:16])

    def __repr__(self) -> str:
        return (
            f"<MeshHost running={self._running} "
            f"peers={len(self.connections)}/{self._config.network.max_peers}>"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_port_from_multiaddr(addr: str) -> int:
    """Extract port from a multiaddr like /ip4/0.0.0.0/tcp/7101."""
    parts = [p for p in addr.split("/") if p]
    try:
        tcp_idx = parts.index("tcp")
        return int(parts[tcp_idx + 1])
    except (ValueError, IndexError):
        return 7101


def _parse_host_port(addr: str) -> tuple[str, int]:
    """
    Parse a multiaddr or host:port string into (host, port).

    Supports:
        /ip4/1.2.3.4/tcp/7101
        /dns4/host.example.com/tcp/7101
        192.168.1.5:7101
    """
    addr = addr.strip()
    if addr.startswith("/"):
        parts = [p for p in addr.split("/") if p]
        try:
            if parts[0] in ("ip4", "dns4") and parts[2] == "tcp":
                return parts[1], int(parts[3])
        except (IndexError, ValueError):
            pass
        return "", 0
    else:
        # host:port fallback
        try:
            host, port_str = addr.rsplit(":", 1)
            return host, int(port_str)
        except (ValueError, AttributeError):
            return "", 0
