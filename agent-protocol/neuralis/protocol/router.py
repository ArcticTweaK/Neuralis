"""
neuralis.protocol.router
========================
Task routing for the Neuralis agent protocol.

The ProtocolRouter is the Module 5 brain.  It sits above the mesh transport
(Module 2) and the agent runtime (Module 4) and is responsible for:

  1. Maintaining a remote capability table — which nodes can handle which tasks
  2. Routing outbound TASK_REQUESTs to the best available remote node
  3. Tracking pending requests and matching responses to their waiters
  4. Dispatching inbound ProtocolMessages to registered local handlers
  5. Enforcing per-request timeouts

Routing algorithm
-----------------
When ``route_task(task, payload, ...)`` is called:

  1. Check if any *remote* node advertises the task capability.
  2. If multiple candidates exist, prefer nodes with lower average latency
     (last_ping_ms from the mesh PeerStore), falling back to round-robin.
  3. If no remote capable node is found, raise ``NoRouteError``.
  4. Send a TASK_REQUEST envelope via the mesh and register a pending waiter.
  5. Caller awaits the Future; ProtocolRouter resolves it when the matching
     TASK_RESPONSE / TASK_ERROR arrives.

The router does NOT do local dispatch — that is the AgentRuntime's job
(Module 4).  Local tasks go directly through the bus.  Remote tasks go
through the router.

Integration
-----------
    proto = ProtocolRouter(node, mesh_host, agent_runtime)
    await proto.start()

    # Send a task to a remote agent and await the response
    response = await proto.route_task("summarise", {"text": "..."})

    # Inbound messages are handled automatically via mesh.on_message()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from neuralis.protocol.messages import (
    AgentCapability,
    ProtocolError,
    ProtocolMessage,
    ProtocolMessageType,
)

logger = logging.getLogger(__name__)

# Default timeout waiting for a remote task response (seconds)
DEFAULT_ROUTE_TIMEOUT: float = 30.0

# Maximum pending requests tracked at once
MAX_PENDING: int = 256


# ---------------------------------------------------------------------------
# NoRouteError
# ---------------------------------------------------------------------------

class NoRouteError(Exception):
    """Raised when no remote node is known to handle the requested task."""


# ---------------------------------------------------------------------------
# RemoteNodeInfo — capability record for one remote node
# ---------------------------------------------------------------------------

@dataclass
class RemoteNodeInfo:
    """
    What the local node knows about a remote node's agent capabilities.

    Updated whenever an AGENT_ANNOUNCE or CAPABILITY_REPLY arrives.

    Attributes
    ----------
    node_id       : NRL1... identifier
    capabilities  : dict mapping agent_name → AgentCapability
    last_seen     : unix timestamp of the most recent announce / reply
    """
    node_id:      str
    capabilities: Dict[str, AgentCapability] = field(default_factory=dict)
    last_seen:    float                      = field(default_factory=time.time)

    # --- helpers ---

    def tasks(self) -> Set[str]:
        """Return the flat set of all tasks this node can handle."""
        result: Set[str] = set()
        for cap in self.capabilities.values():
            result.update(cap.tasks)
        return result

    def can_handle(self, task: str) -> bool:
        return task in self.tasks()

    def update(self, caps: List[AgentCapability]) -> None:
        """Replace capability list and refresh last_seen."""
        self.capabilities = {c.agent_name: c for c in caps}
        self.last_seen = time.time()

    def withdraw(self, agent_names: List[str]) -> None:
        """Remove named agents from the capability table."""
        for name in agent_names:
            self.capabilities.pop(name, None)
        self.last_seen = time.time()

    def is_stale(self, max_age: float = 300.0) -> bool:
        """True if we haven't heard from this node in max_age seconds."""
        return (time.time() - self.last_seen) > max_age

    def __repr__(self) -> str:
        return (
            f"<RemoteNodeInfo node={self.node_id[:12]} "
            f"agents={list(self.capabilities.keys())} "
            f"tasks={self.tasks()}>"
        )


# ---------------------------------------------------------------------------
# PendingRequest — an in-flight TASK_REQUEST waiting for a response
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    """
    Tracks a single outbound TASK_REQUEST and its response waiter.

    Attributes
    ----------
    msg_id      : the msg_id of the outgoing TASK_REQUEST
    session_id  : session grouping this request and its response
    task        : task name
    dst_node    : node we sent to
    future      : asyncio Future resolved when the response arrives
    created_at  : unix timestamp (for timeout eviction)
    """
    msg_id:     str
    session_id: str
    task:       str
    dst_node:   str
    future:     asyncio.Future
    created_at: float = field(default_factory=time.time)

    def is_expired(self, timeout: float = DEFAULT_ROUTE_TIMEOUT) -> bool:
        return (time.time() - self.created_at) > timeout


