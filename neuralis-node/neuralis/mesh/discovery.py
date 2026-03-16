"""
neuralis.mesh.discovery
=======================
Peer discovery for the Neuralis mesh — mDNS (LAN) and static bootstrap (WAN).

Architecture
------------
Discovery is intentionally separated from transport.  The DiscoveryEngine
finds peer addresses and feeds them to a callback; the MeshHost decides
whether and how to connect.

Two discovery mechanisms are implemented:

1. mDNS (zero-config LAN discovery)
   Uses Python's socket multicast API to send/receive DNS-SD style probes
   on the link-local multicast group 224.0.0.251:5353.
   Neuralis uses the service type _neuralis._tcp.local.
   Peers respond with their node_id, peer_id, public key, and TCP address.
   No external libraries required — pure stdlib sockets.

2. Bootstrap peers (static WAN)
   A list of known multiaddrs from config.network.bootstrap_peers.
   Parsed and yielded once at startup.  The MeshHost handles retry logic.

Design notes
------------
- All discovery is PULL-based from the MeshHost's perspective: it calls
  DiscoveryEngine.start() and receives PeerAnnouncement objects via a
  callback registered at construction time.
- The mDNS socket is non-blocking and runs in a background asyncio task.
- No third-party discovery libraries are used — zero extra dependencies
  for this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# mDNS multicast group and port (RFC 6762)
MDNS_MULTICAST_ADDR = "224.0.0.251"
MDNS_PORT = 5353

# Neuralis service identifier embedded in mDNS probes
NEURALIS_SERVICE = "_neuralis._tcp.local"

# How often (seconds) to send an mDNS announce
ANNOUNCE_INTERVAL = 10.0

# Max age of a received mDNS announcement before we re-request
ANNOUNCEMENT_MAX_AGE = 30.0


# ---------------------------------------------------------------------------
# PeerAnnouncement — returned to the MeshHost
# ---------------------------------------------------------------------------

@dataclass
class PeerAnnouncement:
    """
    A discovered peer address, ready for the MeshHost to dial.

    source      : "mdns" | "bootstrap" | "gossip"
    node_id     : NRL1... identifier
    peer_id     : 12D3KooW... identifier
    public_key  : hex-encoded raw Ed25519 public key
    addresses   : list of multiaddr strings (e.g. /ip4/192.168.1.5/tcp/7101)
    alias       : optional human-readable name
    timestamp   : when this announcement was received
    """
    source: str
    node_id: str
    peer_id: str
    public_key: str
    addresses: List[str]
    alias: Optional[str] = None
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_peer_card(self) -> dict:
        return {
            "node_id": self.node_id,
            "peer_id": self.peer_id,
            "public_key": self.public_key,
            "addresses": self.addresses,
            "alias": self.alias,
        }


# ---------------------------------------------------------------------------
# mDNS probe wire format
# ---------------------------------------------------------------------------

def _build_mdns_probe(
    node_id: str,
    peer_id: str,
    public_key_hex: str,
    port: int,
    alias: Optional[str] = None,
) -> bytes:
    """
    Build a compact JSON-over-UDP mDNS probe.

    We do NOT implement the full DNS wire format (that's a 300-line parser).
    Instead we embed a compact JSON payload in the Additional section spirit —
    the probe is a single UDP datagram containing a JSON object that identifies
    the Neuralis service.  Any Neuralis node listening on the multicast group
    will parse it; non-Neuralis mDNS listeners will ignore it.

    Format:
        NEURALIS_MAGIC (4 bytes) + little-endian uint16 length + JSON payload
    """
    MAGIC = b"NRL\x01"
    payload = {
        "service": NEURALIS_SERVICE,
        "node_id": node_id,
        "peer_id": peer_id,
        "public_key": public_key_hex,
        "port": port,
        "alias": alias,
        "ts": time.time(),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    length = struct.pack("<H", len(body))
    return MAGIC + length + body


def _parse_mdns_probe(data: bytes) -> Optional[dict]:
    """
    Parse a Neuralis mDNS probe.  Returns the JSON dict or None on failure.
    """
    MAGIC = b"NRL\x01"
    if len(data) < 6 or data[:4] != MAGIC:
        return None
    length = struct.unpack("<H", data[4:6])[0]
    if len(data) < 6 + length:
        return None
    try:
        return json.loads(data[6:6 + length].decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# mDNS socket helpers
# ---------------------------------------------------------------------------

def _create_mdns_sender() -> socket.socket:
    """Create a UDP socket for sending mDNS multicast probes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass  # Not available on all platforms
    return sock


def _create_mdns_receiver() -> Optional[socket.socket]:
    """
    Create a UDP socket for receiving mDNS multicast probes.
    Returns None if multicast is not available on this system.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", MDNS_PORT))
        # Join the multicast group
        mreq = struct.pack(
            "4sL",
            socket.inet_aton(MDNS_MULTICAST_ADDR),
            socket.INADDR_ANY,
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        return sock
    except OSError as exc:
        logger.warning("mDNS receiver socket failed: %s — LAN discovery disabled", exc)
        return None


# ---------------------------------------------------------------------------
# DiscoveryEngine
# ---------------------------------------------------------------------------

class DiscoveryEngine:
    """
    Peer discovery engine for Neuralis mesh transport.

    Handles mDNS LAN discovery and static bootstrap peer resolution.
    Feeds discovered peers to the MeshHost via a callback.

    Usage
    -----
        def on_peer(announcement: PeerAnnouncement):
            asyncio.create_task(mesh_host.connect_peer(announcement))

        engine = DiscoveryEngine(
            node_id=identity.node_id,
            peer_id=identity.peer_id,
            public_key_hex=identity.public_key_hex(),
            listen_port=7101,
            bootstrap_peers=config.network.bootstrap_peers,
            enable_mdns=config.network.enable_mdns,
            on_peer_discovered=on_peer,
            mdns_interval=config.network.mdns_interval_secs,
            alias=identity.alias,
        )
        await engine.start()
        # ... later ...
        await engine.stop()
    """

    def __init__(
        self,
        node_id: str,
        peer_id: str,
        public_key_hex: str,
        listen_port: int,
        bootstrap_peers: List[str],
        enable_mdns: bool,
        on_peer_discovered: Callable[[PeerAnnouncement], None],
        mdns_interval: float = ANNOUNCE_INTERVAL,
        alias: Optional[str] = None,
    ):
        self.node_id = node_id
        self.peer_id = peer_id
        self.public_key_hex = public_key_hex
        self.listen_port = listen_port
        self.bootstrap_peers = bootstrap_peers
        self.enable_mdns = enable_mdns
        self.on_peer_discovered = on_peer_discovered
        self.mdns_interval = mdns_interval
        self.alias = alias

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._seen_node_ids: set[str] = set()   # deduplicate announcements
        self._sender_sock: Optional[socket.socket] = None
        self._receiver_sock: Optional[socket.socket] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all discovery mechanisms."""
        if self._running:
            return
        self._running = True
        logger.info("DiscoveryEngine starting (mDNS=%s bootstrap=%d peers)",
                    self.enable_mdns, len(self.bootstrap_peers))

        # Bootstrap peers — emit immediately
        if self.bootstrap_peers:
            self._tasks.append(
                asyncio.create_task(self._announce_bootstrap_peers())
            )

        # mDNS
        if self.enable_mdns:
            self._sender_sock = _create_mdns_sender()
            self._receiver_sock = _create_mdns_receiver()

            if self._receiver_sock:
                self._tasks.append(
                    asyncio.create_task(self._mdns_receive_loop())
                )
            self._tasks.append(
                asyncio.create_task(self._mdns_announce_loop())
            )

    async def stop(self) -> None:
        """Stop all discovery tasks and close sockets."""
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        for sock in (self._sender_sock, self._receiver_sock):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        self._sender_sock = None
        self._receiver_sock = None
        logger.info("DiscoveryEngine stopped")

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    async def _announce_bootstrap_peers(self) -> None:
        """
        Parse static bootstrap multiaddrs and emit PeerAnnouncement objects.

        Bootstrap entries are multiaddr strings:
            /ip4/1.2.3.4/tcp/7101/p2p/NRL1abc...
            /dns4/bootstrap.neuralis.local/tcp/7101/p2p/NRL1abc...

        We parse the /p2p/ component as the node_id.
        The public key is not known until handshake — we emit a minimal
        announcement and the MeshHost fills in the key after handshake.
        """
        for addr in self.bootstrap_peers:
            try:
                announcement = _parse_bootstrap_multiaddr(addr)
                if announcement:
                    logger.info("Bootstrap peer: %s @ %s",
                                announcement.node_id[:20], addr)
                    self._emit(announcement)
                else:
                    logger.warning("Could not parse bootstrap addr: %s", addr)
            except Exception as exc:
                logger.warning("Bootstrap addr error %s: %s", addr, exc)
            await asyncio.sleep(0)   # yield to event loop

    # ------------------------------------------------------------------
    # mDNS announce loop
    # ------------------------------------------------------------------

    async def _mdns_announce_loop(self) -> None:
        """
        Periodically broadcast our presence on the LAN multicast group.

        Sends a Neuralis mDNS probe every mdns_interval seconds.
        The first probe is sent immediately.
        """
        probe = _build_mdns_probe(
            node_id=self.node_id,
            peer_id=self.peer_id,
            public_key_hex=self.public_key_hex,
            port=self.listen_port,
            alias=self.alias,
        )
        while self._running:
            try:
                if self._sender_sock:
                    self._sender_sock.sendto(probe, (MDNS_MULTICAST_ADDR, MDNS_PORT))
                    logger.debug("mDNS probe sent (%d bytes)", len(probe))
            except OSError as exc:
                logger.debug("mDNS send failed: %s", exc)
            await asyncio.sleep(self.mdns_interval)

    # ------------------------------------------------------------------
    # mDNS receive loop
    # ------------------------------------------------------------------

    async def _mdns_receive_loop(self) -> None:
        """
        Listen for mDNS probes from other Neuralis nodes on the LAN.

        Runs as a background task, yielding to the event loop between reads.
        Uses non-blocking socket + asyncio.sleep(0) instead of
        add_reader() for maximum portability across platforms.
        """
        loop = asyncio.get_event_loop()
        logger.debug("mDNS receive loop started on %s:%d",
                     MDNS_MULTICAST_ADDR, MDNS_PORT)

        while self._running:
            try:
                # Non-blocking recv — socket is set to non-blocking at creation
                try:
                    data, addr = self._receiver_sock.recvfrom(4096)
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                    continue

                parsed = _parse_mdns_probe(data)
                if parsed is None:
                    await asyncio.sleep(0)
                    continue

                # Ignore our own probes
                if parsed.get("node_id") == self.node_id:
                    await asyncio.sleep(0)
                    continue

                # Validate required fields
                required = {"node_id", "peer_id", "public_key", "port"}
                if not required.issubset(parsed.keys()):
                    await asyncio.sleep(0)
                    continue

                sender_ip = addr[0]
                port = parsed["port"]
                node_id = parsed["node_id"]

                # Rate-limit: don't re-emit the same peer within 10s
                if node_id in self._seen_node_ids:
                    await asyncio.sleep(0)
                    continue
                self._seen_node_ids.add(node_id)
                # Clear the seen set periodically (simple approach)
                loop.call_later(ANNOUNCEMENT_MAX_AGE,
                                self._seen_node_ids.discard, node_id)

                announcement = PeerAnnouncement(
                    source="mdns",
                    node_id=node_id,
                    peer_id=parsed["peer_id"],
                    public_key=parsed["public_key"],
                    addresses=[f"/ip4/{sender_ip}/tcp/{port}"],
                    alias=parsed.get("alias"),
                )
                logger.info("mDNS: discovered peer %s @ %s:%d",
                            node_id[:20], sender_ip, port)
                self._emit(announcement)

            except Exception as exc:
                logger.debug("mDNS receive error: %s", exc)
                await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, announcement: PeerAnnouncement) -> None:
        """Deliver a PeerAnnouncement to the registered callback."""
        try:
            self.on_peer_discovered(announcement)
        except Exception as exc:
            logger.error("on_peer_discovered callback raised: %s", exc)

    def announce_now(self) -> None:
        """
        Manually trigger an immediate mDNS probe (e.g. after port change).
        Fire-and-forget — does not wait for delivery.
        """
        if self._sender_sock and self.enable_mdns:
            probe = _build_mdns_probe(
                self.node_id, self.peer_id,
                self.public_key_hex, self.listen_port, self.alias,
            )
            try:
                self._sender_sock.sendto(probe, (MDNS_MULTICAST_ADDR, MDNS_PORT))
            except OSError:
                pass

    def __repr__(self) -> str:
        return (
            f"<DiscoveryEngine mdns={self.enable_mdns} "
            f"bootstrap={len(self.bootstrap_peers)} "
            f"running={self._running}>"
        )


