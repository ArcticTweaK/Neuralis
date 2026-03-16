"""
tests/test_module4.py
=====================
Full test suite for Module 4: agent-runtime

Tests cover:
- AgentMessage / AgentResponse / AgentMeta data structures
- AgentState transitions
- BaseAgent lifecycle and interface contract
- AgentBus: subscribe/unsubscribe, publish, request/reply, dead-letter, wildcard
- InferenceEngine: stub mode, load/unload, complete, stats
- AgentLoader: discovery, duplicate rejection, hot-reload, stop_all
- AgentRuntime: full start/stop, dispatch, request, wire, reload
- Integration: multi-agent bus routing, task-based routing
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from neuralis.agents.base import (
    AgentMessage, AgentMeta, AgentResponse, AgentState,
    BaseAgent, ResponseStatus,
)
from neuralis.agents.bus import AgentBus
from neuralis.agents.inference import InferenceEngine, InferenceRequest, InferenceResult
from neuralis.agents.loader import AgentLoadError, AgentLoader
from neuralis.agents.runtime import AgentRuntime
from neuralis.config import NodeConfig
from neuralis.identity import NodeIdentity


# ===========================================================================
# Helpers
# ===========================================================================

def make_node(tmp_path: Path):
    kd = tmp_path / "identity"
    ident = NodeIdentity.create_new(key_dir=kd)
    cfg = NodeConfig.defaults()
    cfg.identity.key_dir = str(kd)
    cfg.agents.agents_dir = str(tmp_path / "agents")
    cfg.agents.models_dir = str(tmp_path / "models")
    cfg.agents.default_model = None
    cfg.agents.enable_auto_discover = False
    cfg.logging.log_dir = str(tmp_path / "logs")
    node = MagicMock()
    node.identity = ident
    node.config = cfg
    node.register_subsystem = MagicMock()
    node.on_shutdown = MagicMock()
    return node


def make_message(target="echo", task="echo", payload=None) -> AgentMessage:
    return AgentMessage(
        target=target,
        task=task,
        payload=payload or {"text": "hello"},
    )


class EchoAgent(BaseAgent):
    NAME = "echo"
    VERSION = "1.0.0"
    DESCRIPTION = "Echoes payload back"
    CAPABILITIES = ["echo", "ping"]
    REQUIRED_MODEL = None

    async def handle(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.ok(
            request_id=message.message_id,
            agent=self.NAME,
            data={"echo": message.payload},
        )


class SlowAgent(BaseAgent):
    NAME = "slow"
    CAPABILITIES = ["slow_task"]

    async def handle(self, message: AgentMessage) -> AgentResponse:
        await asyncio.sleep(0.05)
        return AgentResponse.ok(request_id=message.message_id, agent=self.NAME)


class ErrorAgent(BaseAgent):
    NAME = "error_agent"
    CAPABILITIES = ["fail"]

    async def handle(self, message: AgentMessage) -> AgentResponse:
        raise ValueError("intentional test error")


class ModelAgent(BaseAgent):
    NAME = "model_agent"
    CAPABILITIES = ["infer"]
    REQUIRED_MODEL = "test-model.gguf"

    async def handle(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.ok(request_id=message.message_id, agent=self.NAME)


# ===========================================================================
# 1. AgentMessage
# ===========================================================================

class TestAgentMessage:
    def test_defaults(self):
        msg = AgentMessage(target="echo", task="ping")
        assert msg.target == "echo"
        assert msg.task == "ping"
        assert msg.sender_id == ""
        assert msg.reply_to == ""
        assert msg.ttl == 8
        assert len(msg.message_id) == 36  # UUID4
        assert msg.timestamp <= time.time()

    def test_unique_ids(self):
        m1 = AgentMessage(target="a", task="b")
        m2 = AgentMessage(target="a", task="b")
        assert m1.message_id != m2.message_id

    def test_to_dict_round_trip(self):
        msg = AgentMessage(target="echo", task="ping", payload={"x": 1})
        d = msg.to_dict()
        restored = AgentMessage.from_dict(d)
        assert restored.message_id == msg.message_id
        assert restored.target == msg.target
        assert restored.task == msg.task
        assert restored.payload == msg.payload
        assert restored.ttl == msg.ttl

    def test_from_dict_defaults(self):
        d = {"message_id": str(uuid.uuid4()), "target": "x", "task": "y"}
        msg = AgentMessage.from_dict(d)
        assert msg.sender_id == ""
        assert msg.reply_to == ""
        assert msg.ttl == 8

    def test_is_expired_false_new(self):
        msg = AgentMessage(target="a", task="b")
        assert not msg.is_expired()

    def test_is_expired_true_old(self):
        msg = AgentMessage(target="a", task="b")
        msg.timestamp = time.time() - 61
        assert msg.is_expired()

    def test_repr(self):
        msg = AgentMessage(target="echo", task="ping")
        r = repr(msg)
        assert "echo" in r
        assert "ping" in r

    def test_payload_defaults_to_dict(self):
        msg = AgentMessage(target="a", task="b")
        assert isinstance(msg.payload, dict)

    def test_custom_sender(self):
        msg = AgentMessage(target="a", task="b", sender_id="NRL1abc")
        assert msg.sender_id == "NRL1abc"

    def test_reply_to(self):
        original = AgentMessage(target="a", task="b")
        reply = AgentMessage(target="a", task="b", reply_to=original.message_id)
        assert reply.reply_to == original.message_id


# ===========================================================================
# 2. AgentResponse
# ===========================================================================

class TestAgentResponse:
    def test_ok_factory(self):
        r = AgentResponse.ok("req1", "echo", data={"x": 1})
        assert r.status == ResponseStatus.OK
        assert r.is_ok()
        assert r.data == {"x": 1}
        assert r.error == ""

    def test_error_factory(self):
        r = AgentResponse.from_error("req1", "echo", error="something went wrong")
        assert r.status == ResponseStatus.ERROR
        assert not r.is_ok()
        assert r.error == "something went wrong"

    def test_pending_factory(self):
        r = AgentResponse.pending("req1", "echo")
        assert r.status == ResponseStatus.PENDING

    def test_to_dict(self):
        r = AgentResponse.ok("req1", "echo", data={"k": "v"})
        d = r.to_dict()
        assert d["request_id"] == "req1"
        assert d["agent"] == "echo"
        assert d["status"] == "ok"
        assert d["data"] == {"k": "v"}

    def test_repr(self):
        r = AgentResponse.ok("req123456789", "echo")
        assert "echo" in repr(r)
        assert "ok" in repr(r)

    def test_duration_ms(self):
        r = AgentResponse.ok("x", "y", duration_ms=42.5)
        assert r.duration_ms == 42.5


# ===========================================================================
# 3. AgentMeta
# ===========================================================================

class TestAgentMeta:
    def test_to_dict(self):
        meta = AgentMeta(name="echo", version="1.0.0", capabilities=["echo"])
        d = meta.to_dict()
        assert d["name"] == "echo"
        assert d["capabilities"] == ["echo"]
        assert d["required_model"] is None

    def test_defaults(self):
        meta = AgentMeta(name="x")
        assert meta.version == "1.0.0"
        assert meta.capabilities == []
        assert meta.author == ""


# ===========================================================================
# 4. BaseAgent
# ===========================================================================

class TestBaseAgent:
    def setup_method(self):
        self.node = MagicMock()
        self.config = NodeConfig.defaults().agents

    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        agent = EchoAgent(self.node, self.config)
        assert agent.state == AgentState.IDLE
        await agent.start()
        assert agent.state == AgentState.RUNNING
        assert agent.is_running

    @pytest.mark.asyncio
    async def test_stop_sets_stopped(self):
        agent = EchoAgent(self.node, self.config)
        await agent.start()
        await agent.stop()
        assert agent.state == AgentState.STOPPED
        assert not agent.is_running

    @pytest.mark.asyncio
    async def test_handle_returns_response(self):
        agent = EchoAgent(self.node, self.config)
        await agent.start()
        msg = make_message()
        resp = await agent.handle(msg)
        assert resp.is_ok()
        assert resp.agent == "echo"
        assert resp.data["echo"] == msg.payload

    def test_can_handle(self):
        agent = EchoAgent(self.node, self.config)
        assert agent.can_handle("echo")
        assert agent.can_handle("ping")
        assert not agent.can_handle("summarise")

    def test_meta(self):
        agent = EchoAgent(self.node, self.config)
        meta = agent.meta
        assert meta.name == "echo"
        assert "echo" in meta.capabilities
        assert meta.required_model is None

    def test_stats(self):
        agent = EchoAgent(self.node, self.config)
        s = agent.stats()
        assert s["name"] == "echo"
        assert s["handled"] == 0
        assert s["errors"] == 0

    def test_record_handled(self):
        agent = EchoAgent(self.node, self.config)
        agent._record_handled()
        agent._record_handled()
        assert agent.stats()["handled"] == 2

    def test_record_error(self):
        agent = EchoAgent(self.node, self.config)
        agent._record_error()
        assert agent.stats()["errors"] == 1

    def test_repr(self):
        agent = EchoAgent(self.node, self.config)
        assert "EchoAgent" in repr(agent)
        assert "echo" in repr(agent)

    def test_required_model_none(self):
        agent = EchoAgent(self.node, self.config)
        assert agent.REQUIRED_MODEL is None

    def test_required_model_set(self):
        agent = ModelAgent(self.node, self.config)
        assert agent.REQUIRED_MODEL == "test-model.gguf"


# ===========================================================================
# 5. AgentBus
# ===========================================================================

class TestAgentBus:
    def setup_method(self):
        self.bus = AgentBus()

    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscriber(self):
        received = []

        async def handler(msg):
            received.append(msg)
            return AgentResponse.ok(msg.message_id, "test")

        self.bus.subscribe("echo", handler, "sub1")
        msg = make_message("echo", "echo")
        responses = await self.bus.publish(msg)
        assert len(received) == 1
        assert received[0].message_id == msg.message_id
        assert len(responses) == 1

    @pytest.mark.asyncio
    async def test_publish_no_subscriber_dead_letters(self):
        msg = make_message("nonexistent", "task")
        responses = await self.bus.publish(msg)
        assert responses == []
        assert len(self.bus.dead_letters()) == 1
        assert self.bus.stats()["dead_letters"] == 1

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self):
        received_a, received_b = [], []

        async def ha(msg): received_a.append(msg); return None
        async def hb(msg): received_b.append(msg); return None

        self.bus.subscribe("echo", ha, "a")
        self.bus.subscribe("echo", hb, "b")
        await self.bus.publish(make_message("echo", "echo"))
        assert len(received_a) == 1
        assert len(received_b) == 1

    @pytest.mark.asyncio
    async def test_broadcast_delivers_to_all(self):
        received = []

        async def h(msg): received.append(msg)

        self.bus.subscribe("broadcast", h, "listener")
        msg = make_message("echo", "echo")  # target != broadcast
        await self.bus.publish(msg)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_wildcard_receives_all(self):
        received = []

        async def h(msg): received.append(msg)

        self.bus.subscribe("*", h, "wildcard")
        await self.bus.publish(make_message("echo", "echo"))
        await self.bus.publish(make_message("summarise", "summarise"))
        assert len(received) == 2

    def test_unsubscribe(self):
        async def h(msg): pass
        self.bus.subscribe("echo", h, "sub1")
        assert self.bus.subscriber_count("echo") == 1
        removed = self.bus.unsubscribe("echo", "sub1")
        assert removed
        assert self.bus.subscriber_count("echo") == 0

    def test_unsubscribe_nonexistent(self):
        removed = self.bus.unsubscribe("echo", "ghost")
        assert not removed

    def test_unsubscribe_all(self):
        async def h(msg): pass
        self.bus.subscribe("echo", h, "myagent")
        self.bus.subscribe("ping", h, "myagent")
        count = self.bus.unsubscribe_all("myagent")
        assert count == 2

    @pytest.mark.asyncio
    async def test_expired_message_dropped(self):
        received = []
        async def h(msg): received.append(msg)
        self.bus.subscribe("echo", h, "s")
        msg = make_message("echo", "echo")
        msg.timestamp = time.time() - 61
        await self.bus.publish(msg)
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_request_returns_response(self):
        async def h(msg):
            return AgentResponse.ok(msg.message_id, "echo", data={"ok": True})
        self.bus.subscribe("echo", h, "echo")
        msg = make_message("echo", "echo")
        resp = await self.bus.request("echo", msg, timeout=2.0)
        assert resp is not None
        assert resp.is_ok()

    @pytest.mark.asyncio
    async def test_request_timeout(self):
        async def slow(msg):
            await asyncio.sleep(5)
            return None
        self.bus.subscribe("slow", slow, "slow")
        msg = make_message("slow", "task")
        with pytest.raises(asyncio.TimeoutError):
            await self.bus.request("slow", msg, timeout=0.05)

    @pytest.mark.asyncio
    async def test_request_no_handler_returns_none(self):
        msg = make_message("ghost", "task")
        resp = await self.bus.request("ghost", msg)
        assert resp is None

    @pytest.mark.asyncio
    async def test_handler_exception_doesnt_crash_bus(self):
        async def bad(msg): raise RuntimeError("boom")
        self.bus.subscribe("echo", bad, "bad")
        await self.bus.publish(make_message("echo", "echo"))
        # No exception propagated

    def test_stats(self):
        s = self.bus.stats()
        assert s["published"] == 0
        assert s["delivered"] == 0
        assert "topics" in s

    def test_drain_dead_letters(self):
        self.bus._dead_letters.append(make_message("x", "y"))
        msgs = self.bus.drain_dead_letters()
        assert len(msgs) == 1
        assert len(self.bus.dead_letters()) == 0

    def test_reset(self):
        async def h(msg): pass
        self.bus.subscribe("echo", h, "s")
        self.bus._published = 5
        self.bus.reset()
        assert self.bus.subscriber_count("echo") == 0
        assert self.bus.stats()["published"] == 0

    def test_repr(self):
        assert "AgentBus" in repr(self.bus)

    @pytest.mark.asyncio
    async def test_none_return_not_in_responses(self):
        async def h(msg): return None
        self.bus.subscribe("echo", h, "s")
        responses = await self.bus.publish(make_message("echo", "echo"))
        assert responses == []

    def test_topics_returns_active(self):
        async def h(msg): pass
        self.bus.subscribe("echo", h, "s")
        assert "echo" in self.bus.topics()


# ===========================================================================
# 6. InferenceEngine (stub mode — no llama_cpp required)
# ===========================================================================

class TestInferenceEngine:
    def setup_method(self):
        self.config = NodeConfig.defaults().agents
        self.config.inference_threads = 1

    @pytest.mark.asyncio
    async def test_not_loaded_initially(self):
        engine = InferenceEngine(self.config)
        assert not engine.is_loaded

    @pytest.mark.asyncio
    async def test_load_stub_mode(self, tmp_path):
        # In stub mode (no llama_cpp), load() succeeds without a real file
        engine = InferenceEngine(self.config)
        self.config.models_dir = str(tmp_path / "models")
        # Patch _LLAMA_AVAILABLE to False to force stub mode
        import neuralis.agents.inference as inf_mod
        original = inf_mod._LLAMA_AVAILABLE
        inf_mod._LLAMA_AVAILABLE = False
        try:
            await engine.load("fake-model.gguf")
            assert engine.is_loaded
            assert engine.model_name == "fake-model.gguf"
        finally:
            inf_mod._LLAMA_AVAILABLE = original
            await engine.unload()

    @pytest.mark.asyncio
    async def test_complete_without_load_returns_error(self):
        engine = InferenceEngine(self.config)
        result = await engine.complete(InferenceRequest(prompt="hello"))
        assert not result.is_ok()
        assert "not loaded" in result.error

    @pytest.mark.asyncio
    async def test_complete_stub_returns_text(self, tmp_path):
        import neuralis.agents.inference as inf_mod
        engine = InferenceEngine(self.config)
        self.config.models_dir = str(tmp_path)
        original = inf_mod._LLAMA_AVAILABLE
        inf_mod._LLAMA_AVAILABLE = False
        try:
            await engine.load("stub.gguf")
            result = await engine.complete(InferenceRequest(prompt="test prompt"))
            assert result.is_ok()
            assert len(result.text) > 0
            assert result.model_name == "stub.gguf"
        finally:
            inf_mod._LLAMA_AVAILABLE = original
            await engine.unload()

    @pytest.mark.asyncio
    async def test_complete_text_convenience(self, tmp_path):
        import neuralis.agents.inference as inf_mod
        engine = InferenceEngine(self.config)
        original = inf_mod._LLAMA_AVAILABLE
        inf_mod._LLAMA_AVAILABLE = False
        try:
            await engine.load("stub.gguf")
            text = await engine.complete_text("hello world")
            assert isinstance(text, str)
        finally:
            inf_mod._LLAMA_AVAILABLE = original
            await engine.unload()

    @pytest.mark.asyncio
    async def test_unload_clears_state(self, tmp_path):
        import neuralis.agents.inference as inf_mod
        engine = InferenceEngine(self.config)
        original = inf_mod._LLAMA_AVAILABLE
        inf_mod._LLAMA_AVAILABLE = False
        try:
            await engine.load("stub.gguf")
            assert engine.is_loaded
        finally:
            inf_mod._LLAMA_AVAILABLE = original
        await engine.unload()
        assert not engine.is_loaded
        assert engine.model_name == ""

    @pytest.mark.asyncio
    async def test_model_not_found_raises(self, tmp_path):
        import neuralis.agents.inference as inf_mod
        if not inf_mod._LLAMA_AVAILABLE:
            pytest.skip("stub mode doesn't raise FileNotFoundError")
        engine = InferenceEngine(self.config)
        self.config.models_dir = str(tmp_path)
        with pytest.raises(FileNotFoundError):
            await engine.load("nonexistent.gguf")

    def test_stats(self):
        engine = InferenceEngine(self.config)
        s = engine.stats()
        assert "loaded" in s
        assert "model" in s
        assert "total_calls" in s

    def test_repr(self):
        engine = InferenceEngine(self.config)
        assert "InferenceEngine" in repr(engine)

    @pytest.mark.asyncio
    async def test_inference_request_fields(self):
        req = InferenceRequest(prompt="test", max_tokens=100, temperature=0.5)
        assert req.prompt == "test"
        assert req.max_tokens == 100
        assert req.temperature == 0.5
        assert req.stop == []

    def test_inference_result_fields(self):
        r = InferenceResult(text="hello", model_name="m.gguf")
        assert r.is_ok()
        assert r.error == ""
        d = r.to_dict()
        assert d["text"] == "hello"


# ===========================================================================
# 7. AgentLoader
# ===========================================================================

class TestAgentLoader:
    def setup_method(self):
        pass

    @pytest.mark.asyncio
    async def test_empty_dir_loads_nothing(self, tmp_path):
        node = make_node(tmp_path)
        node.config.agents.agents_dir = str(tmp_path / "agents")
        (tmp_path / "agents").mkdir()
        loader = AgentLoader(node, node.config.agents)
        result = await loader.discover()
        assert result == []
        assert loader.count() == 0

    @pytest.mark.asyncio
    async def test_nonexistent_dir_loads_nothing(self, tmp_path):
        node = make_node(tmp_path)
        node.config.agents.agents_dir = str(tmp_path / "no_such_dir")
        loader = AgentLoader(node, node.config.agents)
        result = await loader.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_loads_valid_agent_file(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)

        (agents_dir / "my_echo.py").write_text(
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class MyEcho(BaseAgent):\n"
            "    NAME = 'my_echo'\n"
            "    CAPABILITIES = ['echo']\n"
            "    async def handle(self, msg):\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME)\n"
        )

        loader = AgentLoader(node, node.config.agents)
        loaded = await loader.discover()
        assert len(loaded) == 1
        assert loader.get("my_echo") is not None
        assert loader.count() == 1

    @pytest.mark.asyncio
    async def test_skips_underscore_files(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        (agents_dir / "_private.py").write_text("# private\n")
        loader = AgentLoader(node, node.config.agents)
        await loader.discover()
        assert loader.count() == 0

    @pytest.mark.asyncio
    async def test_rejects_duplicate_name(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)

        agent_code = (
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class A{n}(BaseAgent):\n"
            "    NAME = 'dup_agent'\n"
            "    CAPABILITIES = []\n"
            "    async def handle(self, msg):\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME)\n"
        )
        (agents_dir / "dup_a.py").write_text(agent_code.format(n="1"))
        (agents_dir / "dup_b.py").write_text(agent_code.format(n="2"))

        loader = AgentLoader(node, node.config.agents)
        await loader.discover()
        assert loader.count() == 1  # second duplicate silently skipped

    @pytest.mark.asyncio
    async def test_invalid_file_skipped(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        (agents_dir / "bad.py").write_text("this is not valid python )()(")
        loader = AgentLoader(node, node.config.agents)
        loaded = await loader.discover()
        assert loaded == []
        assert loader.count() == 0

    @pytest.mark.asyncio
    async def test_no_base_agent_subclass_skipped(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        (agents_dir / "notanagent.py").write_text("class NotAnAgent:\n    pass\n")
        loader = AgentLoader(node, node.config.agents)
        loaded = await loader.discover()
        assert loaded == []

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, tmp_path):
        node = make_node(tmp_path)
        loader = AgentLoader(node, node.config.agents)
        assert loader.get("ghost") is None

    @pytest.mark.asyncio
    async def test_names_returns_list(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        (agents_dir / "e.py").write_text(
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class E(BaseAgent):\n"
            "    NAME = 'e_agent'\n"
            "    CAPABILITIES = ['e']\n"
            "    async def handle(self, msg):\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME)\n"
        )
        loader = AgentLoader(node, node.config.agents)
        await loader.discover()
        assert "e_agent" in loader.names()

    @pytest.mark.asyncio
    async def test_agents_for_task(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        (agents_dir / "t.py").write_text(
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class T(BaseAgent):\n"
            "    NAME = 't_agent'\n"
            "    CAPABILITIES = ['special_task']\n"
            "    async def handle(self, msg):\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME)\n"
        )
        loader = AgentLoader(node, node.config.agents)
        await loader.discover()
        matches = loader.agents_for_task("special_task")
        assert len(matches) == 1
        assert matches[0].NAME == "t_agent"
        assert loader.agents_for_task("other_task") == []

    @pytest.mark.asyncio
    async def test_stop_all(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        (agents_dir / "s.py").write_text(
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class S(BaseAgent):\n"
            "    NAME = 's_agent'\n"
            "    CAPABILITIES = []\n"
            "    async def handle(self, msg):\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME)\n"
        )
        loader = AgentLoader(node, node.config.agents)
        await loader.discover()
        assert loader.count() == 1
        await loader.stop_all()
        assert loader.count() == 0

    @pytest.mark.asyncio
    async def test_hot_reload_detects_new_file(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        loader = AgentLoader(node, node.config.agents)
        await loader.discover()
        assert loader.count() == 0

        (agents_dir / "new_agent.py").write_text(
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class NewAgent(BaseAgent):\n"
            "    NAME = 'new_agent'\n"
            "    CAPABILITIES = ['new']\n"
            "    async def handle(self, msg):\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME)\n"
        )
        result = await loader.reload()
        assert "new_agent" in result["added"]
        assert loader.count() == 1

    def test_repr(self, tmp_path):
        node = make_node(tmp_path)
        loader = AgentLoader(node, node.config.agents)
        assert "AgentLoader" in repr(loader)


# ===========================================================================
# 8. AgentRuntime
# ===========================================================================

class TestAgentRuntime:
    @pytest.mark.asyncio
    async def test_start_stop(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()
        assert runtime._running
        node.register_subsystem.assert_called_once_with("agents", runtime)
        node.on_shutdown.assert_called_once()
        await runtime.stop()
        assert not runtime._running

    @pytest.mark.asyncio
    async def test_double_start_idempotent(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()
        await runtime.start()  # should not raise or double-register
        assert node.register_subsystem.call_count == 1
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_dispatch_not_running_raises(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        with pytest.raises(RuntimeError, match="not running"):
            await runtime.dispatch(make_message())

    @pytest.mark.asyncio
    async def test_dispatch_delivers_to_agent(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        # Manually inject an echo agent
        agent = EchoAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["echo"] = agent
        runtime._wire_agent(agent)

        msg = make_message("echo", "echo", {"text": "hi"})
        responses = await runtime.dispatch(msg)
        assert len(responses) >= 1
        assert responses[0].is_ok()

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_request_gets_response(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        agent = EchoAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["echo"] = agent
        runtime._wire_agent(agent)

        msg = make_message("echo", "echo")
        resp = await runtime.request("echo", msg, timeout=2.0)
        assert resp is not None
        assert resp.is_ok()

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_error_agent_returns_error_response(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        agent = ErrorAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["error_agent"] = agent
        runtime._wire_agent(agent)

        msg = make_message("error_agent", "fail")
        responses = await runtime.dispatch(msg)
        assert len(responses) >= 1
        assert not responses[0].is_ok()
        assert "intentional" in responses[0].error

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_get_agent(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        agent = EchoAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["echo"] = agent

        assert runtime.get_agent("echo") is agent
        assert runtime.get_agent("ghost") is None
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_all_agents(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        agent = EchoAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["echo"] = agent

        agents = runtime.all_agents()
        assert len(agents) == 1
        assert agents[0].NAME == "echo"
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_agents_for_task(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        agent = EchoAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["echo"] = agent

        assert len(runtime.agents_for_task("echo")) == 1
        assert len(runtime.agents_for_task("unknown")) == 0
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_status(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()
        s = runtime.status()
        assert s["running"] is True
        assert "agents" in s
        assert "bus" in s
        assert "engine" in s
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_bus(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()
        agent = EchoAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["echo"] = agent
        runtime._wire_agent(agent)
        assert runtime.bus.subscriber_count("echo") > 0
        await runtime.stop()
        assert runtime.bus.subscriber_count("echo") == 0

    @pytest.mark.asyncio
    async def test_reload_adds_agent(self, tmp_path):
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        node.config.agents.enable_auto_discover = True

        runtime = AgentRuntime(node)
        await runtime.start()
        assert runtime.loader.count() == 0

        (agents_dir / "hot.py").write_text(
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class HotAgent(BaseAgent):\n"
            "    NAME = 'hot_agent'\n"
            "    CAPABILITIES = ['hot']\n"
            "    async def handle(self, msg):\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME)\n"
        )
        result = await runtime.reload_agents()
        assert "hot_agent" in result["added"]
        assert runtime.loader.count() == 1

        # After reload, the agent should be wired on the bus
        msg = make_message("hot_agent", "hot")
        responses = await runtime.dispatch(msg)
        assert len(responses) >= 1

        await runtime.stop()

    def test_repr(self, tmp_path):
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        assert "AgentRuntime" in repr(runtime)


# ===========================================================================
# 9. Integration
# ===========================================================================

class TestIntegration:
    @pytest.mark.asyncio
    async def test_multi_agent_task_routing(self, tmp_path):
        """Two agents subscribed to different tasks — messages route correctly."""
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        echo = EchoAgent(node, node.config.agents)
        await echo.start()
        runtime.loader._agents["echo"] = echo
        runtime._wire_agent(echo)

        slow = SlowAgent(node, node.config.agents)
        await slow.start()
        runtime.loader._agents["slow"] = slow
        runtime._wire_agent(slow)

        echo_msg = make_message("echo", "echo")
        slow_msg = make_message("slow", "slow_task")

        echo_resp = await runtime.dispatch(echo_msg)
        slow_resp = await runtime.dispatch(slow_msg)

        assert echo_resp[0].agent == "echo"
        assert slow_resp[0].agent == "slow"

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_capability_routing_via_bus(self, tmp_path):
        """
        Subscribing to a capability topic routes messages to agents that
        declare that capability.
        """
        bus = AgentBus()
        node = MagicMock()
        config = NodeConfig.defaults().agents

        agent = EchoAgent(node, config)
        await agent.start()

        # Subscribe agent's handle to its capability topic
        async def handler(msg):
            return await agent.handle(msg)

        bus.subscribe("ping", handler, "echo@ping")

        msg = make_message(target="ping", task="ping")
        responses = await bus.publish(msg)
        assert len(responses) == 1
        assert responses[0].is_ok()

    @pytest.mark.asyncio
    async def test_agent_stat_tracking(self, tmp_path):
        """handled / error counters increment correctly through runtime."""
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        agent = EchoAgent(node, node.config.agents)
        await agent.start()
        runtime.loader._agents["echo"] = agent
        runtime._wire_agent(agent)

        for _ in range(3):
            await runtime.dispatch(make_message("echo", "echo"))

        # One subscription per topic (echo, ping), so echo topic fires 1x per dispatch
        # but agent.handled count is tracked by _record_handled in the handler wrapper
        assert agent.stats()["handled"] >= 3

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_full_pipeline_from_file(self, tmp_path):
        """Load an agent from a file, dispatch to it, verify response end-to-end."""
        node = make_node(tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        node.config.agents.agents_dir = str(agents_dir)
        node.config.agents.enable_auto_discover = True

        (agents_dir / "pipeline_agent.py").write_text(
            "from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse\n"
            "class PipelineAgent(BaseAgent):\n"
            "    NAME = 'pipeline'\n"
            "    CAPABILITIES = ['process']\n"
            "    async def handle(self, msg):\n"
            "        result = str(msg.payload).upper()\n"
            "        return AgentResponse.ok(msg.message_id, self.NAME, data={'result': result})\n"
        )

        runtime = AgentRuntime(node)
        await runtime.start()

        assert runtime.loader.count() == 1
        msg = make_message("pipeline", "process", {"text": "hello"})
        responses = await runtime.dispatch(msg)
        assert len(responses) >= 1
        assert responses[0].is_ok()
        assert "HELLO" in str(responses[0].data["result"])

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_inference_engine_accessible_from_agent(self, tmp_path):
        """An agent can access the InferenceEngine through the runtime."""
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        engine = runtime.engine
        assert engine is not None
        assert not engine.is_loaded   # no model configured

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_bus_dead_letter_on_unroutable(self, tmp_path):
        """Messages to agents that don't exist end up in the dead-letter queue."""
        node = make_node(tmp_path)
        runtime = AgentRuntime(node)
        await runtime.start()

        msg = make_message("nonexistent_agent", "task")
        responses = await runtime.dispatch(msg)
        assert responses == []
        assert runtime.bus.stats()["dead_letters"] >= 1

        await runtime.stop()
