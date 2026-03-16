"""
test_module6.py
===============
Test suite for neuralis.api (Module 6 — canvas-api).

Uses FastAPI's TestClient (sync) and httpx.AsyncClient (async) via
ASGI transport — no real server needed, no ports opened.

Coverage
--------
  TestModels           — Pydantic model validation and serialisation
  TestHealthEndpoint   — GET /health
  TestNodeEndpoints    — GET/PATCH /api/node/*
  TestPeerEndpoints    — GET/POST/DELETE /api/peers/*
  TestContentEndpoints — GET/POST /api/content/*
  TestAgentEndpoints   — GET/POST /api/agents/*
  TestProtocolEndpoints— GET/POST /api/protocol/*
  TestWebSocket        — /ws connect, snapshot, ping/pong
  TestConnectionManager— broadcast, connect, disconnect
  TestNoNode           — 503 when subsystems not registered
  TestIntegration      — full node mock wired to all subsystems
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from neuralis.api.models import (
    AgentListResponse,
    AgentResponse,
    ContentAddRequest,
    ContentAddResponse,
    NodeStatusResponse,
    OkResponse,
    PeerListResponse,
    PeerResponse,
    RemoteNodeListResponse,
    StorageStatsResponse,
    TaskRequest,
    TaskResponse,
    ErrorResponse,
)
from neuralis.api.app import ConnectionManager, create_app

# ---------------------------------------------------------------------------
# httpx / starlette test deps
# ---------------------------------------------------------------------------
try:
    from httpx import AsyncClient, ASGITransport
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    from starlette.testclient import TestClient
    TESTCLIENT_AVAILABLE = True
except ImportError:
    TESTCLIENT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not HTTPX_AVAILABLE,
    reason="httpx required for API tests"
)


# ===========================================================================
# Fake objects
# ===========================================================================

NODE_ID = "NRL1aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PEER_ID = "12D3KooWaaaaaaaaaaaaaaaaaaaaaaaa"


def _fake_identity(node_id=NODE_ID):
    identity = MagicMock()
    identity.node_id     = node_id
    identity.peer_id     = PEER_ID
    identity.alias       = "test-node"
    identity.public_key_hex = MagicMock(return_value="abcd" * 16)
    identity.set_alias   = MagicMock()
    identity.sign        = MagicMock(return_value=b"\x00" * 64)
    return identity


def _fake_config():
    cfg = MagicMock()
    cfg.api.host         = "127.0.0.1"
    cfg.api.port         = 7100
    cfg.api.cors_origins = ["http://localhost:3000"]
    cfg.api.enable_docs  = False
    cfg.network.listen_addresses = ["/ip4/0.0.0.0/tcp/7101"]
    cfg.network.enable_mdns = True
    cfg.network.enable_dht  = True
    cfg.network.max_peers   = 50
    cfg.key_dir = "/tmp/neuralis-test"
    return cfg


def _fake_node(node_id=NODE_ID, subsystems: Optional[dict] = None):
    node = MagicMock()
    node.identity = _fake_identity(node_id)
    node.config   = _fake_config()
    node.state    = "RUNNING"
    node.boot_time = time.time() - 60
    node.subsystems = subsystems or {}
    node.status = MagicMock(return_value={
        "node_id":          node_id,
        "peer_id":          PEER_ID,
        "alias":            "test-node",
        "public_key":       "abcd" * 16,
        "state":            "RUNNING",
        "boot_time":        node.boot_time,
        "uptime_seconds":   60.0,
        "subsystems":       list((subsystems or {}).keys()),
        "listen_addresses": ["/ip4/0.0.0.0/tcp/7101"],
        "mdns_enabled":     True,
        "dht_enabled":      True,
        "max_peers":        50,
        "telemetry_enabled": False,
    })
    node.shutdown_async = AsyncMock()
    return node


def _fake_peer(node_id="NRL1bbbb", status="VERIFIED"):
    peer = MagicMock()
    peer.node_id         = node_id
    peer.peer_id         = "12D3KooWbbbb"
    peer.alias           = "peer-b"
    peer.status          = MagicMock()
    peer.status.value    = status
    peer.addresses       = ["/ip4/1.2.3.4/tcp/7101"]
    peer.last_seen       = time.time()
    peer.last_ping_ms    = 12.5
    peer.failed_attempts = 0
    return peer


def _fake_mesh(peers=None):
    mesh = MagicMock()
    peers = peers or [_fake_peer()]
    mesh.peer_store.all_peers = MagicMock(return_value=peers)
    mesh.peer_store.get_by_node_id = MagicMock(
        side_effect=lambda nid: next((p for p in peers if p.node_id == nid), None)
    )
    mesh.connections = {p.node_id: MagicMock() for p in peers}
    mesh._handle_disconnection = AsyncMock()
    mesh._on_peer_discovered   = MagicMock()
    mesh.on_message = MagicMock()
    return mesh


def _fake_ipfs():
    ipfs = MagicMock()
    ipfs.add        = AsyncMock(return_value="bafkreiabc123")
    ipfs.get        = AsyncMock(return_value=b"hello world")
    ipfs.pin        = AsyncMock()
    ipfs.unpin      = AsyncMock()
    ipfs.list_pins  = AsyncMock(return_value=[{"cid": "bafkreiabc123", "name": "test"}])
    ipfs.stats      = AsyncMock(return_value={
        "total_blocks": 10,
        "total_bytes":  1024,
        "pinned_count": 3,
        "max_bytes":    10 * 1024**3,
        "used_percent": 0.01,
    })
    return ipfs


def _fake_agent(name="echo", caps=None):
    agent = MagicMock()
    agent.meta.name           = name
    agent.meta.version        = "1.0.0"
    agent.meta.capabilities   = caps or [name]
    agent.meta.required_model = None
    agent.state               = MagicMock()
    agent.state.value         = "RUNNING"
    agent.stats               = MagicMock(return_value={
        "handled": 5, "errors": 0
    })
    return agent


def _fake_runtime(agents=None):
    runtime = MagicMock()
    agents = agents or [_fake_agent("echo")]
    runtime.all_agents  = MagicMock(return_value=agents)
    runtime.get_agent   = MagicMock(
        side_effect=lambda n: next((a for a in agents if a.meta.name == n), None)
    )
    runtime.dispatch    = AsyncMock()
    runtime.reload_agents = AsyncMock(return_value={"added": ["new"], "updated": [], "removed": []})
    return runtime


def _fake_proto(remote_nodes=None):
    from neuralis.api.models import RemoteNodeResponse
    proto = MagicMock()
    remote = remote_nodes or []
    proto.all_remote_nodes = MagicMock(return_value=remote)
    proto.route_task       = AsyncMock()
    proto.query_capabilities = AsyncMock()
    proto.on_protocol_message = MagicMock()
    return proto


def _make_app(subsystems=None):
    """Build a test app with a fully mocked node."""
    node = _fake_node(subsystems=subsystems or {})
    return create_app(node), node


async def _client(app):
    """Async context manager for an httpx test client."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ===========================================================================