# ---------------------------------------------------------------------------
# Bootstrap multiaddr parser
# ---------------------------------------------------------------------------

def _parse_bootstrap_multiaddr(addr: str) -> Optional[PeerAnnouncement]:
    """
    Parse a bootstrap multiaddr string into a PeerAnnouncement.

    Supported formats:
        /ip4/1.2.3.4/tcp/7101/p2p/NRL1abc...
        /ip4/1.2.3.4/tcp/7101
        /dns4/host.example.com/tcp/7101/p2p/NRL1abc...

    The /p2p/ component, if present, is used as the node_id.
    If absent, a placeholder announcement is returned with an empty node_id
    and the MeshHost will fill in the identity after handshake.
    """
    parts = [p for p in addr.split("/") if p]
    if len(parts) < 4:
        return None

    # Extract IP or DNS
    if parts[0] == "ip4":
        try:
            ipaddress.ip_address(parts[1])
            host = parts[1]
        except ValueError:
            return None
    elif parts[0] == "dns4":
        host = parts[1]
    else:
        return None

    # Extract port
    if parts[2] != "tcp":
        return None
    try:
        port = int(parts[3])
    except ValueError:
        return None

    # Extract optional p2p node_id
    node_id = ""
    if len(parts) >= 6 and parts[4] == "p2p":
        node_id = parts[5]

    clean_addr = f"/ip4/{host}/tcp/{port}" if parts[0] == "ip4" else f"/dns4/{host}/tcp/{port}"

    return PeerAnnouncement(
        source="bootstrap",
        node_id=node_id,
        peer_id="",
        public_key="",
        addresses=[clean_addr],
        alias=None,
    )
