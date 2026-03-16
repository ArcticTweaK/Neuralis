"""
neuralis.node
=============
Node lifecycle management — the top-level entry point for a Neuralis node.

This module owns the boot sequence:

    1. Load / create NodeConfig
    2. Load / create NodeIdentity  (Ed25519 keypair)
    3. Configure logging
    4. Emit a signed boot record (written to disk + gossiped in later modules)
    5. Expose the running Node object to the rest of the stack

The Node object acts as the dependency-injection root — every other module
(mesh transport, IPFS store, agent runtime, Canvas API) receives a reference
to it and can pull config, identity, and shared state from a single place.

Usage
-----
    # Minimal — defaults everything
    from neuralis.node import Node
    node = Node.boot()
    print(node.identity)
    print(node.config)
    node.shutdown()

    # With custom config path and alias
    node = Node.boot(config_path=Path("/etc/neuralis/config.toml"), alias="gateway-1")
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from neuralis.config import NodeConfig, CONFIG_FILE
from neuralis.identity import NodeIdentity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NodeState — simple state machine
# ---------------------------------------------------------------------------

class NodeState:
    BOOTING = "BOOTING"
    RUNNING = "RUNNING"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# BootRecord — signed proof-of-boot written to disk
# ---------------------------------------------------------------------------

def _create_boot_record(identity: NodeIdentity, config: NodeConfig) -> dict:
    """
    Create a signed boot record.

    The boot record is written to ~/.neuralis/logs/boot.json and will be
    gossiped to peers in the mesh-transport module.  It proves that this
    node's private key was present at boot time (liveness proof).
    """
    record = {
        "event": "node_boot",
        "node_id": identity.node_id,
        "peer_id": identity.peer_id,
        "public_key": identity.public_key_hex(),
        "alias": identity.alias,
        "boot_time": time.time(),
        "listen_addresses": config.network.listen_addresses,
        "neuralis_version": "0.1.0",
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    import base64
    record["signature"] = base64.b64encode(identity.sign(payload)).decode()
    return record


# ---------------------------------------------------------------------------
# Node — the root object
# ---------------------------------------------------------------------------

class Node:
    """
    A running Neuralis node.

    Attributes
    ----------
    identity    : NodeIdentity — this node's Ed25519 identity
    config      : NodeConfig   — merged config (file + env overrides)
    state       : str          — current NodeState value
    boot_time   : float        — Unix timestamp of successful boot
    subsystems  : dict         — registered subsystem references
                                 (populated by later modules)

    Lifecycle hooks
    ---------------
    Subsystems (mesh, IPFS, agents, API) register themselves via
    node.register_subsystem(name, obj) and optional on_shutdown callbacks
    via node.on_shutdown(callback).  Node.shutdown() calls all callbacks
    in LIFO order.
    """

    def __init__(self, identity: NodeIdentity, config: NodeConfig):
        self.identity = identity
        self.config = config
        self.state = NodeState.BOOTING
        self.boot_time: float = 0.0
        self.subsystems: Dict[str, Any] = {}
        self._shutdown_callbacks: List[Callable] = []
        self._boot_record: Optional[dict] = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def boot(
        cls,
        config_path: Path = CONFIG_FILE,
        alias: Optional[str] = None,
    ) -> "Node":
        """
        Full synchronous boot sequence.

        Steps
        -----
        1. Load NodeConfig (file → env overrides)
        2. Configure Python logging
        3. Load or create NodeIdentity
        4. Apply alias if provided
        5. Create and persist a signed boot record
        6. Set state → RUNNING
        7. Install signal handlers (SIGINT / SIGTERM → graceful shutdown)

        Returns
        -------
        Node   — ready-to-use node instance
        """
        # ---- 1. Config ---------------------------------------------------
        config = NodeConfig.load(config_path)

        # ---- 2. Logging --------------------------------------------------
        _configure_logging(config)
        logger.info("=" * 60)
        logger.info("Neuralis Node — booting")
        logger.info("=" * 60)

        # ---- 3. Identity -------------------------------------------------
        key_dir = config.key_dir
        identity = NodeIdentity.load_or_create(key_dir=key_dir, alias=alias)

        # ---- 4. Alias override -------------------------------------------
        if alias and identity.alias != alias:
            identity.set_alias(alias, key_dir=key_dir)
            logger.info("Node alias set to: %s", alias)

        # ---- 5. Boot record ----------------------------------------------
        node = cls(identity=identity, config=config)
        node._boot_record = _create_boot_record(identity, config)
        node._persist_boot_record()

        # ---- 6. State transition -----------------------------------------
        node.boot_time = time.time()
        node.state = NodeState.RUNNING

        logger.info("Node ID   : %s", identity.node_id)
        logger.info("Peer ID   : %s", identity.peer_id)
        logger.info("Alias     : %s", identity.alias or "(none)")
        logger.info("API       : http://%s:%d", config.api.host, config.api.port)
        logger.info("Listen    : %s", config.network.listen_addresses)
        logger.info("mDNS      : %s | DHT: %s", config.network.enable_mdns, config.network.enable_dht)
        logger.info("State     : %s", node.state)
        logger.info("=" * 60)

        # ---- 7. Signal handlers -----------------------------------------
        _install_signal_handlers(node)

        return node

    @classmethod
    async def boot_async(
        cls,
        config_path: Path = CONFIG_FILE,
        alias: Optional[str] = None,
    ) -> "Node":
        """
        Async variant of boot() — for use inside an existing event loop.

        Identical logic to boot() but runs the boot sequence in the
        executor to avoid blocking the loop during key derivation.
        """
        loop = asyncio.get_event_loop()
        node = await loop.run_in_executor(None, lambda: cls.boot(config_path, alias))
        return node

    # ------------------------------------------------------------------
    # Subsystem registration
    # ------------------------------------------------------------------

    def register_subsystem(self, name: str, obj: Any) -> None:
        """
        Register a subsystem (mesh, ipfs, agents, api) with this node.

        Called by each subsystem module after initialisation:
            node.register_subsystem("mesh", mesh_host)
        """
        if name in self.subsystems:
            logger.warning("Subsystem '%s' already registered — overwriting", name)
        self.subsystems[name] = obj
        logger.debug("Subsystem registered: %s (%s)", name, type(obj).__name__)

    def get_subsystem(self, name: str) -> Any:
        """Retrieve a registered subsystem by name.  Raises KeyError if absent."""
        if name not in self.subsystems:
            raise KeyError(
                f"Subsystem '{name}' not registered.  "
                f"Available: {list(self.subsystems.keys())}"
            )
        return self.subsystems[name]

    def on_shutdown(self, callback: Callable) -> None:
        """
        Register a shutdown callback (LIFO order).

        Each subsystem registers its own teardown here:
            node.on_shutdown(mesh_host.stop)
        """
        self._shutdown_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """
        Graceful synchronous shutdown.

        Runs registered callbacks in reverse registration order (LIFO).
        """
        if self.state in (NodeState.SHUTTING_DOWN, NodeState.STOPPED):
            return

        self.state = NodeState.SHUTTING_DOWN
        logger.info("Node shutting down…")

        for callback in reversed(self._shutdown_callbacks):
            try:
                result = callback()
                # Support coroutine callbacks
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(result)
                        else:
                            loop.run_until_complete(result)
                    except RuntimeError:
                        asyncio.run(result)
            except Exception as exc:
                logger.error("Error during shutdown callback %s: %s", callback, exc)

        uptime = time.time() - self.boot_time if self.boot_time else 0
        logger.info("Node stopped. Uptime: %.1f seconds.", uptime)
        self.state = NodeState.STOPPED

    async def shutdown_async(self) -> None:
        """Async variant of shutdown()."""
        if self.state in (NodeState.SHUTTING_DOWN, NodeState.STOPPED):
            return

        self.state = NodeState.SHUTTING_DOWN
        logger.info("Node shutting down (async)…")

        for callback in reversed(self._shutdown_callbacks):
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error("Error during shutdown callback %s: %s", callback, exc)

        uptime = time.time() - self.boot_time if self.boot_time else 0
        logger.info("Node stopped. Uptime: %.1f seconds.", uptime)
        self.state = NodeState.STOPPED

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """
        Return a structured status dict — exposed by the Canvas API.

        Safe to serialise to JSON and send to the UI.
        """
        uptime = time.time() - self.boot_time if self.boot_time else 0
        return {
            "node_id": self.identity.node_id,
            "peer_id": self.identity.peer_id,
            "alias": self.identity.alias,
            "public_key": self.identity.public_key_hex(),
            "state": self.state,
            "boot_time": self.boot_time,
            "uptime_seconds": round(uptime, 1),
            "subsystems": list(self.subsystems.keys()),
            "listen_addresses": self.config.network.listen_addresses,
            "mdns_enabled": self.config.network.enable_mdns,
            "dht_enabled": self.config.network.enable_dht,
            "max_peers": self.config.network.max_peers,
            "telemetry_enabled": False,   # always false — zero telemetry
        }

    def __repr__(self) -> str:
        return (
            f"<Node {self.identity.node_id} "
            f"state={self.state} "
            f"uptime={round(time.time() - self.boot_time, 1) if self.boot_time else 0}s>"
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _persist_boot_record(self) -> None:
        """Write the signed boot record to ~/.neuralis/logs/boot.json."""
        if not self._boot_record:
            return
        log_dir = Path(self.config.logging.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        boot_path = log_dir / "boot.json"
        boot_path.write_text(json.dumps(self._boot_record, indent=2))
        logger.debug("Boot record written to %s", boot_path)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging(config: NodeConfig) -> None:
    """Configure Python's logging based on NodeConfig.logging."""
    log_cfg = config.logging
    level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    if log_cfg.enable_console:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        ))
        handlers.append(console)

    # Rotating file handler
    log_dir = Path(log_cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_dir / "neuralis.log",
            maxBytes=log_cfg.max_log_size_mb * 1024 * 1024,
            backupCount=log_cfg.backup_count,
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
        ))
        handlers.append(file_handler)
    except Exception as exc:
        print(f"[neuralis] Warning: could not create log file handler: {exc}")

    logging.basicConfig(level=level, handlers=handlers, force=True)
    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "asyncio", "multiaddr"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _install_signal_handlers(node: Node) -> None:
    """Install SIGINT / SIGTERM handlers for graceful shutdown."""
    def _handler(signum, frame):
        logger.info("Received signal %s — initiating shutdown", signum)
        node.shutdown()

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except (OSError, ValueError):
        # Can't install signal handlers in non-main threads — skip silently
        pass