# TestModels
# ===========================================================================

class TestModels:
    def test_node_status_response(self):
        m = NodeStatusResponse(
            node_id="NRL1aaa", peer_id="12D", alias="n",
            public_key="pk", state="RUNNING",
            boot_time=1.0, uptime_seconds=60.0,
            subsystems=["mesh"], listen_addresses=["/ip4/0.0.0.0/tcp/7101"],
            mdns_enabled=True, dht_enabled=True, max_peers=50,
        )
        assert m.telemetry_enabled is False

    def test_peer_response_defaults(self):
        p = PeerResponse(node_id="n", peer_id="p", status="VERIFIED")
        assert p.addresses == []
        assert p.last_seen is None

    def test_content_add_request_defaults(self):
        r = ContentAddRequest(data="hello")
        assert r.pin is True
        assert r.name is None

    def test_task_request_defaults(self):
        r = TaskRequest(task="echo")
        assert r.payload == {}
        assert r.timeout == 10.0

    def test_ok_response(self):
        r = OkResponse()
        assert r.ok is True

    def test_error_response(self):
        r = ErrorResponse(error="boom")
        assert r.ok is False

    def test_storage_stats_response(self):
        r = StorageStatsResponse(
            total_blocks=5, total_bytes=512,
            pinned_count=2, max_bytes=1000, used_percent=0.51,
        )
        assert r.used_percent == 0.51

    def test_agent_list_response(self):
        r = AgentListResponse(
            agents=[AgentResponse(name="echo", version="1.0.0", state="RUNNING")],
            total=1,
        )
        assert r.total == 1

    def test_remote_node_list_response(self):
        r = RemoteNodeListResponse(nodes=[], total=0)
        assert r.total == 0


