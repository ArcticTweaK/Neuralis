"""
neuralis.api.routes
===================
FastAPI route handlers for the Canvas API.

Organised into four APIRouter groups:
  node_router     — /api/node/*      node identity and status
  peer_router     — /api/peers/*     mesh peer management
  content_router  — /api/content/*   IPFS content store
  agent_router    — /api/agents/*    local agent runtime
  protocol_router — /api/protocol/*  remote node / task routing

Each router is registered on the main FastAPI app in app.py.

All handlers access subsystems through the Node object injected via
FastAPI's dependency system (``Depends(get_node)``).  The node is the
single source of truth — no global state in this module.

Error handling
--------------
All handlers catch subsystem exceptions and re-raise as HTTPException
so the UI always receives a consistent JSON error shape.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from neuralis.api.models import (
    AgentListResponse,
    AgentReloadResponse,
    AgentResponse,
    ContentAddRequest,
    ContentAddResponse,
    ContentGetResponse,
    ErrorResponse,
    NodeAliasRequest,
    NodeAliasResponse,
    NodeStatusResponse,
    OkResponse,
    PeerConnectRequest,
    PeerConnectResponse,
    PeerDisconnectResponse,
    PeerListResponse,
    PeerResponse,
    PinListResponse,
    PinRequest,
    PinResponse,
    RemoteNodeListResponse,
    RemoteNodeResponse,
    RemoteTaskRequest,
    RemoteTaskResponse,
    StorageStatsResponse,
    TaskRequest,
    TaskResponse,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency — node is injected via app.state at startup
# ---------------------------------------------------------------------------

def get_node(request: Request):
    """FastAPI dependency: extract the Node from app.state."""
    node = getattr(request.app.state, "node", None)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Node not initialised",
        )
    return node


# ===========================================================================
# Node router  —  /api/node
# ===========================================================================

node_router = APIRouter(prefix="/api/node", tags=["node"])


@node_router.get("/status", response_model=NodeStatusResponse)
async def get_node_status(node=Depends(get_node)):
    """Return the full node status including identity and subsystem list."""
    try:
        return NodeStatusResponse(**node.status())
    except Exception as exc:
        logger.error("get_node_status error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@node_router.patch("/alias", response_model=NodeAliasResponse)
async def set_node_alias(body: NodeAliasRequest, node=Depends(get_node)):
    """Update the node's human-readable alias."""
    try:
        node.identity.set_alias(body.alias, key_dir=node.config.key_dir)
        return NodeAliasResponse(alias=body.alias, node_id=node.identity.node_id)
    except Exception as exc:
        logger.error("set_node_alias error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@node_router.post("/shutdown", response_model=OkResponse)
async def shutdown_node(node=Depends(get_node)):
    """Initiate graceful node shutdown."""
    import asyncio
    asyncio.create_task(node.shutdown_async())
    return OkResponse(message="Shutdown initiated")


# ===========================================================================
# Peer router  —  /api/peers
# ===========================================================================

peer_router = APIRouter(prefix="/api/peers", tags=["peers"])


def _mesh(node):
    """Get the mesh subsystem or raise 503."""
    mesh = node.subsystems.get("mesh")
    if mesh is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mesh subsystem not running",
        )
    return mesh


@peer_router.get("", response_model=PeerListResponse)
async def list_peers(node=Depends(get_node)):
    """Return all known peers and their connection status."""
    mesh = _mesh(node)
    peers = [
        PeerResponse(
            node_id        = p.node_id,
            peer_id        = p.peer_id,
            alias          = p.alias,
            status         = p.status.value if hasattr(p.status, "value") else str(p.status),
            addresses      = list(p.addresses),
            last_seen      = p.last_seen,
            last_ping_ms   = p.last_ping_ms,
            failed_attempts = p.failed_attempts,
        )
        for p in mesh.peer_store.all_peers()
    ]
    connected = sum(
        1 for p in peers
        if p.status in ("CONNECTED", "VERIFIED")
    )
    return PeerListResponse(peers=peers, total=len(peers), connected=connected)


@peer_router.post("/connect", response_model=PeerConnectResponse)
async def connect_peer(body: PeerConnectRequest, node=Depends(get_node)):
    """
    Dial a peer by multiaddr.

    The mesh host will attempt to connect and complete the Noise handshake.
    """
    mesh = _mesh(node)
    try:
        from neuralis.mesh.discovery import PeerAnnouncement
        announcement = PeerAnnouncement.from_multiaddr(body.multiaddr)
        mesh._on_peer_discovered(announcement)
        return PeerConnectResponse(
            success  = True,
            node_id  = announcement.node_id,
            message  = f"Dial scheduled to {body.multiaddr}",
        )
    except Exception as exc:
        logger.warning("connect_peer error: %s", exc)
        return PeerConnectResponse(success=False, message=str(exc))


