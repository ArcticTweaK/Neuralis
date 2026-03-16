"""
neuralis.api.app
================
FastAPI application factory for the Canvas API (Module 6).

The Canvas API is a thin JSON bridge between the React Canvas UI (Module 7)
and the local Neuralis node.  It runs on 127.0.0.1:7100 by default and is
ONLY reachable from the local machine — no external exposure by design.

Architecture
------------
- One FastAPI app per node process
- All subsystems (mesh, ipfs, agents, protocol) are accessed via
  ``app.state.node`` — no globals, no singletons
- WebSocket endpoint at /ws for real-time Canvas updates (peer joins,
  agent state changes, content announcements)
- CORS locked to localhost origins defined in NodeConfig.api.cors_origins
- Swagger UI disabled in production (enable_docs=False by default)

Usage
-----
    from neuralis.api.app import create_app, serve

    app = create_app(node)
    await serve(app, node.config.api)   # blocks until shutdown

    # Or in tests:
    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/node/status")
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from neuralis.api.routes import (
    agent_router,
    content_router,
    node_router,
    peer_router,
    protocol_router,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """
    Tracks all live WebSocket connections and broadcasts JSON events.

    Events are structured as::

        {"event": "peer_connected", "data": {...}, "ts": 1234567890.0}

    The Canvas UI subscribes to /ws and renders real-time updates without
    polling the REST endpoints.
    """

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        logger.debug("WebSocket client connected (%d total)", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.debug("WebSocket client disconnected (%d total)", len(self._connections))

    async def broadcast(self, event: str, data: Dict[str, Any]) -> None:
        """Send an event to all connected WebSocket clients."""
        if not self._connections:
            return
        message = json.dumps({"event": event, "data": data, "ts": time.time()})
        dead: List[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def __repr__(self) -> str:
        return f"<ConnectionManager connections={self.connection_count}>"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(node=None) -> FastAPI:
    """
    Create and configure the Canvas API FastAPI application.

    Parameters
    ----------
    node : neuralis.node.Node | None
        The running node.  If None, the app starts but all /api/* endpoints
        return 503 until a node is attached via ``app.state.node = node``.

    Returns
    -------
    FastAPI
    """
    # Pull API config from node if available
    api_cfg  = node.config.api if node else None
    docs_url = "/docs" if (api_cfg and api_cfg.enable_docs) else None
    redoc_url = "/redoc" if (api_cfg and api_cfg.enable_docs) else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Lifespan context: wire mesh event hooks on startup."""
        logger.info("Canvas API starting up")
        if app.state.node is not None:
            _wire_mesh_events(app)
            _wire_agent_events(app)
        yield
        logger.info("Canvas API shutting down")

    app = FastAPI(
        title       = "Neuralis Canvas API",
        description = "Local-only JSON bridge between the Canvas UI and the Neuralis node.",
        version     = "0.1.0",
        docs_url    = docs_url,
        redoc_url   = redoc_url,
        lifespan    = lifespan,
    )

    # --- State ---
    app.state.node    = node
    app.state.manager = ConnectionManager()

    # --- CORS (localhost only) ---
    origins = (
        api_cfg.cors_origins
        if api_cfg
        else ["http://localhost:3000", "http://localhost:7100"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = origins,
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # --- Routers ---
    app.include_router(node_router)
    app.include_router(peer_router)
    app.include_router(content_router)
    app.include_router(agent_router)
    app.include_router(protocol_router)

    # --- WebSocket ---
    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        manager: ConnectionManager = ws.app.state.manager
        await manager.connect(ws)
        # Send initial snapshot on connect
        n = ws.app.state.node
        if n is not None:
            await ws.send_text(json.dumps({
                "event": "node_status",
                "data":  n.status(),
                "ts":    time.time(),
            }))
        try:
            while True:
                # Keep connection alive; client can send pings
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text(json.dumps({"event": "pong", "ts": time.time()}))
        except WebSocketDisconnect:
            manager.disconnect(ws)

    # --- Health check (no auth required) ---
    @app.get("/health")
    async def health():
        n = app.state.node
        return {
            "ok":    True,
            "state": n.state if n else "no_node",
            "ts":    time.time(),
        }

    return app


# ---------------------------------------------------------------------------
# Mesh event wiring
# ---------------------------------------------------------------------------

def _wire_mesh_events(app: FastAPI) -> None:
    """
    Hook into the mesh host to broadcast peer events over WebSocket.

    Called once during lifespan startup when a node is present.
    """
    node    = app.state.node
    manager = app.state.manager
    mesh    = node.subsystems.get("mesh")
    if mesh is None:
        return

    try:
        from neuralis.mesh.peers import MessageType
    except ImportError:
        return

    async def _on_peer_card(envelope, peer_info):
        if peer_info:
            await manager.broadcast("peer_connected", {
                "node_id":  peer_info.node_id,
                "alias":    peer_info.alias,
                "status":   peer_info.status.value
                            if hasattr(peer_info.status, "value")
                            else str(peer_info.status),
            })

    async def _on_goodbye(envelope, peer_info):
        await manager.broadcast("peer_disconnected", {
            "node_id": envelope.sender_id,
        })

    async def _on_content_announce(envelope, peer_info):
        await manager.broadcast("content_announced", {
            "sender":  envelope.sender_id,
            "payload": envelope.payload,
        })

    mesh.on_message(MessageType.PEER_CARD, _on_peer_card)
    mesh.on_message(MessageType.GOODBYE,   _on_goodbye)
    mesh.on_message(MessageType.CONTENT_ANNOUNCE, _on_content_announce)

    logger.debug("Mesh WebSocket event hooks wired")


def _wire_agent_events(app: FastAPI) -> None:
    """
    Hook into the protocol router to broadcast remote capability announcements.
    """
    node    = app.state.node
    manager = app.state.manager
    proto   = node.subsystems.get("protocol")
    if proto is None:
        return

    try:
        from neuralis.protocol.messages import ProtocolMessageType
    except ImportError:
        return

    async def _on_agent_announce(msg):
        await manager.broadcast("remote_agent_announce", {
            "node_id":      msg.src_node,
            "capabilities": msg.payload.get("capabilities", []),
        })

    proto.on_protocol_message(ProtocolMessageType.AGENT_ANNOUNCE, _on_agent_announce)
    logger.debug("Protocol WebSocket event hooks wired")


# ---------------------------------------------------------------------------
# ASGI server entry point
# ---------------------------------------------------------------------------

async def serve(app: FastAPI, api_cfg=None, host: str = "127.0.0.1", port: int = 7100) -> None:
    """
    Run the Canvas API with uvicorn.

    Parameters
    ----------
    app     : the FastAPI app returned by create_app()
    api_cfg : APIConfig (from NodeConfig.api); overrides host/port if given
    host    : fallback host (default 127.0.0.1)
    port    : fallback port (default 7100)
    """
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "uvicorn is required to run the Canvas API. "
            "Install it: pip install uvicorn"
        )

    if api_cfg is not None:
        host = api_cfg.host
        port = api_cfg.port

    config = uvicorn.Config(
        app         = app,
        host        = host,
        port        = port,
        log_level   = "warning",   # uvicorn's own logs; neuralis uses its own
        access_log  = False,       # no request logging — zero telemetry
    )
    server = uvicorn.Server(config)
    logger.info("Canvas API serving on http://%s:%d", host, port)
    await server.serve()