# ===========================================================================
# TestHealthEndpoint
# ===========================================================================

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_health_returns_state(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.json()["state"] == "RUNNING"

    @pytest.mark.asyncio
    async def test_health_no_node(self):
        app = create_app(None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["state"] == "no_node"


# ===========================================================================
# TestNodeEndpoints
# ===========================================================================

class TestNodeEndpoints:
    @pytest.mark.asyncio
    async def test_get_status(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/node/status")
        assert r.status_code == 200
        data = r.json()
        assert data["node_id"] == NODE_ID
        assert data["telemetry_enabled"] is False

    @pytest.mark.asyncio
    async def test_set_alias(self):
        app, node = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/api/node/alias", json={"alias": "new-name"})
        assert r.status_code == 200
        assert r.json()["alias"] == "new-name"
        node.identity.set_alias.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_alias_empty_rejected(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/api/node/alias", json={"alias": ""})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_shutdown(self):
        app, node = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/node/shutdown")
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ===========================================================================
# TestPeerEndpoints
# ===========================================================================

class TestPeerEndpoints:
    def _app_with_mesh(self, peers=None):
        mesh = _fake_mesh(peers=peers or [_fake_peer()])
        return _make_app(subsystems={"mesh": mesh})

    @pytest.mark.asyncio
    async def test_list_peers(self):
        app, _ = self._app_with_mesh()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/peers")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["peers"][0]["status"] == "VERIFIED"

    @pytest.mark.asyncio
    async def test_list_peers_no_mesh(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/peers")
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_get_peer_found(self):
        peer = _fake_peer("NRL1bbbb")
        app, _ = self._app_with_mesh(peers=[peer])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/peers/NRL1bbbb")
        assert r.status_code == 200
        assert r.json()["node_id"] == "NRL1bbbb"

    @pytest.mark.asyncio
    async def test_get_peer_not_found(self):
        app, _ = self._app_with_mesh()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/peers/NRL1MISSING")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_disconnect_peer(self):
        peer = _fake_peer("NRL1bbbb")
        mesh = _fake_mesh(peers=[peer])
        app, _ = _make_app(subsystems={"mesh": mesh})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete("/api/peers/NRL1bbbb")
        assert r.status_code == 200
        mesh._handle_disconnection.assert_called_once_with("NRL1bbbb")

    @pytest.mark.asyncio
    async def test_disconnect_peer_not_connected(self):
        mesh = _fake_mesh(peers=[])
        mesh.connections = {}
        app, _ = _make_app(subsystems={"mesh": mesh})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete("/api/peers/NRL1GHOST")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_connected_count_in_list(self):
        peers = [_fake_peer("NRL1b", "VERIFIED"), _fake_peer("NRL1c", "DISCOVERED")]
        app, _ = self._app_with_mesh(peers=peers)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/peers")
        data = r.json()
        assert data["total"] == 2
        assert data["connected"] == 1   # only VERIFIED counts


# ===========================================================================
# TestContentEndpoints
# ===========================================================================

class TestContentEndpoints:
    def _app_with_ipfs(self):
        return _make_app(subsystems={"ipfs": _fake_ipfs()})

    @pytest.mark.asyncio
    async def test_add_content(self):
        app, _ = self._app_with_ipfs()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/content", json={"data": "hello world"})
        assert r.status_code == 200
        data = r.json()
        assert data["cid"] == "bafkreiabc123"
        assert data["pinned"] is True

    @pytest.mark.asyncio
    async def test_get_content(self):
        app, _ = self._app_with_ipfs()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/content/bafkreiabc123")
        assert r.status_code == 200
        assert r.json()["data"] == "hello world"

    @pytest.mark.asyncio
    async def test_get_content_not_found(self):
        ipfs = _fake_ipfs()
        ipfs.get = AsyncMock(return_value=None)
        app, _ = _make_app(subsystems={"ipfs": ipfs})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/content/bafkrei_missing")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_pin_content(self):
        app, _ = self._app_with_ipfs()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/content/bafkreiabc123/pin", json={})
        assert r.status_code == 200
        assert r.json()["pinned"] is True

    @pytest.mark.asyncio
    async def test_unpin_content(self):
        app, _ = self._app_with_ipfs()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.delete("/api/content/bafkreiabc123/pin")
        assert r.status_code == 200
        assert r.json()["pinned"] is False

    @pytest.mark.asyncio
    async def test_list_pins(self):
        app, _ = self._app_with_ipfs()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/content")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_storage_stats(self):
        app, _ = self._app_with_ipfs()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/content/stats/storage")
        assert r.status_code == 200
        data = r.json()
        assert data["total_blocks"] == 10
        assert data["used_percent"] == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_content_no_ipfs(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/content/bafkreiabc123")
        assert r.status_code == 503


# ===========================================================================
# TestAgentEndpoints
# ===========================================================================

class TestAgentEndpoints:
    def _app_with_runtime(self, agents=None):
        runtime = _fake_runtime(agents=agents)
        app, node = _make_app(subsystems={"agents": runtime})
        return app, runtime

    @pytest.mark.asyncio
    async def test_list_agents(self):
        app, _ = self._app_with_runtime()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/agents")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["agents"][0]["name"] == "echo"

    @pytest.mark.asyncio
    async def test_get_agent_found(self):
        app, _ = self._app_with_runtime()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/agents/echo")
        assert r.status_code == 200
        assert r.json()["name"] == "echo"

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self):
        app, _ = self._app_with_runtime()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/agents/ghost")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_dispatch_task_success(self):
        from neuralis.agents.base import AgentResponse as AR
        app, runtime = self._app_with_runtime()
        runtime.dispatch = AsyncMock(return_value=[
            AR.ok("req-1", "echo", {"result": "hello"})
        ])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/agents/task", json={"task": "echo", "payload": {"text": "hi"}})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["data"]["result"] == "hello"

    @pytest.mark.asyncio
    async def test_dispatch_task_no_handler(self):
        app, runtime = self._app_with_runtime()
        runtime.dispatch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/agents/task", json={"task": "missing"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_reload_agents(self):
        app, runtime = self._app_with_runtime()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/agents/reload")
        assert r.status_code == 200
        data = r.json()
        assert "new" in data["added"]

    @pytest.mark.asyncio
    async def test_agents_no_runtime(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/agents")
        assert r.status_code == 503


# ===========================================================================
# TestProtocolEndpoints
# ===========================================================================

class TestProtocolEndpoints:
    def _app_with_proto(self):
        from neuralis.protocol.router import RemoteNodeInfo
        from neuralis.protocol.messages import AgentCapability
        info = RemoteNodeInfo(
            "NRL1bbbb",
            capabilities={"echo": AgentCapability("echo", tasks=["echo"])},
        )
        proto = _fake_proto(remote_nodes=[info])
        app, node = _make_app(subsystems={"protocol": proto})
        return app, proto

    @pytest.mark.asyncio
    async def test_list_remote_nodes(self):
        app, _ = self._app_with_proto()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/protocol/nodes")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["nodes"][0]["node_id"] == "NRL1bbbb"

    @pytest.mark.asyncio
    async def test_list_remote_nodes_empty(self):
        proto = _fake_proto(remote_nodes=[])
        app, _ = _make_app(subsystems={"protocol": proto})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/protocol/nodes")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_route_remote_task(self):
        from neuralis.protocol.messages import ProtocolMessage, ProtocolMessageType
        app, proto = self._app_with_proto()
        mock_resp = ProtocolMessage.task_response(
            src_node   = "NRL1bbbb",
            dst_node   = NODE_ID,
            reply_to   = "req-id",
            session_id = "sess-id",
            task       = "echo",
            payload    = {"result": "pong"},
        )
        proto.route_task = AsyncMock(return_value=mock_resp)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/protocol/task", json={
                "task": "echo", "payload": {}, "dst_node": "NRL1bbbb"
            })
        assert r.status_code == 200
        data = r.json()
        assert data["task"] == "echo"
        assert data["msg_type"] == "TASK_RESPONSE"

    @pytest.mark.asyncio
    async def test_query_capabilities(self):
        app, proto = self._app_with_proto()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/protocol/query")
        assert r.status_code == 200
        proto.query_capabilities.assert_called_once()

    @pytest.mark.asyncio
    async def test_protocol_no_subsystem(self):
        app, _ = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/protocol/nodes")
        assert r.status_code == 503


# ===========================================================================
# TestWebSocket
# ===========================================================================

class TestWebSocket:
    @pytest.mark.asyncio
    async def test_ws_connect_receives_snapshot(self):
        app, _ = _make_app()
        from starlette.testclient import TestClient
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                data = ws.receive_json()
                assert data["event"] == "node_status"
                assert "node_id" in data["data"]

    @pytest.mark.asyncio
    async def test_ws_ping_pong(self):
        app, _ = _make_app()
        from starlette.testclient import TestClient
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()   # consume snapshot
                ws.send_text("ping")
                data = ws.receive_json()
                assert data["event"] == "pong"

    @pytest.mark.asyncio
    async def test_ws_no_node_no_snapshot(self):
        app = create_app(None)
        from starlette.testclient import TestClient
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_text("ping")
                data = ws.receive_json()
                assert data["event"] == "pong"


# ===========================================================================
# TestConnectionManager
# ===========================================================================

class TestConnectionManager:
    @pytest.mark.asyncio
    async def test_initial_count_zero(self):
        mgr = ConnectionManager()
        assert mgr.connection_count == 0

    @pytest.mark.asyncio
    async def test_connect_increments_count(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        assert mgr.connection_count == 1

    @pytest.mark.asyncio
    async def test_disconnect_decrements_count(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        mgr.disconnect(ws)
        assert mgr.connection_count == 0

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self):
        mgr = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await mgr.connect(ws1)
        await mgr.connect(ws2)
        await mgr.broadcast("test_event", {"key": "value"})
        ws1.send_text.assert_called_once()
        ws2.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_json_format(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        await mgr.broadcast("peer_connected", {"node_id": "NRL1aaa"})
        call_args = ws.send_text.call_args[0][0]
        payload = json.loads(call_args)
        assert payload["event"] == "peer_connected"
        assert payload["data"]["node_id"] == "NRL1aaa"
        assert "ts" in payload

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_connections(self):
        mgr = ConnectionManager()
        ws_dead = AsyncMock()
        ws_dead.send_text = AsyncMock(side_effect=Exception("disconnected"))
        ws_alive = AsyncMock()
        await mgr.connect(ws_dead)
        await mgr.connect(ws_alive)
        await mgr.broadcast("test", {})
        assert mgr.connection_count == 1  # dead one removed

    @pytest.mark.asyncio
    async def test_broadcast_empty_no_error(self):
        mgr = ConnectionManager()
        await mgr.broadcast("test", {})   # must not raise

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_safe(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        mgr.disconnect(ws)   # must not raise

    def test_repr(self):
        mgr = ConnectionManager()
        assert "ConnectionManager" in repr(mgr)


# ===========================================================================
# TestNoNode
# ===========================================================================

class TestNoNode:
    @pytest.mark.asyncio
    async def test_node_status_no_node(self):
        app = create_app(None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/node/status")
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_peers_no_node(self):
        app = create_app(None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/peers")
        assert r.status_code == 503


# ===========================================================================
# TestIntegration — all subsystems wired together
# ===========================================================================

class TestIntegration:
    def _full_app(self):
        from neuralis.protocol.router import RemoteNodeInfo
        from neuralis.protocol.messages import AgentCapability
        info = RemoteNodeInfo(
            "NRL1bbbb",
            capabilities={"echo": AgentCapability("echo", tasks=["echo"])},
        )
        proto   = _fake_proto(remote_nodes=[info])
        mesh    = _fake_mesh()
        ipfs    = _fake_ipfs()
        runtime = _fake_runtime()
        subs = {"mesh": mesh, "ipfs": ipfs, "agents": runtime, "protocol": proto}
        return _make_app(subsystems=subs)

    @pytest.mark.asyncio
    async def test_node_status_includes_all_subsystems(self):
        app, node = self._full_app()
        node.status.return_value["subsystems"] = ["mesh", "ipfs", "agents", "protocol"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/node/status")
        assert r.status_code == 200
        assert "mesh" in r.json()["subsystems"]

    @pytest.mark.asyncio
    async def test_full_pipeline_add_then_get(self):
        app, _ = self._full_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            add_r = await c.post("/api/content", json={"data": "neuralis test"})
            assert add_r.status_code == 200
            cid = add_r.json()["cid"]

            get_r = await c.get(f"/api/content/{cid}")
            assert get_r.status_code == 200

    @pytest.mark.asyncio
    async def test_all_routers_reachable(self):
        app, _ = self._full_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            endpoints = [
                ("GET",  "/health"),
                ("GET",  "/api/node/status"),
                ("GET",  "/api/peers"),
                ("GET",  "/api/agents"),
                ("GET",  "/api/protocol/nodes"),
            ]
            for method, path in endpoints:
                r = await c.request(method, path)
                assert r.status_code in (200, 201), f"{method} {path} → {r.status_code}"

    @pytest.mark.asyncio
    async def test_telemetry_never_true(self):
        """Telemetry must be False in every node status response."""
        app, _ = self._full_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/node/status")
        assert r.json()["telemetry_enabled"] is False

    @pytest.mark.asyncio
    async def test_cors_headers_present(self):
        app, _ = self._full_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(
                "/health",
                headers={"Origin": "http://localhost:3000"},
            )
        assert "access-control-allow-origin" in r.headers

    @pytest.mark.asyncio
    async def test_connection_manager_in_app_state(self):
        app, _ = self._full_app()
        assert isinstance(app.state.manager, ConnectionManager)