@peer_router.delete("/{node_id}", response_model=PeerDisconnectResponse)
async def disconnect_peer(node_id: str, node=Depends(get_node)):
    """Disconnect and evict a peer by node_id."""
    mesh = _mesh(node)
    conn = mesh.connections.get(node_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Peer {node_id!r} not connected")
    try:
        await mesh._handle_disconnection(node_id)
        return PeerDisconnectResponse(success=True, message=f"Disconnected {node_id[:16]}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@peer_router.get("/{node_id}", response_model=PeerResponse)
async def get_peer(node_id: str, node=Depends(get_node)):
    """Return info for a single peer."""
    mesh = _mesh(node)
    peer = mesh.peer_store.get_by_node_id(node_id)
    if peer is None:
        raise HTTPException(status_code=404, detail=f"Peer {node_id!r} not found")
    return PeerResponse(
        node_id         = peer.node_id,
        peer_id         = peer.peer_id,
        alias           = peer.alias,
        status          = peer.status.value if hasattr(peer.status, "value") else str(peer.status),
        addresses       = list(peer.addresses),
        last_seen       = peer.last_seen,
        last_ping_ms    = peer.last_ping_ms,
        failed_attempts = peer.failed_attempts,
    )


# ===========================================================================
# Content router  —  /api/content
# ===========================================================================

content_router = APIRouter(prefix="/api/content", tags=["content"])


def _ipfs(node):
    """Get the ipfs subsystem or raise 503."""
    ipfs = node.subsystems.get("ipfs")
    if ipfs is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IPFS subsystem not running",
        )
    return ipfs


@content_router.post("", response_model=ContentAddResponse)
async def add_content(body: ContentAddRequest, node=Depends(get_node)):
    """Store UTF-8 content and return its CID."""
    ipfs = _ipfs(node)
    try:
        data_bytes = body.data.encode("utf-8")
        cid = await ipfs.add(data_bytes, pin=body.pin, name=body.name)
        return ContentAddResponse(
            cid    = str(cid),
            size   = len(data_bytes),
            pinned = body.pin,
        )
    except Exception as exc:
        logger.error("add_content error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@content_router.get("/{cid}", response_model=ContentGetResponse)
async def get_content(cid: str, node=Depends(get_node)):
    """Retrieve content by CID."""
    ipfs = _ipfs(node)
    try:
        data_bytes = await ipfs.get(cid)
        if data_bytes is None:
            raise HTTPException(status_code=404, detail=f"CID {cid!r} not found")
        return ContentGetResponse(
            cid  = cid,
            data = data_bytes.decode("utf-8", errors="replace"),
            size = len(data_bytes),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_content error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@content_router.post("/{cid}/pin", response_model=PinResponse)
async def pin_content(cid: str, body: PinRequest, node=Depends(get_node)):
    """Pin content by CID so it survives garbage collection."""
    ipfs = _ipfs(node)
    try:
        await ipfs.pin(cid, name=body.name)
        return PinResponse(cid=cid, pinned=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@content_router.delete("/{cid}/pin", response_model=PinResponse)
async def unpin_content(cid: str, node=Depends(get_node)):
    """Unpin content (it may be garbage-collected)."""
    ipfs = _ipfs(node)
    try:
        await ipfs.unpin(cid)
        return PinResponse(cid=cid, pinned=False)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@content_router.get("", response_model=PinListResponse)
async def list_pins(node=Depends(get_node)):
    """List all pinned CIDs."""
    ipfs = _ipfs(node)
    try:
        pins = await ipfs.list_pins()
        return PinListResponse(pins=pins, total=len(pins))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@content_router.get("/stats/storage", response_model=StorageStatsResponse)
async def storage_stats(node=Depends(get_node)):
    """Return storage usage statistics."""
    ipfs = _ipfs(node)
    try:
        stats = await ipfs.stats()
        return StorageStatsResponse(
            total_blocks  = stats.get("total_blocks", 0),
            total_bytes   = stats.get("total_bytes", 0),
            pinned_count  = stats.get("pinned_count", 0),
            max_bytes     = stats.get("max_bytes", 0),
            used_percent  = stats.get("used_percent", 0.0),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ===========================================================================
# Agent router  —  /api/agents
# ===========================================================================

agent_router = APIRouter(prefix="/api/agents", tags=["agents"])


def _runtime(node):
    """Get the agent runtime subsystem or raise 503."""
    runtime = node.subsystems.get("agents")
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent runtime not running",
        )
    return runtime


@agent_router.get("", response_model=AgentListResponse)
async def list_agents(node=Depends(get_node)):
    """Return all loaded local agents."""
    runtime = _runtime(node)
    agents = [
        AgentResponse(
            name           = a.meta.name,
            version        = a.meta.version,
            state          = a.state.value if hasattr(a.state, "value") else str(a.state),
            capabilities   = list(a.meta.capabilities),
            required_model = a.meta.required_model,
            stats          = a.stats(),
        )
        for a in runtime.all_agents()
    ]
    return AgentListResponse(agents=agents, total=len(agents))


@agent_router.post("/task", response_model=TaskResponse)
async def dispatch_task(body: TaskRequest, node=Depends(get_node)):
    """Dispatch a task to a local agent and return the response."""
    runtime = _runtime(node)
    try:
        from neuralis.agents.base import AgentMessage
        msg = AgentMessage(
            target  = body.target or body.task,
            task    = body.task,
            payload = body.payload,
        )
        responses = await runtime.dispatch(msg)
        if not responses:
            raise HTTPException(
                status_code=404,
                detail=f"No agent handled task {body.task!r}",
            )
        r = responses[0]
        return TaskResponse(
            request_id  = r.request_id,
            agent       = r.agent,
            status      = r.status,
            data        = r.data or {},
            error       = r.error,
            duration_ms = r.duration_ms,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("dispatch_task error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@agent_router.post("/reload", response_model=AgentReloadResponse)
async def reload_agents(node=Depends(get_node)):
    """Hot-reload agents from the agents directory."""
    runtime = _runtime(node)
    try:
        result = await runtime.reload_agents()
        return AgentReloadResponse(
            added   = result.get("added", []),
            updated = result.get("updated", []),
            removed = result.get("removed", []),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@agent_router.get("/{name}", response_model=AgentResponse)
async def get_agent(name: str, node=Depends(get_node)):
    """Return info for a single agent by name."""
    runtime = _runtime(node)
    agent = runtime.get_agent(name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {name!r} not found")
    return AgentResponse(
        name           = agent.meta.name,
        version        = agent.meta.version,
        state          = agent.state.value if hasattr(agent.state, "value") else str(agent.state),
        capabilities   = list(agent.meta.capabilities),
        required_model = agent.meta.required_model,
        stats          = agent.stats(),
    )


# ===========================================================================
# Protocol router  —  /api/protocol
# ===========================================================================

protocol_router = APIRouter(prefix="/api/protocol", tags=["protocol"])


def _proto(node):
    """Get the protocol router subsystem or raise 503."""
    proto = node.subsystems.get("protocol")
    if proto is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Protocol router not running",
        )
    return proto


@protocol_router.get("/nodes", response_model=RemoteNodeListResponse)
async def list_remote_nodes(node=Depends(get_node)):
    """Return all remote nodes known to the protocol router."""
    proto = _proto(node)
    nodes = [
        RemoteNodeResponse(
            node_id  = info.node_id,
            agents   = list(info.capabilities.keys()),
            tasks    = list(info.tasks()),
            last_seen = info.last_seen,
        )
        for info in proto.all_remote_nodes()
    ]
    return RemoteNodeListResponse(nodes=nodes, total=len(nodes))


@protocol_router.post("/task", response_model=RemoteTaskResponse)
async def route_remote_task(body: RemoteTaskRequest, node=Depends(get_node)):
    """Route a task to a remote node via the protocol layer."""
    proto = _proto(node)
    try:
        result = await proto.route_task(
            task     = body.task,
            payload  = body.payload,
            dst_node = body.dst_node or "",
            timeout  = body.timeout,
        )
        return RemoteTaskResponse(
            src_node   = result.src_node,
            dst_node   = result.dst_node,
            task       = result.task,
            payload    = result.payload,
            session_id = result.session_id,
            msg_type   = result.msg_type.value,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@protocol_router.post("/query", response_model=OkResponse)
async def query_capabilities(dst_node: str = "", node=Depends(get_node)):
    """Broadcast (or unicast) a capability query to the mesh."""
    proto = _proto(node)
    await proto.query_capabilities(dst_node=dst_node)
    return OkResponse(message="Capability query sent")
