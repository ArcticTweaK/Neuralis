"""
neuralis.agents.runtime
=======================
AgentRuntime — the Module 4 subsystem registered on the Node.

The ``AgentRuntime`` is the single entry point for everything agent-related:
  - Owns the ``AgentLoader`` (plugin discovery)
  - Owns the ``InferenceEngine`` (local model inference)
  - Owns the ``AgentBus`` (intra-node pub/sub)
  - Integrates with the Node lifecycle (start / stop / register_subsystem)

Startup sequence
----------------
1. Create AgentBus
2. Create InferenceEngine; load default model if configured
3. Create AgentLoader; run discovery (scans agents_dir)
4. For each loaded agent: subscribe its handle() to the bus
5. Register self as "agents" subsystem on the Node
6. Register stop() as a shutdown callback

Usage
-----
    runtime = AgentRuntime(node)
    await runtime.start()

    # Dispatch a task to any agent that handles "echo"
    from neuralis.agents.base import AgentMessage
    msg = AgentMessage(target="echo", task="echo", payload={"text": "hello"})
    responses = await runtime.dispatch(msg)

    # Direct access
    agent = runtime.get_agent("echo")
    engine = runtime.engine

    await runtime.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from neuralis.agents.base import AgentMessage, AgentResponse, AgentState
from neuralis.agents.bus import AgentBus
from neuralis.agents.inference import InferenceEngine
from neuralis.agents.loader import AgentLoader

logger = logging.getLogger(__name__)


class AgentRuntime:
    """
    Coordinates all agent subsystems for a single Neuralis node.

    Parameters
    ----------
    node : neuralis.node.Node  — the running node (provides config + identity)
    """

    def __init__(self, node: Any) -> None:
        self._node    = node
        self._config  = node.config.agents
        self._running = False
        self._started_at: Optional[float] = None

        self.bus     = AgentBus()
        self.engine  = InferenceEngine(self._config)
        self.loader  = AgentLoader(node, self._config)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Full startup sequence.

        1. Load default model if ``config.default_model`` is set
        2. Discover and start all agent plugins
        3. Wire each agent onto the bus
        4. Register with the Node
        """
        if self._running:
            return

        logger.info("AgentRuntime: starting …")

        # 1. Load inference model if configured
        if self._config.default_model:
            try:
                await self.engine.load(self._config.default_model)
            except FileNotFoundError as exc:
                logger.warning("AgentRuntime: default model not found — %s", exc)
            except Exception as exc:
                logger.error("AgentRuntime: failed to load model — %s", exc)

        # 2. Discover agent plugins
        if self._config.enable_auto_discover:
            await self.loader.discover()

        # 3. Wire agents onto the bus
        self._wire_agents()

        # 4. Register with node
        self._node.register_subsystem("agents", self)
        self._node.on_shutdown(self.stop)

        self._running = True
        self._started_at = time.time()

        logger.info(
            "AgentRuntime: started — %d agent(s) loaded, model=%s",
            self.loader.count(),
            self.engine.model_name or "none",
        )

    async def stop(self) -> None:
        """Stop all agents and unload the model."""
        if not self._running:
            return

        logger.info("AgentRuntime: stopping …")
        self._running = False

        await self.loader.stop_all()
        self.bus.reset()
        await self.engine.unload()

        logger.info("AgentRuntime: stopped")

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, message: AgentMessage) -> List[AgentResponse]:
        """
        Route an ``AgentMessage`` through the bus.

        The bus delivers to all agents subscribed to ``message.target``.
        Agents that declare ``message.task`` in their CAPABILITIES are
        preferred; the bus handles routing automatically via subscriptions.

        Returns
        -------
        List[AgentResponse] — one response per subscribed handler
        """
        if not self._running:
            raise RuntimeError("AgentRuntime is not running — call start() first")

        return await self.bus.publish(message)

    async def request(
        self,
        target: str,
        message: AgentMessage,
        timeout: float = 10.0,
    ) -> Optional[AgentResponse]:
        """
        Send a message to a named agent and wait for its response.

        Parameters
        ----------
        target  : agent name
        message : AgentMessage
        timeout : seconds before asyncio.TimeoutError

        Returns
        -------
        AgentResponse | None
        """
        if not self._running:
            raise RuntimeError("AgentRuntime is not running — call start() first")

        return await self.bus.request(target, message, timeout=timeout)

    # ------------------------------------------------------------------
    # Agent access
    # ------------------------------------------------------------------

    def get_agent(self, name: str):
        """Return a loaded agent by name, or None."""
        return self.loader.get(name)

    def all_agents(self):
        """Return all loaded agents."""
        return self.loader.all_agents()

    def agents_for_task(self, task: str):
        """Return all agents that can handle ``task``."""
        return self.loader.agents_for_task(task)

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    async def reload_agents(self) -> dict:
        """
        Hot-reload agent plugins from disk.

        Newly added agents are started and wired onto the bus.
        Removed agents are stopped and unsubscribed.
        Changed agents are restarted.

        Returns
        -------
        dict with keys ``added``, ``updated``, ``removed``
        """
        result = await self.loader.reload()

        # Wire any newly added / updated agents
        for name in result["added"] + result["updated"]:
            agent = self.loader.get(name)
            if agent:
                self._wire_agent(agent)

        logger.info(
            "AgentRuntime: reload complete — added=%s updated=%s removed=%s",
            result["added"], result["updated"], result["removed"],
        )
        return result

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a serialisable status dict for the Canvas API."""
        uptime = (time.time() - self._started_at) if self._started_at else 0.0
        return {
            "running":    self._running,
            "uptime_s":   round(uptime, 1),
            "agents":     [a.stats() for a in self.loader.all_agents()],
            "agent_count": self.loader.count(),
            "bus":        self.bus.stats(),
            "engine":     self.engine.stats(),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _wire_agents(self) -> None:
        """Subscribe all loaded agents onto the bus."""
        for agent in self.loader.all_agents():
            self._wire_agent(agent)

    def _wire_agent(self, agent) -> None:
        """
        Subscribe a single agent's handle() method onto the bus.

        Each agent is subscribed to:
          - Its own name  (direct addressing)
          - Each of its declared CAPABILITIES  (task-based routing)
        """
        topics = set([agent.NAME] + list(agent.CAPABILITIES))
        for topic in topics:
            self.bus.subscribe(
                topic,
                self._make_handler(agent),
                subscriber_id=f"{agent.NAME}@{topic}",
            )
        logger.debug(
            "AgentRuntime: wired %s onto topics: %s",
            agent.NAME, sorted(topics),
        )

    def _make_handler(self, agent):
        """
        Return a closure that calls agent.handle() with timing + stat tracking.
        """
        async def _handler(message: AgentMessage) -> Optional[AgentResponse]:
            if not agent.is_running:
                logger.debug(
                    "AgentRuntime: agent %s not running, skipping message %s",
                    agent.NAME, message.message_id[:8],
                )
                return None

            t0 = time.monotonic()
            agent._state = AgentState.BUSY
            try:
                response = await agent.handle(message)
                agent._record_handled()
                if response is not None:
                    response.duration_ms = (time.monotonic() - t0) * 1000
                return response
            except Exception as exc:
                agent._record_error()
                logger.error(
                    "AgentRuntime: agent %s raised on message %s: %s",
                    agent.NAME, message.message_id[:8], exc,
                )
                return AgentResponse.from_error(
                    request_id=message.message_id,
                    agent=agent.NAME,
                    error=str(exc),
                    duration_ms=(time.monotonic() - t0) * 1000,
                )
            finally:
                if agent._state == AgentState.BUSY:
                    agent._state = AgentState.RUNNING

        return _handler

    def __repr__(self) -> str:
        return (
            f"<AgentRuntime running={self._running} "
            f"agents={self.loader.count()} "
            f"model={self.engine.model_name!r}>"
        )
