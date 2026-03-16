"""
neuralis.agents.base
====================
Base class and data structures for all Neuralis agents.

Every agent in the system — whether it summarises text, routes messages,
answers questions, or manages storage — inherits from ``BaseAgent``.

An agent is a self-contained unit of AI capability that:
  - Declares its own metadata (name, version, capabilities, model requirements)
  - Responds to ``AgentMessage`` objects via ``handle()``
  - Manages its own lifecycle via ``start()`` / ``stop()``
  - Runs entirely locally — no external API calls ever

Plugin contract
---------------
To create an agent plugin, drop a .py file into the ``agents/`` directory
that contains exactly one class inheriting from ``BaseAgent``.  The
``AgentLoader`` in ``loader.py`` will find and instantiate it automatically.

Example plugin (agents/echo_agent.py):

    from neuralis.agents.base import BaseAgent, AgentMessage, AgentResponse

    class EchoAgent(BaseAgent):
        NAME = "echo"
        VERSION = "1.0.0"
        CAPABILITIES = ["echo", "ping"]
        REQUIRED_MODEL = None

        async def handle(self, message: AgentMessage) -> AgentResponse:
            return AgentResponse.ok(
                request_id=message.message_id,
                agent=self.NAME,
                data={"echo": message.payload},
            )
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# AgentState
# ---------------------------------------------------------------------------

class AgentState(str, Enum):
    IDLE      = "idle"
    STARTING  = "starting"
    RUNNING   = "running"
    BUSY      = "busy"
    STOPPING  = "stopping"
    STOPPED   = "stopped"
    ERROR     = "error"


# ---------------------------------------------------------------------------
# AgentMessage
# ---------------------------------------------------------------------------

@dataclass
class AgentMessage:
    """
    A single task or query delivered to an agent.

    Attributes
    ----------
    message_id  : unique ID for this message (UUID4 string)
    sender_id   : node_id of the originating node (empty string = local)
    target      : agent name this message is addressed to
    task        : short task identifier (e.g. "summarise", "search", "ping")
    payload     : arbitrary task data
    reply_to    : message_id this is a reply to (empty string = new request)
    timestamp   : Unix time the message was created
    ttl         : hops remaining before the message is discarded
    """
    target:     str
    task:       str
    payload:    Any   = field(default_factory=dict)
    sender_id:  str   = ""
    reply_to:   str   = ""
    ttl:        int   = 8
    message_id: str   = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:  float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        """True if the message is older than 60 seconds."""
        return (time.time() - self.timestamp) > 60.0

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "sender_id":  self.sender_id,
            "target":     self.target,
            "task":       self.task,
            "payload":    self.payload,
            "reply_to":   self.reply_to,
            "timestamp":  self.timestamp,
            "ttl":        self.ttl,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentMessage":
        return cls(
            message_id = d["message_id"],
            sender_id  = d.get("sender_id", ""),
            target     = d["target"],
            task       = d["task"],
            payload    = d.get("payload", {}),
            reply_to   = d.get("reply_to", ""),
            timestamp  = d.get("timestamp", time.time()),
            ttl        = d.get("ttl", 8),
        )

    def __repr__(self) -> str:
        return (
            f"<AgentMessage id={self.message_id[:8]} "
            f"target={self.target} task={self.task}>"
        )


# ---------------------------------------------------------------------------
# AgentResponse
# ---------------------------------------------------------------------------

class ResponseStatus(str, Enum):
    OK      = "ok"
    ERROR   = "error"
    PENDING = "pending"


@dataclass
class AgentResponse:
    """
    The result of an agent handling an AgentMessage.

    Attributes
    ----------
    request_id  : message_id of the AgentMessage this responds to
    agent       : name of the agent that produced this response
    status      : ok / error / pending
    data        : response payload (task-dependent)
    error       : error message if status == error
    duration_ms : wall-clock milliseconds the handler took
    timestamp   : Unix time this response was created
    """
    request_id:  str
    agent:       str
    status:      ResponseStatus = ResponseStatus.OK
    data:        Any            = field(default_factory=dict)
    error:       str            = ""
    duration_ms: float          = 0.0
    timestamp:   float          = field(default_factory=time.time)

    @classmethod
    def ok(cls, request_id: str, agent: str, data: Any = None, duration_ms: float = 0.0) -> "AgentResponse":
        return cls(request_id=request_id, agent=agent, status=ResponseStatus.OK,
                   data=data or {}, duration_ms=duration_ms)

    @classmethod
    def from_error(cls, request_id: str, agent: str, error: str, duration_ms: float = 0.0) -> "AgentResponse":
        return cls(request_id=request_id, agent=agent, status=ResponseStatus.ERROR,
                   error=error, duration_ms=duration_ms)

    @classmethod
    def pending(cls, request_id: str, agent: str) -> "AgentResponse":
        return cls(request_id=request_id, agent=agent, status=ResponseStatus.PENDING)

    def is_ok(self) -> bool:
        return self.status == ResponseStatus.OK

    def to_dict(self) -> dict:
        return {
            "request_id":  self.request_id,
            "agent":       self.agent,
            "status":      self.status.value,
            "data":        self.data,
            "error":       self.error,
            "duration_ms": self.duration_ms,
            "timestamp":   self.timestamp,
        }

    def __repr__(self) -> str:
        return (
            f"<AgentResponse req={self.request_id[:8]} "
            f"agent={self.agent} status={self.status.value}>"
        )


# ---------------------------------------------------------------------------
# AgentMeta
# ---------------------------------------------------------------------------

@dataclass
class AgentMeta:
    """Static metadata declared by each agent class."""
    name:           str
    version:        str           = "1.0.0"
    description:    str           = ""
    capabilities:   List[str]     = field(default_factory=list)
    required_model: Optional[str] = None
    author:         str           = ""

    def to_dict(self) -> dict:
        return {
            "name":           self.name,
            "version":        self.version,
            "description":    self.description,
            "capabilities":   self.capabilities,
            "required_model": self.required_model,
            "author":         self.author,
        }


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------

class BaseAgent(ABC):
    """
    Abstract base class for all Neuralis agents.

    Subclass this and implement ``handle()``.  Declare class-level
    NAME, VERSION, CAPABILITIES, and optionally REQUIRED_MODEL,
    DESCRIPTION, AUTHOR.

    Lifecycle
    ---------
    1. ``__init__(node, config)`` — called by AgentLoader at discovery
    2. ``start()``               — called when the runtime activates the agent
    3. ``handle(message)``       — called for each incoming AgentMessage
    4. ``stop()``                — called on node shutdown (LIFO)
    """

    NAME:           str           = "unnamed"
    VERSION:        str           = "1.0.0"
    DESCRIPTION:    str           = ""
    CAPABILITIES:   List[str]     = []
    REQUIRED_MODEL: Optional[str] = None
    AUTHOR:         str           = ""

    def __init__(self, node: Any, config: Any) -> None:
        self._node           = node
        self._config         = config
        self._state          = AgentState.IDLE
        self._started_at:    Optional[float] = None
        self._handled_count: int = 0
        self._error_count:   int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Called once when the agent is activated.
        Override to open resources, load models, start background tasks.
        Call ``await super().start()`` at the top of your override.
        """
        self._state = AgentState.RUNNING
        self._started_at = time.time()

    async def stop(self) -> None:
        """
        Called once on shutdown.
        Override to release resources.
        Call ``await super().stop()`` at the end of your override.
        """
        self._state = AgentState.STOPPED

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def handle(self, message: AgentMessage) -> AgentResponse:
        """
        Process one AgentMessage and return an AgentResponse.

        For long-running inference use asyncio.to_thread() or the
        InferenceEngine executor — never block the event loop.
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def can_handle(self, task: str) -> bool:
        """True if this agent declares it handles ``task``."""
        return task in self.CAPABILITIES

    @property
    def meta(self) -> AgentMeta:
        return AgentMeta(
            name           = self.NAME,
            version        = self.VERSION,
            description    = self.DESCRIPTION,
            capabilities   = list(self.CAPABILITIES),
            required_model = self.REQUIRED_MODEL,
            author         = self.AUTHOR,
        )

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state in (AgentState.RUNNING, AgentState.BUSY)

    def stats(self) -> dict:
        uptime = (time.time() - self._started_at) if self._started_at else 0.0
        return {
            "name":         self.NAME,
            "state":        self._state.value,
            "uptime_s":     round(uptime, 1),
            "handled":      self._handled_count,
            "errors":       self._error_count,
            "capabilities": self.CAPABILITIES,
        }

    def _record_handled(self) -> None:
        self._handled_count += 1

    def _record_error(self) -> None:
        self._error_count += 1

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.NAME} state={self._state.value}>"