# ---------------------------------------------------------------------------
# ProtocolRouter
# ---------------------------------------------------------------------------

class ProtocolRouter:
    """
    Routes inter-node agent tasks across the Neuralis mesh.

    Parameters
    ----------
    node          : neuralis.node.Node — the running local node
    mesh          : MeshHost           — Module 2 mesh host (for send_to/broadcast)
    agent_runtime : AgentRuntime       — Module 4 runtime (for local capability export)
    timeout       : default request timeout in seconds
    """

    def __init__(
        self,
        node,
        mesh,
        agent_runtime=None,
        timeout: float = DEFAULT_ROUTE_TIMEOUT,
    ) -> None:
        self._node    = node
        self._mesh    = mesh
        self._runtime = agent_runtime
        self._timeout = timeout

        # node_id → RemoteNodeInfo
        self._remote_nodes: Dict[str, RemoteNodeInfo] = {}

        # msg_id → PendingRequest
        self._pending: Dict[str, PendingRequest] = {}

        # Registered inbound handlers: msg_type → list[coroutine fn]
        self._handlers: Dict[ProtocolMessageType, list] = {}

        self._running    = False
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start the protocol router.

        Registers an AGENT_MSG handler on the mesh host and announces
        local agent capabilities to the mesh.
        """
        if self._running:
            return
        self._running = True

        # Hook into the mesh: every AGENT_MSG is funnelled through us
        from neuralis.mesh.peers import MessageType as MeshMsgType
        self._mesh.on_message(MeshMsgType.AGENT_MSG, self._on_mesh_agent_msg)

        # Background: evict timed-out pending requests periodically
        self._tasks.append(
            asyncio.create_task(self._eviction_loop(), name="proto-eviction")
        )

        # Announce local capabilities to any already-connected peers
        await self._announce_capabilities()

        self._node.register_subsystem("protocol", self)
        self._node.on_shutdown(self.stop)

        logger.info(
            "ProtocolRouter started | node=%s",
            self._node.identity.node_id[:20],
        )

    async def stop(self) -> None:
        """Gracefully stop the router and cancel pending requests."""
        if not self._running:
            return
        self._running = False

        # Cancel all pending waiters
        for req in list(self._pending.values()):
            if not req.future.done():
                req.future.cancel()
        self._pending.clear()

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        logger.info("ProtocolRouter stopped")

    # ------------------------------------------------------------------
    # Outbound routing
    # ------------------------------------------------------------------

    async def route_task(
        self,
        task:      str,
        payload:   Dict[str, Any],
        dst_node:  str = "",
        src_agent: str = "",
        dst_agent: str = "",
        timeout:   Optional[float] = None,
    ) -> ProtocolMessage:
        """
        Send a TASK_REQUEST to a remote node and await the response.

        Parameters
        ----------
        task      : task identifier (e.g. "summarise")
        payload   : task-specific data dict
        dst_node  : target node_id; if empty, auto-selects best candidate
        src_agent : name of the local sending agent (optional)
        dst_agent : name of the target agent (optional)
        timeout   : seconds to wait; defaults to self._timeout

        Returns
        -------
        ProtocolMessage  — the TASK_RESPONSE message

        Raises
        ------
        NoRouteError     — no capable remote node found
        asyncio.TimeoutError — remote node did not respond in time
        ProtocolError    — remote returned TASK_ERROR
        """
        if not self._running:
            raise RuntimeError("ProtocolRouter is not running")

        if not dst_node:
            dst_node = self._select_node_for_task(task)

        timeout = timeout if timeout is not None else self._timeout
        my_node = self._node.identity.node_id

        msg = ProtocolMessage.task_request(
            src_node  = my_node,
            dst_node  = dst_node,
            task      = task,
            payload   = payload,
            src_agent = src_agent,
            dst_agent = dst_agent,
        )

        # Register waiter before sending to avoid race
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        pending = PendingRequest(
            msg_id     = msg.msg_id,
            session_id = msg.session_id,
            task       = task,
            dst_node   = dst_node,
            future     = future,
        )

        if len(self._pending) >= MAX_PENDING:
            raise ProtocolError("Too many pending requests")
        self._pending[msg.msg_id] = pending

        # Send via mesh
        from neuralis.mesh.peers import MessageType as MeshMsgType
        sent = await self._mesh.send_to(dst_node, MeshMsgType.AGENT_MSG, msg.to_dict())
        if not sent:
            del self._pending[msg.msg_id]
            raise NoRouteError(
                f"Could not send to {dst_node[:16]} — peer not connected"
            )

        logger.debug(
            "route_task %r → %s (msg_id=%s)",
            task, dst_node[:16], msg.msg_id[:8],
        )

        try:
            response: ProtocolMessage = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg.msg_id, None)
            raise asyncio.TimeoutError(
                f"Task {task!r} timed out after {timeout}s"
            )

        if response.msg_type == ProtocolMessageType.TASK_ERROR:
            error_text = response.payload.get("error", "unknown error")
            raise ProtocolError(f"Remote task error: {error_text}")

        return response

    async def broadcast_task(
        self,
        task:    str,
        payload: Dict[str, Any],
    ) -> int:
        """
        Broadcast a TASK_REQUEST to ALL connected peers.

        This is fire-and-forget — no response is tracked.
        Returns the number of peers the message was sent to.
        """
        my_node = self._node.identity.node_id
        msg = ProtocolMessage.task_request(
            src_node = my_node,
            dst_node = "",
            task     = task,
            payload  = payload,
        )
        from neuralis.mesh.peers import MessageType as MeshMsgType
        return await self._mesh.broadcast(MeshMsgType.AGENT_MSG, msg.to_dict())

    # ------------------------------------------------------------------
    # Capability management
    # ------------------------------------------------------------------

    async def query_capabilities(self, dst_node: str = "") -> None:
        """
        Send a CAPABILITY_QUERY to a specific node or broadcast to all.

        Responses are processed automatically by _on_mesh_agent_msg.
        """
        msg = ProtocolMessage.capability_query(
            src_node = self._node.identity.node_id,
            dst_node = dst_node,
        )
        from neuralis.mesh.peers import MessageType as MeshMsgType
        if dst_node:
            await self._mesh.send_to(dst_node, MeshMsgType.AGENT_MSG, msg.to_dict())
        else:
            await self._mesh.broadcast(MeshMsgType.AGENT_MSG, msg.to_dict())

    def nodes_for_task(self, task: str) -> List[RemoteNodeInfo]:
        """Return all remote nodes known to handle the given task."""
        return [
            info for info in self._remote_nodes.values()
            if info.can_handle(task)
        ]

    def all_remote_nodes(self) -> List[RemoteNodeInfo]:
        return list(self._remote_nodes.values())

    def get_remote_node(self, node_id: str) -> Optional[RemoteNodeInfo]:
        return self._remote_nodes.get(node_id)

    # ------------------------------------------------------------------
    # Inbound handler registration
    # ------------------------------------------------------------------

    def on_protocol_message(
        self,
        msg_type: ProtocolMessageType,
        handler,
    ) -> None:
        """
        Register a handler for a specific ProtocolMessageType.

        The handler signature is::

            async def handler(msg: ProtocolMessage) -> None

        Multiple handlers can be registered for the same type;
        they are called in registration order.
        """
        if msg_type not in self._handlers:
            self._handlers[msg_type] = []
        self._handlers[msg_type].append(handler)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "running":          self._running,
            "remote_nodes":     len(self._remote_nodes),
            "pending_requests": len(self._pending),
            "nodes": [
                {
                    "node_id":  info.node_id,
                    "agents":   list(info.capabilities.keys()),
                    "tasks":    list(info.tasks()),
                    "last_seen": info.last_seen,
                }
                for info in self._remote_nodes.values()
            ],
        }

    def __repr__(self) -> str:
        return (
            f"<ProtocolRouter running={self._running} "
            f"remote_nodes={len(self._remote_nodes)} "
            f"pending={len(self._pending)}>"
        )

    # ------------------------------------------------------------------
    # Internal — inbound message dispatch
    # ------------------------------------------------------------------

    async def _on_mesh_agent_msg(self, envelope, peer_info) -> None:
        """
        Called by MeshHost for every AGENT_MSG envelope received.

        Parses the ProtocolMessage from the envelope payload,
        then dispatches to the appropriate internal handler.
        """
        try:
            msg = ProtocolMessage.from_dict(envelope.payload)
        except ProtocolError as exc:
            logger.warning("Unparseable AGENT_MSG from %s: %s", envelope.sender_id[:12], exc)
            return

        if msg.is_expired():
            logger.debug("Dropping expired %s from %s", msg.msg_type, msg.src_node[:12])
            return

        # Drop messages not addressed to us (unless broadcast)
        my_node = self._node.identity.node_id
        if msg.dst_node and msg.dst_node != my_node:
            logger.debug("Dropping %s not addressed to us", msg.msg_type)
            return

        logger.debug(
            "Inbound %s from %s task=%r",
            msg.msg_type.value, msg.src_node[:12], msg.task,
        )

        # Route to correct internal handler
        dispatch = {
            ProtocolMessageType.TASK_REQUEST:     self._handle_task_request,
            ProtocolMessageType.TASK_RESPONSE:    self._handle_task_response,
            ProtocolMessageType.TASK_ERROR:       self._handle_task_response,
            ProtocolMessageType.CAPABILITY_QUERY: self._handle_capability_query,
            ProtocolMessageType.CAPABILITY_REPLY: self._handle_capability_reply,
            ProtocolMessageType.AGENT_ANNOUNCE:   self._handle_agent_announce,
            ProtocolMessageType.AGENT_WITHDRAW:   self._handle_agent_withdraw,
            ProtocolMessageType.HEARTBEAT:        self._handle_heartbeat,
        }
        handler = dispatch.get(msg.msg_type)
        if handler:
            try:
                await handler(msg)
            except Exception as exc:
                logger.error("Error handling %s: %s", msg.msg_type, exc)

        # Call any externally-registered handlers too
        for ext_handler in self._handlers.get(msg.msg_type, []):
            try:
                await ext_handler(msg)
            except Exception as exc:
                logger.error("External handler error for %s: %s", msg.msg_type, exc)

    async def _handle_task_request(self, msg: ProtocolMessage) -> None:
        """
        A remote node wants us to run a task locally.

        Dispatch to the local AgentRuntime if available;
        otherwise send back a TASK_ERROR.
        """
        if self._runtime is None:
            await self._send_error(msg, "No agent runtime available")
            return

        from neuralis.agents.base import AgentMessage
        agent_msg = AgentMessage(
            target    = msg.dst_agent or msg.task,
            task      = msg.task,
            payload   = msg.payload,
            sender_id = msg.src_node,
            reply_to  = msg.msg_id,
        )

        try:
            responses = await self._runtime.dispatch(agent_msg)
        except Exception as exc:
            await self._send_error(msg, str(exc))
            return

        # Find the first non-None AgentResponse
        result = None
        if responses:
            result = responses[0]

        if result is None:
            await self._send_error(msg, f"No agent handled task {msg.task!r}")
            return

        reply = msg.make_reply(
            msg_type  = ProtocolMessageType.TASK_RESPONSE,
            payload   = result.data if hasattr(result, "data") else {"result": str(result)},
            src_node  = self._node.identity.node_id,
            src_agent = msg.dst_agent,
        )

        from neuralis.mesh.peers import MessageType as MeshMsgType
        await self._mesh.send_to(msg.src_node, MeshMsgType.AGENT_MSG, reply.to_dict())

    async def _handle_task_response(self, msg: ProtocolMessage) -> None:
        """A remote node responded to our TASK_REQUEST — resolve the waiter."""
        pending = self._pending.pop(msg.reply_to, None)
        if pending is None:
            # Could be a duplicate or very late response
            logger.debug(
                "No pending request for reply_to=%s", msg.reply_to[:8] if msg.reply_to else "?"
            )
            return
        if not pending.future.done():
            pending.future.set_result(msg)

    async def _handle_capability_query(self, msg: ProtocolMessage) -> None:
        """A peer asked what we can do — reply with our agent list."""
        caps = self._local_capabilities()
        reply = ProtocolMessage.capability_reply(
            src_node     = self._node.identity.node_id,
            dst_node     = msg.src_node,
            reply_to     = msg.msg_id,
            capabilities = caps,
        )
        from neuralis.mesh.peers import MessageType as MeshMsgType
        await self._mesh.send_to(msg.src_node, MeshMsgType.AGENT_MSG, reply.to_dict())

    async def _handle_capability_reply(self, msg: ProtocolMessage) -> None:
        """A peer told us its capabilities — update the remote node table."""
        caps_raw = msg.payload.get("capabilities", [])
        caps = [AgentCapability.from_dict(c) for c in caps_raw]
        self._upsert_remote(msg.src_node, caps)
        logger.debug(
            "Updated capabilities for %s: %d agents",
            msg.src_node[:12], len(caps),
        )

    async def _handle_agent_announce(self, msg: ProtocolMessage) -> None:
        """A new peer announced its agents — same as capability reply."""
        await self._handle_capability_reply(msg)

    async def _handle_agent_withdraw(self, msg: ProtocolMessage) -> None:
        """A peer removed some agents from service."""
        names = msg.payload.get("agents", [])
        info = self._remote_nodes.get(msg.src_node)
        if info:
            info.withdraw(names)
            logger.debug(
                "Withdrew agents %s from %s", names, msg.src_node[:12]
            )

    async def _handle_heartbeat(self, msg: ProtocolMessage) -> None:
        """Update last_seen for the sending node."""
        info = self._remote_nodes.get(msg.src_node)
        if info:
            info.last_seen = time.time()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _select_node_for_task(self, task: str) -> str:
        """
        Pick the best remote node to handle *task*.

        Selection: lowest last_ping_ms (if available), otherwise first found.
        Raises NoRouteError if no capable node is known.
        """
        candidates = self.nodes_for_task(task)
        if not candidates:
            raise NoRouteError(
                f"No remote node known to handle task {task!r}"
            )

        # Try to pick best by mesh ping latency
        mesh_store = getattr(getattr(self._mesh, "peer_store", None), "get_by_node_id", None)
        if mesh_store:
            def _latency(info: RemoteNodeInfo) -> float:
                peer = mesh_store(info.node_id)
                if peer and peer.last_ping_ms is not None:
                    return peer.last_ping_ms
                return float("inf")
            candidates.sort(key=_latency)

        return candidates[0].node_id

    def _upsert_remote(self, node_id: str, caps: List[AgentCapability]) -> None:
        """Insert or update a remote node's capability record."""
        if node_id in self._remote_nodes:
            self._remote_nodes[node_id].update(caps)
        else:
            self._remote_nodes[node_id] = RemoteNodeInfo(
                node_id      = node_id,
                capabilities = {c.agent_name: c for c in caps},
            )

    def _local_capabilities(self) -> List[AgentCapability]:
        """Build an AgentCapability list from the local AgentRuntime."""
        if self._runtime is None:
            return []
        caps = []
        for agent in self._runtime.all_agents():
            meta = agent.meta
            caps.append(AgentCapability(
                agent_name     = meta.name,
                version        = meta.version,
                tasks          = list(meta.capabilities),
                required_model = meta.required_model,
            ))
        return caps

    async def _announce_capabilities(self) -> None:
        """Broadcast local agent capabilities to all connected peers."""
        caps = self._local_capabilities()
        if not caps:
            return
        msg = ProtocolMessage.agent_announce(
            src_node     = self._node.identity.node_id,
            capabilities = caps,
        )
        from neuralis.mesh.peers import MessageType as MeshMsgType
        sent = await self._mesh.broadcast(MeshMsgType.AGENT_MSG, msg.to_dict())
        logger.debug("Announced %d capabilities to %d peers", len(caps), sent)

    async def _eviction_loop(self) -> None:
        """
        Background coroutine: cancel timed-out pending requests every 5s.
        Also evicts stale remote node records (no announce in 5 min).
        """
        while self._running:
            await asyncio.sleep(5.0)
            now = time.time()

            # Evict timed-out pending requests
            expired_ids = [
                mid for mid, req in self._pending.items()
                if req.is_expired(self._timeout)
            ]
            for mid in expired_ids:
                req = self._pending.pop(mid, None)
                if req and not req.future.done():
                    req.future.set_exception(
                        asyncio.TimeoutError(
                            f"Task {req.task!r} timed out (evicted)"
                        )
                    )

            # Evict stale remote nodes (5-minute silence)
            stale = [
                nid for nid, info in self._remote_nodes.items()
                if info.is_stale(300.0)
            ]
            for nid in stale:
                del self._remote_nodes[nid]
                logger.debug("Evicted stale remote node %s", nid[:12])

    async def _send_error(self, original: ProtocolMessage, error: str) -> None:
        """Send a TASK_ERROR back to the originator of a request."""
        reply = ProtocolMessage.task_error(
            src_node   = self._node.identity.node_id,
            dst_node   = original.src_node,
            reply_to   = original.msg_id,
            session_id = original.session_id,
            task       = original.task,
            error      = error,
            src_agent  = original.dst_agent,
        )
        from neuralis.mesh.peers import MessageType as MeshMsgType
        await self._mesh.send_to(
            original.src_node, MeshMsgType.AGENT_MSG, reply.to_dict()
        )
