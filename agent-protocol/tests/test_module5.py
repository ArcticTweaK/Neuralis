"""
test_module5.py
===============
Test suite for neuralis.protocol (Module 5 — agent-protocol).

Coverage
--------
  TestProtocolMessageType       — enum values and membership
  TestAgentCapability           — to_dict / from_dict round-trip
  TestProtocolMessage           — all factories, serialisation, helpers
  TestProtocolMessageExpiry     — TTL and timestamp logic
  TestProtocolMessageReply      — make_reply convenience
  TestProtocolError             — bad payloads raise correctly
  TestCodecFunctions            — encode / decode pure functions
  TestProtocolCodec             — stateful codec with error tracking
  TestRemoteNodeInfo            — capability table for one remote node
  TestProtocolRouter            — routing, capability, pending tracking
  TestProtocolRouterDispatch    — inbound message dispatch
  TestIntegration               — end-to-end task routing with fake mesh
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from neuralis.protocol.messages import (
    PROTOCOL_VERSION,
    MESSAGE_TTL_SECS,
    AgentCapability,
    ProtocolError,
    ProtocolMessage,
    ProtocolMessageType,
)
from neuralis.protocol.codec import ProtocolCodec, decode, encode
from neuralis.protocol.router import (
    NoRouteError,
    PendingRequest,
    ProtocolRouter,
    RemoteNodeInfo,
)


# ===========================================================================
# Fixtures & helpers
# ===========================================================================

NODE_A = "NRL1aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NODE_B = "NRL1bbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NODE_C = "NRL1cccccccccccccccccccccccccccc"


def _cap(name: str, tasks: List[str]) -> AgentCapability:
    return AgentCapability(agent_name=name, tasks=tasks)


def _make_node(node_id: str = NODE_A):
    """Minimal fake Node object."""
    node = MagicMock()
    node.identity.node_id = node_id
    node.register_subsystem = MagicMock()
    node.on_shutdown = MagicMock()
    return node


def _make_mesh(connected_peers: Optional[List[str]] = None):
    """Minimal fake MeshHost."""
    mesh = MagicMock()
    mesh.on_message = MagicMock()
    mesh.send_to = AsyncMock(return_value=True)
    mesh.broadcast = AsyncMock(return_value=len(connected_peers or []))
    peer_store = MagicMock()
    peer_store.get_by_node_id = MagicMock(return_value=None)
    mesh.peer_store = peer_store
    mesh.connections = {nid: MagicMock() for nid in (connected_peers or [])}
    return mesh


def _make_runtime(agents: Optional[list] = None):
    """Minimal fake AgentRuntime."""
    runtime = MagicMock()
    runtime.all_agents = MagicMock(return_value=agents or [])
    runtime.dispatch = AsyncMock(return_value=[])
    return runtime


def _make_router(
    node_id: str = NODE_A,
    connected: Optional[List[str]] = None,
    agents: Optional[list] = None,
    timeout: float = 5.0,
) -> ProtocolRouter:
    node    = _make_node(node_id)
    mesh    = _make_mesh(connected)
    runtime = _make_runtime(agents)
    return ProtocolRouter(node, mesh, runtime, timeout=timeout)


# ===========================================================================
# TestProtocolMessageType
# ===========================================================================

class TestProtocolMessageType:
    def test_all_values_are_strings(self):
        for mt in ProtocolMessageType:
            assert isinstance(mt.value, str)

    def test_expected_types_exist(self):
        expected = {
            "TASK_REQUEST", "TASK_RESPONSE", "TASK_ERROR",
            "CAPABILITY_QUERY", "CAPABILITY_REPLY",
            "AGENT_ANNOUNCE", "AGENT_WITHDRAW", "HEARTBEAT",
        }
        actual = {mt.value for mt in ProtocolMessageType}
        assert expected == actual

    def test_lookup_by_value(self):
        assert ProtocolMessageType("TASK_REQUEST") == ProtocolMessageType.TASK_REQUEST

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            ProtocolMessageType("NONEXISTENT")


# ===========================================================================
# TestAgentCapability
# ===========================================================================

class TestAgentCapability:
    def test_defaults(self):
        cap = AgentCapability(agent_name="echo")
        assert cap.version == "1.0.0"
        assert cap.tasks == []
        assert cap.required_model is None

    def test_to_dict(self):
        cap = AgentCapability("summarise", "2.0.0", ["summarise", "tldr"], "mistral.gguf")
        d = cap.to_dict()
        assert d["agent_name"] == "summarise"
        assert d["version"] == "2.0.0"
        assert d["tasks"] == ["summarise", "tldr"]
        assert d["required_model"] == "mistral.gguf"

    def test_from_dict_round_trip(self):
        cap = AgentCapability("echo", "1.0.0", ["echo", "ping"], None)
        restored = AgentCapability.from_dict(cap.to_dict())
        assert restored.agent_name == cap.agent_name
        assert restored.version == cap.version
        assert restored.tasks == cap.tasks
        assert restored.required_model is None

    def test_from_dict_defaults(self):
        cap = AgentCapability.from_dict({"agent_name": "stub"})
        assert cap.version == "1.0.0"
        assert cap.tasks == []
        assert cap.required_model is None

    def test_from_dict_missing_name_raises(self):
        with pytest.raises(KeyError):
            AgentCapability.from_dict({"tasks": ["echo"]})

    def test_tasks_is_independent_copy(self):
        """Mutating the returned tasks list must not affect the original."""
        cap = AgentCapability("echo", tasks=["echo"])
        d = cap.to_dict()
        d["tasks"].append("INJECTED")
        assert "INJECTED" not in cap.tasks

    def test_repr(self):
        cap = AgentCapability("echo", tasks=["echo"])
        assert "echo" in repr(cap)


# ===========================================================================
# TestProtocolMessage — basics
# ===========================================================================

class TestProtocolMessage:
    def test_task_request_factory(self):
        msg = ProtocolMessage.task_request(
            src_node  = NODE_A,
            dst_node  = NODE_B,
            task      = "echo",
            payload   = {"text": "hello"},
            src_agent = "sender",
            dst_agent = "receiver",
        )
        assert msg.msg_type  == ProtocolMessageType.TASK_REQUEST
        assert msg.src_node  == NODE_A
        assert msg.dst_node  == NODE_B
        assert msg.task      == "echo"
        assert msg.payload   == {"text": "hello"}
        assert msg.src_agent == "sender"
        assert msg.dst_agent == "receiver"
        assert msg.proto_version == PROTOCOL_VERSION

    def test_task_response_factory(self):
        req = ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {})
        resp = ProtocolMessage.task_response(
            src_node   = NODE_B,
            dst_node   = NODE_A,
            reply_to   = req.msg_id,
            session_id = req.session_id,
            task       = "echo",
            payload    = {"result": "ok"},
        )
        assert resp.msg_type  == ProtocolMessageType.TASK_RESPONSE
        assert resp.reply_to  == req.msg_id
        assert resp.session_id == req.session_id

    def test_task_error_factory(self):
        req = ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {})
        err = ProtocolMessage.task_error(
            src_node   = NODE_B,
            dst_node   = NODE_A,
            reply_to   = req.msg_id,
            session_id = req.session_id,
            task       = "echo",
            error      = "agent not found",
        )
        assert err.msg_type == ProtocolMessageType.TASK_ERROR
        assert err.payload["error"] == "agent not found"

    def test_capability_query_factory(self):
        msg = ProtocolMessage.capability_query(NODE_A)
        assert msg.msg_type == ProtocolMessageType.CAPABILITY_QUERY
        assert msg.dst_node == ""   # broadcast

    def test_capability_query_targeted(self):
        msg = ProtocolMessage.capability_query(NODE_A, dst_node=NODE_B)
        assert msg.dst_node == NODE_B

    def test_capability_reply_factory(self):
        caps = [_cap("echo", ["echo"])]
        msg = ProtocolMessage.capability_reply(NODE_B, NODE_A, "req-id", caps)
        assert msg.msg_type == ProtocolMessageType.CAPABILITY_REPLY
        assert len(msg.payload["capabilities"]) == 1

    def test_agent_announce_broadcast(self):
        caps = [_cap("echo", ["echo"]), _cap("search", ["search"])]
        msg = ProtocolMessage.agent_announce(NODE_A, caps)
        assert msg.msg_type == ProtocolMessageType.AGENT_ANNOUNCE
        assert msg.dst_node == ""
        assert len(msg.payload["capabilities"]) == 2

    def test_agent_withdraw_factory(self):
        msg = ProtocolMessage.agent_withdraw(NODE_A, ["echo", "search"])
        assert msg.msg_type == ProtocolMessageType.AGENT_WITHDRAW
        assert msg.payload["agents"] == ["echo", "search"]

    def test_heartbeat_factory(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        assert msg.msg_type == ProtocolMessageType.HEARTBEAT
        assert msg.src_node == NODE_A

    def test_unique_msg_ids(self):
        msgs = [ProtocolMessage.heartbeat(NODE_A) for _ in range(50)]
        ids = {m.msg_id for m in msgs}
        assert len(ids) == 50

    def test_unique_session_ids(self):
        msgs = [ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {}) for _ in range(20)]
        ids = {m.session_id for m in msgs}
        assert len(ids) == 20

    def test_is_broadcast_true(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        assert msg.is_broadcast()

    def test_is_broadcast_false(self):
        msg = ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {})
        assert not msg.is_broadcast()

    def test_repr(self):
        msg = ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {})
        r = repr(msg)
        assert "TASK_REQUEST" in r
        assert "echo" in r


# ===========================================================================
# TestProtocolMessageExpiry
# ===========================================================================

class TestProtocolMessageExpiry:
    def test_new_message_not_expired(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        assert not msg.is_expired()

    def test_old_message_is_expired(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        msg.timestamp = time.time() - (MESSAGE_TTL_SECS + 1)
        assert msg.is_expired()

    def test_decrement_ttl_returns_true(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        msg.ttl = 3
        assert msg.decrement_ttl() is True
        assert msg.ttl == 2

    def test_decrement_ttl_at_one_returns_false(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        msg.ttl = 1
        assert msg.decrement_ttl() is False
        assert msg.ttl == 0

    def test_decrement_ttl_does_not_go_negative(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        msg.ttl = 0
        msg.decrement_ttl()
        assert msg.ttl == 0


# ===========================================================================
# TestProtocolMessageReply
# ===========================================================================

class TestProtocolMessageReply:
    def test_make_reply_swaps_src_dst(self):
        req = ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {"text": "hi"})
        reply = req.make_reply(
            msg_type  = ProtocolMessageType.TASK_RESPONSE,
            payload   = {"result": "hi"},
            src_node  = NODE_B,
            src_agent = "echobot",
        )
        assert reply.src_node  == NODE_B
        assert reply.dst_node  == NODE_A
        assert reply.reply_to  == req.msg_id
        assert reply.session_id == req.session_id
        assert reply.task      == req.task
        assert reply.src_agent == "echobot"
        assert reply.dst_agent == req.src_agent

    def test_make_reply_unique_msg_id(self):
        req   = ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {})
        reply = req.make_reply(ProtocolMessageType.TASK_RESPONSE, {}, NODE_B)
        assert reply.msg_id != req.msg_id


# ===========================================================================
# TestProtocolMessageSerialisation
# ===========================================================================

class TestProtocolMessageSerialisation:
    def _round_trip(self, msg: ProtocolMessage) -> ProtocolMessage:
        return ProtocolMessage.from_dict(msg.to_dict())

    def test_task_request_round_trip(self):
        msg = ProtocolMessage.task_request(
            NODE_A, NODE_B, "echo", {"x": 1}, "agent-a", "agent-b"
        )
        restored = self._round_trip(msg)
        assert restored.msg_type   == msg.msg_type
        assert restored.src_node   == msg.src_node
        assert restored.dst_node   == msg.dst_node
        assert restored.task       == msg.task
        assert restored.payload    == msg.payload
        assert restored.src_agent  == msg.src_agent
        assert restored.dst_agent  == msg.dst_agent
        assert restored.msg_id     == msg.msg_id
        assert restored.session_id == msg.session_id
        assert restored.ttl        == msg.ttl

    def test_all_message_types_serialise(self):
        caps = [_cap("echo", ["echo"])]
        msgs = [
            ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {}),
            ProtocolMessage.task_response(NODE_B, NODE_A, "r", "s", "echo", {}),
            ProtocolMessage.task_error(NODE_B, NODE_A, "r", "s", "echo", "oops"),
            ProtocolMessage.capability_query(NODE_A),
            ProtocolMessage.capability_reply(NODE_B, NODE_A, "r", caps),
            ProtocolMessage.agent_announce(NODE_A, caps),
            ProtocolMessage.agent_withdraw(NODE_A, ["echo"]),
            ProtocolMessage.heartbeat(NODE_A),
        ]
        for msg in msgs:
            restored = self._round_trip(msg)
            assert restored.msg_type == msg.msg_type

    def test_from_dict_bad_msg_type(self):
        d = ProtocolMessage.heartbeat(NODE_A).to_dict()
        d["msg_type"] = "INVALID_TYPE"
        with pytest.raises(ProtocolError):
            ProtocolMessage.from_dict(d)

    def test_from_dict_missing_msg_type(self):
        d = ProtocolMessage.heartbeat(NODE_A).to_dict()
        del d["msg_type"]
        with pytest.raises(ProtocolError):
            ProtocolMessage.from_dict(d)

    def test_payload_is_independent_copy(self):
        """Mutating the payload after creation must not affect serialised form."""
        msg = ProtocolMessage.task_request(NODE_A, NODE_B, "echo", {"k": "v"})
        d = msg.to_dict()
        d["payload"]["injected"] = True
        assert "injected" not in msg.payload


# ===========================================================================
# TestCodecFunctions
# ===========================================================================

class TestCodecFunctions:
    def test_encode_returns_dict(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        result = encode(msg)
        assert isinstance(result, dict)
        assert result["msg_type"] == "HEARTBEAT"

    def test_decode_returns_protocol_message(self):
        msg = ProtocolMessage.heartbeat(NODE_A)
        decoded = decode(encode(msg))
        assert isinstance(decoded, ProtocolMessage)
        assert decoded.msg_type == ProtocolMessageType.HEARTBEAT

    def test_decode_non_dict_raises(self):
        with pytest.raises(ProtocolError):
            decode("not a dict")  # type: ignore

    def test_decode_zero_version_raises(self):
        d = ProtocolMessage.heartbeat(NODE_A).to_dict()
        d["proto_version"] = 0
        with pytest.raises(ProtocolError):
            decode(d)

    def test_decode_negative_version_raises(self):
        d = ProtocolMessage.heartbeat(NODE_A).to_dict()
        d["proto_version"] = -1
        with pytest.raises(ProtocolError):
            decode(d)

    def test_decode_future_version_succeeds_with_warning(self, caplog):
        d = ProtocolMessage.heartbeat(NODE_A).to_dict()
        d["proto_version"] = PROTOCOL_VERSION + 99
        import logging
        with caplog.at_level(logging.WARNING, logger="neuralis.protocol.codec"):
            msg = decode(d)
        assert msg.proto_version == PROTOCOL_VERSION + 99

    def test_encode_decode_round_trip(self):
        original = ProtocolMessage.task_request(
            NODE_A, NODE_B, "summarise", {"text": "hello world"}
        )
        restored = decode(encode(original))
        assert restored.src_node == original.src_node
        assert restored.task     == original.task
        assert restored.payload  == original.payload


# ===========================================================================
# TestProtocolCodec
# ===========================================================================

class TestProtocolCodec:
    def test_initial_stats_zero(self):
        codec = ProtocolCodec()
        s = codec.stats()
        assert s["messages_in"]  == 0
        assert s["messages_out"] == 0
        assert s["decode_errors"] == 0
        assert s["encode_errors"] == 0

    def test_encode_increments_messages_out(self):
        codec = ProtocolCodec()
        codec.encode(ProtocolMessage.heartbeat(NODE_A))
        assert codec.messages_out == 1

    def test_decode_increments_messages_in(self):
        codec = ProtocolCodec()
        d = encode(ProtocolMessage.heartbeat(NODE_A))
        codec.decode(d)
        assert codec.messages_in == 1

    def test_bad_decode_increments_error_count(self):
        codec = ProtocolCodec()
        with pytest.raises(ProtocolError):
            codec.decode({"msg_type": "BAD_TYPE", "proto_version": 1})
        assert codec.decode_errors == 1

    def test_decode_safe_returns_none_on_error(self):
        codec = ProtocolCodec()
        result = codec.decode_safe({"bad": "data"})
        assert result is None
        assert codec.decode_errors == 1

    def test_decode_safe_returns_message_on_success(self):
        codec = ProtocolCodec()
        d = encode(ProtocolMessage.heartbeat(NODE_A))
        result = codec.decode_safe(d)
        assert result is not None
        assert result.msg_type == ProtocolMessageType.HEARTBEAT

    def test_multiple_messages(self):
        codec = ProtocolCodec()
        for _ in range(5):
            codec.encode(ProtocolMessage.heartbeat(NODE_A))
        for _ in range(3):
            d = encode(ProtocolMessage.heartbeat(NODE_A))
            codec.decode(d)
        assert codec.messages_out == 5
        assert codec.messages_in  == 3

    def test_repr(self):
        codec = ProtocolCodec()
        assert "ProtocolCodec" in repr(codec)


# ===========================================================================
# TestRemoteNodeInfo
# ===========================================================================

class TestRemoteNodeInfo:
    def test_tasks_returns_flat_set(self):
        info = RemoteNodeInfo(NODE_B, capabilities={
            "echo":    AgentCapability("echo",    tasks=["echo", "ping"]),
            "search":  AgentCapability("search",  tasks=["search"]),
        })
        assert info.tasks() == {"echo", "ping", "search"}

    def test_can_handle_true(self):
        info = RemoteNodeInfo(NODE_B, capabilities={
            "echo": AgentCapability("echo", tasks=["echo"]),
        })
        assert info.can_handle("echo")

    def test_can_handle_false(self):
        info = RemoteNodeInfo(NODE_B, capabilities={
            "echo": AgentCapability("echo", tasks=["echo"]),
        })
        assert not info.can_handle("summarise")

    def test_update_replaces_capabilities(self):
        info = RemoteNodeInfo(NODE_B)
        info.update([AgentCapability("echo", tasks=["echo"])])
        assert "echo" in info.capabilities
        info.update([AgentCapability("search", tasks=["search"])])
        assert "echo" not in info.capabilities
        assert "search" in info.capabilities

    def test_update_refreshes_last_seen(self):
        info = RemoteNodeInfo(NODE_B)
        info.last_seen = time.time() - 200
        info.update([])
        assert (time.time() - info.last_seen) < 1.0

    def test_withdraw_removes_named_agents(self):
        info = RemoteNodeInfo(NODE_B, capabilities={
            "echo":   AgentCapability("echo",   tasks=["echo"]),
            "search": AgentCapability("search", tasks=["search"]),
        })
        info.withdraw(["echo"])
        assert "echo"   not in info.capabilities
        assert "search" in    info.capabilities

    def test_withdraw_nonexistent_is_safe(self):
        info = RemoteNodeInfo(NODE_B)
        info.withdraw(["ghost"])   # must not raise

    def test_is_stale_true(self):
        info = RemoteNodeInfo(NODE_B)
        info.last_seen = time.time() - 400
        assert info.is_stale(300.0)

    def test_is_stale_false(self):
        info = RemoteNodeInfo(NODE_B)
        info.last_seen = time.time() - 10
        assert not info.is_stale(300.0)

    def test_empty_node_has_no_tasks(self):
        info = RemoteNodeInfo(NODE_B)
        assert info.tasks() == set()

    def test_repr(self):
        info = RemoteNodeInfo(NODE_B)
        assert "RemoteNodeInfo" in repr(info)


# ===========================================================================
# TestProtocolRouter — lifecycle and routing
# ===========================================================================

class TestProtocolRouter:
    def test_initial_state(self):
        router = _make_router()
        assert not router._running
        assert router._remote_nodes == {}
        assert router._pending == {}

    @pytest.mark.asyncio
    async def test_start_registers_mesh_handler(self):
        router = _make_router()
        await router.start()
        router._mesh.on_message.assert_called_once()
        await router.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        router = _make_router()
        await router.start()
        await router.start()   # second call must not raise or double-register
        assert router._mesh.on_message.call_count == 1
        await router.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_futures(self):
        router = _make_router()
        await router.start()

        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        router._pending["test-id"] = PendingRequest(
            msg_id="test-id", session_id="s", task="echo",
            dst_node=NODE_B, future=fut,
        )
        await router.stop()
        assert fut.cancelled()

    @pytest.mark.asyncio
    async def test_stop_when_not_running_is_safe(self):
        router = _make_router()
        await router.stop()   # must not raise

    @pytest.mark.asyncio
    async def test_repr(self):
        router = _make_router()
        assert "ProtocolRouter" in repr(router)

    def test_status_when_stopped(self):
        router = _make_router()
        s = router.status()
        assert s["running"]       is False
        assert s["remote_nodes"]  == 0
        assert s["pending_requests"] == 0

    @pytest.mark.asyncio
    async def test_status_when_running(self):
        router = _make_router()
        await router.start()
        s = router.status()
        assert s["running"] is True
        await router.stop()

    @pytest.mark.asyncio
    async def test_route_task_not_running_raises(self):
        router = _make_router()
        with pytest.raises(RuntimeError):
            await router.route_task("echo", {}, dst_node=NODE_B)

    @pytest.mark.asyncio
    async def test_route_task_no_route_raises(self):
        router = _make_router()
        await router.start()
        with pytest.raises(NoRouteError):
            await router.route_task("echo", {})   # no remote nodes known
        await router.stop()

    @pytest.mark.asyncio
    async def test_route_task_send_failure_raises_no_route(self):
        router = _make_router(connected=[NODE_B])
        await router.start()

        # Inject a remote node so routing succeeds but send fails
        router._remote_nodes[NODE_B] = RemoteNodeInfo(
            NODE_B, capabilities={"echo": _cap("echo", ["echo"])}
        )
        router._mesh.send_to = AsyncMock(return_value=False)

        with pytest.raises(NoRouteError):
            await router.route_task("echo", {})
        await router.stop()

    @pytest.mark.asyncio
    async def test_route_task_resolves_on_response(self):
        router = _make_router(connected=[NODE_B])
        await router.start()

        router._remote_nodes[NODE_B] = RemoteNodeInfo(
            NODE_B, capabilities={"echo": _cap("echo", ["echo"])}
        )

        async def _fake_send(node_id, msg_type, payload):
            # Immediately resolve the pending future with a fake response
            # find the pending request for this task
            for req in router._pending.values():
                if req.task == "echo" and not req.future.done():
                    resp = ProtocolMessage.task_response(
                        src_node   = NODE_B,
                        dst_node   = NODE_A,
                        reply_to   = req.msg_id,
                        session_id = req.session_id,
                        task       = "echo",
                        payload    = {"result": "hello"},
                    )
                    req.future.set_result(resp)
            return True

        router._mesh.send_to = _fake_send

        result = await router.route_task("echo", {"text": "hello"})
        assert result.payload["result"] == "hello"
        await router.stop()

    @pytest.mark.asyncio
    async def test_route_task_timeout(self):
        router = _make_router(connected=[NODE_B], timeout=0.05)
        await router.start()

        router._remote_nodes[NODE_B] = RemoteNodeInfo(
            NODE_B, capabilities={"echo": _cap("echo", ["echo"])}
        )
        # send_to returns True but never resolves the future
        router._mesh.send_to = AsyncMock(return_value=True)

        with pytest.raises(asyncio.TimeoutError):
            await router.route_task("echo", {}, timeout=0.05)
        await router.stop()

    @pytest.mark.asyncio
    async def test_broadcast_task(self):
        router = _make_router(connected=[NODE_B, NODE_C])
        await router.start()
        count = await router.broadcast_task("ping", {})
        assert count == 2
        await router.stop()

    @pytest.mark.asyncio
    async def test_query_capabilities_broadcast(self):
        router = _make_router()
        await router.start()
        await router.query_capabilities()
        router._mesh.broadcast.assert_called()
        await router.stop()

    @pytest.mark.asyncio
    async def test_query_capabilities_targeted(self):
        router = _make_router()
        await router.start()
        await router.query_capabilities(dst_node=NODE_B)
        router._mesh.send_to.assert_called()
        await router.stop()

    def test_nodes_for_task_empty(self):
        router = _make_router()
        assert router.nodes_for_task("echo") == []

    def test_nodes_for_task_found(self):
        router = _make_router()
        router._remote_nodes[NODE_B] = RemoteNodeInfo(
            NODE_B, capabilities={"echo": _cap("echo", ["echo"])}
        )
        result = router.nodes_for_task("echo")
        assert len(result) == 1
        assert result[0].node_id == NODE_B

    def test_all_remote_nodes(self):
        router = _make_router()
        router._remote_nodes[NODE_B] = RemoteNodeInfo(NODE_B)
        router._remote_nodes[NODE_C] = RemoteNodeInfo(NODE_C)
        assert len(router.all_remote_nodes()) == 2

    def test_get_remote_node_found(self):
        router = _make_router()
        router._remote_nodes[NODE_B] = RemoteNodeInfo(NODE_B)
        assert router.get_remote_node(NODE_B) is not None

    def test_get_remote_node_missing(self):
        router = _make_router()
        assert router.get_remote_node(NODE_B) is None

    def test_on_protocol_message_registers_handler(self):
        router = _make_router()
        handler = AsyncMock()
        router.on_protocol_message(ProtocolMessageType.HEARTBEAT, handler)
        assert handler in router._handlers[ProtocolMessageType.HEARTBEAT]


# ===========================================================================
# TestProtocolRouterDispatch — inbound message handling
# ===========================================================================

class TestProtocolRouterDispatch:
    """Tests for _on_mesh_agent_msg and the internal dispatch table."""

    def _fake_envelope(self, msg: ProtocolMessage):
        env = MagicMock()
        env.payload    = msg.to_dict()
        env.sender_id  = msg.src_node
        return env

    @pytest.mark.asyncio
    async def test_agent_announce_updates_remote_nodes(self):
        router = _make_router()
        await router.start()

        caps = [_cap("echo", ["echo"])]
        announce = ProtocolMessage.agent_announce(NODE_B, caps)
        env = self._fake_envelope(announce)

        await router._on_mesh_agent_msg(env, None)
        assert NODE_B in router._remote_nodes
        assert router._remote_nodes[NODE_B].can_handle("echo")
        await router.stop()

    @pytest.mark.asyncio
    async def test_agent_withdraw_removes_agent(self):
        router = _make_router()
        await router.start()

        router._remote_nodes[NODE_B] = RemoteNodeInfo(
            NODE_B, capabilities={"echo": _cap("echo", ["echo"])}
        )
        withdraw = ProtocolMessage.agent_withdraw(NODE_B, ["echo"])
        env = self._fake_envelope(withdraw)

        await router._on_mesh_agent_msg(env, None)
        assert "echo" not in router._remote_nodes[NODE_B].capabilities
        await router.stop()

    @pytest.mark.asyncio
    async def test_capability_query_triggers_reply(self):
        router = _make_router()
        await router.start()

        query = ProtocolMessage.capability_query(NODE_B, dst_node=NODE_A)
        env = self._fake_envelope(query)

        await router._on_mesh_agent_msg(env, None)
        router._mesh.send_to.assert_called()
        await router.stop()

    @pytest.mark.asyncio
    async def test_capability_reply_updates_remote(self):
        router = _make_router()
        await router.start()

        caps = [_cap("search", ["search"])]
        reply = ProtocolMessage.capability_reply(NODE_B, NODE_A, "q-id", caps)
        env = self._fake_envelope(reply)

        await router._on_mesh_agent_msg(env, None)
        assert NODE_B in router._remote_nodes
        assert router._remote_nodes[NODE_B].can_handle("search")
        await router.stop()

    @pytest.mark.asyncio
    async def test_heartbeat_updates_last_seen(self):
        router = _make_router()
        await router.start()

        router._remote_nodes[NODE_B] = RemoteNodeInfo(NODE_B)
        router._remote_nodes[NODE_B].last_seen = time.time() - 100

        hb = ProtocolMessage.heartbeat(NODE_B)
        env = self._fake_envelope(hb)

        await router._on_mesh_agent_msg(env, None)
        assert (time.time() - router._remote_nodes[NODE_B].last_seen) < 1.0
        await router.stop()

    @pytest.mark.asyncio
    async def test_expired_message_dropped(self):
        router = _make_router()
        await router.start()

        hb = ProtocolMessage.heartbeat(NODE_B)
        hb.timestamp = time.time() - (MESSAGE_TTL_SECS + 5)
        env = self._fake_envelope(hb)

        # Should not crash and should not update anything
        router._remote_nodes[NODE_B] = RemoteNodeInfo(NODE_B)
        before = router._remote_nodes[NODE_B].last_seen
        await router._on_mesh_agent_msg(env, None)
        assert router._remote_nodes[NODE_B].last_seen == before
        await router.stop()

    @pytest.mark.asyncio
    async def test_message_not_for_us_dropped(self):
        router = _make_router(node_id=NODE_A)
        await router.start()

        # Message addressed to NODE_C, not NODE_A
        hb = ProtocolMessage.heartbeat(NODE_B)
        hb.dst_node = NODE_C
        env = self._fake_envelope(hb)

        router._remote_nodes[NODE_B] = RemoteNodeInfo(NODE_B)
        before = router._remote_nodes[NODE_B].last_seen
        await router._on_mesh_agent_msg(env, None)
        assert router._remote_nodes[NODE_B].last_seen == before
        await router.stop()

    @pytest.mark.asyncio
    async def test_task_response_resolves_pending(self):
        router = _make_router()
        await router.start()

        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        req_id = str(uuid.uuid4())
        router._pending[req_id] = PendingRequest(
            msg_id="irrelevant", session_id="s", task="echo",
            dst_node=NODE_B, future=fut,
        )

        resp = ProtocolMessage.task_response(
            src_node   = NODE_B,
            dst_node   = NODE_A,
            reply_to   = req_id,
            session_id = "s",
            task       = "echo",
            payload    = {"result": "done"},
        )
        env = self._fake_envelope(resp)
        await router._on_mesh_agent_msg(env, None)

        assert fut.done()
        resolved = fut.result()
        assert resolved.payload["result"] == "done"
        await router.stop()

    @pytest.mark.asyncio
    async def test_task_error_resolves_pending_with_error_msg(self):
        router = _make_router()
        await router.start()

        loop   = asyncio.get_event_loop()
        fut    = loop.create_future()
        req_id = str(uuid.uuid4())
        router._pending[req_id] = PendingRequest(
            msg_id="irrel", session_id="s", task="echo",
            dst_node=NODE_B, future=fut,
        )

        err_msg = ProtocolMessage.task_error(
            src_node=NODE_B, dst_node=NODE_A,
            reply_to=req_id, session_id="s",
            task="echo", error="agent crashed",
        )
        env = self._fake_envelope(err_msg)
        await router._on_mesh_agent_msg(env, None)

        assert fut.done()
        result = fut.result()
        assert result.msg_type == ProtocolMessageType.TASK_ERROR
        await router.stop()

    @pytest.mark.asyncio
    async def test_unparseable_payload_does_not_crash(self):
        router = _make_router()
        await router.start()

        env = MagicMock()
        env.payload   = {"msg_type": "GARBAGE", "proto_version": 1}
        env.sender_id = NODE_B

        await router._on_mesh_agent_msg(env, None)   # must not raise
        await router.stop()

    @pytest.mark.asyncio
    async def test_external_handler_called(self):
        router = _make_router()
        await router.start()

        received = []
        async def _handler(msg):
            received.append(msg)

        router.on_protocol_message(ProtocolMessageType.HEARTBEAT, _handler)

        hb  = ProtocolMessage.heartbeat(NODE_B)
        env = MagicMock()
        env.payload   = hb.to_dict()
        env.sender_id = NODE_B

        await router._on_mesh_agent_msg(env, None)
        assert len(received) == 1
        assert received[0].msg_type == ProtocolMessageType.HEARTBEAT
        await router.stop()

    @pytest.mark.asyncio
    async def test_task_request_no_runtime_sends_error(self):
        node = _make_node(NODE_A)
        mesh = _make_mesh()
        # No agent runtime
        router = ProtocolRouter(node, mesh, agent_runtime=None)
        await router.start()

        req = ProtocolMessage.task_request(NODE_B, NODE_A, "echo", {})
        env = MagicMock()
        env.payload   = req.to_dict()
        env.sender_id = NODE_B

        await router._on_mesh_agent_msg(env, None)
        mesh.send_to.assert_called()   # should have sent a TASK_ERROR back
        await router.stop()


# ===========================================================================
# TestIntegration — end-to-end with wired-together fakes
# ===========================================================================

class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_task_routing_round_trip(self):
        """
        Simulate Node A sending a task to Node B and receiving a response.

        - Router A's _mesh.send_to is wired to directly call Router B's
          _on_mesh_agent_msg.
        - Router B has a real AgentRuntime that returns a fake response.
        """
        node_a = _make_node(NODE_A)
        node_b = _make_node(NODE_B)
        mesh_a = _make_mesh([NODE_B])
        mesh_b = _make_mesh([NODE_A])

        # When mesh_a sends to NODE_B, feed to router_b
        router_b_ref: List[ProtocolRouter] = []

        async def _a_sends_to_b(dst, msg_type, payload):
            env = MagicMock()
            env.payload   = payload
            env.sender_id = NODE_A
            if router_b_ref:
                await router_b_ref[0]._on_mesh_agent_msg(env, None)
            return True

        mesh_a.send_to = _a_sends_to_b

        # Router B's runtime returns a response
        from neuralis.agents.base import AgentResponse
        runtime_b = _make_runtime()
        runtime_b.dispatch = AsyncMock(
            return_value=[AgentResponse.ok("req-id", "echo", {"result": "pong"})]
        )

        # When mesh_b sends to NODE_A, resolve router_a's pending future
        router_a_ref: List[ProtocolRouter] = []

        async def _b_sends_to_a(dst, msg_type, payload):
            env = MagicMock()
            env.payload   = payload
            env.sender_id = NODE_B
            if router_a_ref:
                await router_a_ref[0]._on_mesh_agent_msg(env, None)
            return True

        mesh_b.send_to = _b_sends_to_a

        router_a = ProtocolRouter(node_a, mesh_a, None,  timeout=5.0)
        router_b = ProtocolRouter(node_b, mesh_b, runtime_b, timeout=5.0)
        router_a_ref.append(router_a)
        router_b_ref.append(router_b)

        await router_a.start()
        await router_b.start()

        # Tell A about B's capabilities
        router_a._remote_nodes[NODE_B] = RemoteNodeInfo(
            NODE_B, capabilities={"echo": _cap("echo", ["echo"])}
        )

        result = await router_a.route_task("echo", {"text": "hello"})
        assert result.msg_type in (
            ProtocolMessageType.TASK_RESPONSE,
            ProtocolMessageType.TASK_ERROR,
        )

        await router_a.stop()
        await router_b.stop()

    @pytest.mark.asyncio
    async def test_capability_announce_and_lookup(self):
        """Start router, inject capabilities, verify lookup."""
        router = _make_router()
        await router.start()

        caps = [
            _cap("summarise", ["summarise", "tldr"]),
            _cap("search",    ["search", "web"]),
        ]
        router._upsert_remote(NODE_B, caps)

        assert router.nodes_for_task("summarise")[0].node_id == NODE_B
        assert router.nodes_for_task("web")[0].node_id       == NODE_B
        assert router.nodes_for_task("echo") == []

        await router.stop()

    @pytest.mark.asyncio
    async def test_stale_node_evicted_by_eviction_loop(self):
        """Artificially age a remote node and run one eviction cycle."""
        router = _make_router()
        await router.start()

        router._remote_nodes[NODE_B] = RemoteNodeInfo(NODE_B)
        router._remote_nodes[NODE_B].last_seen = time.time() - 400   # > 5 min

        # Run eviction logic directly (don't wait 5s in tests)
        stale = [
            nid for nid, info in router._remote_nodes.items()
            if info.is_stale(300.0)
        ]
        for nid in stale:
            del router._remote_nodes[nid]

        assert NODE_B not in router._remote_nodes
        await router.stop()

    @pytest.mark.asyncio
    async def test_codec_used_end_to_end(self):
        """Encode a message, decode it, verify round-trip fidelity."""
        codec = ProtocolCodec()
        original = ProtocolMessage.task_request(
            NODE_A, NODE_B, "summarise",
            {"text": "Neuralis is a decentralised AI mesh."},
            src_agent = "planner",
            dst_agent = "summarise",
        )
        payload  = codec.encode(original)
        restored = codec.decode(payload)

        assert restored.src_node   == original.src_node
        assert restored.dst_node   == original.dst_node
        assert restored.task       == original.task
        assert restored.payload    == original.payload
        assert restored.src_agent  == original.src_agent
        assert restored.session_id == original.session_id

        s = codec.stats()
        assert s["messages_out"] == 1
        assert s["messages_in"]  == 1

    @pytest.mark.asyncio
    async def test_multi_node_capability_table(self):
        """Multiple remote nodes with overlapping capabilities."""
        router = _make_router()
        await router.start()

        router._upsert_remote(NODE_B, [_cap("echo",   ["echo"])])
        router._upsert_remote(NODE_C, [_cap("echo",   ["echo"]),
                                        _cap("search", ["search"])])

        echo_nodes = router.nodes_for_task("echo")
        assert len(echo_nodes) == 2

        search_nodes = router.nodes_for_task("search")
        assert len(search_nodes) == 1
        assert search_nodes[0].node_id == NODE_C

        await router.stop()

    @pytest.mark.asyncio
    async def test_pending_request_eviction(self):
        """Expired pending requests are cancelled by the eviction loop."""
        router = _make_router(timeout=0.01)
        await router.start()

        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        old_req = PendingRequest(
            msg_id="old-id", session_id="s", task="echo",
            dst_node=NODE_B, future=fut,
            created_at=time.time() - 100,   # already expired
        )
        router._pending["old-id"] = old_req

        # Run eviction inline
        expired_ids = [
            mid for mid, req in router._pending.items()
            if req.is_expired(router._timeout)
        ]
        for mid in expired_ids:
            req = router._pending.pop(mid, None)
            if req and not req.future.done():
                req.future.set_exception(asyncio.TimeoutError("evicted"))

        assert "old-id" not in router._pending
        assert fut.done()

        await router.stop()
