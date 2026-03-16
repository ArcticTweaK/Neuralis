"""
neuralis.protocol.messages
==========================
Inter-agent message format for the Neuralis protocol layer.

Module 5 sits between the mesh transport (Module 2) and the agent runtime
(Module 4).  It defines:

  - The canonical wire format for all agent-to-agent messages
  - AgentCapability — what a node advertises about its agents
  - ProtocolMessage — the payload that rides inside MessageEnvelope(AGENT_MSG)
  - Serialisation / deserialisation helpers
  - Protocol version negotiation

Wire format
-----------
All inter-node agent messages travel inside a ``MessageEnvelope`` from
Module 2 with ``type = AGENT_MSG``.  The envelope's ``payload`` field
contains a JSON-serialised ``ProtocolMessage``:

    MessageEnvelope(type=AGENT_MSG, payload={
        "proto_version": 1,
        "msg_type":      "TASK_REQUEST",
        "msg_id":        "<uuid4>",
        "session_id":    "<uuid4>",      # groups request + response
        "src_node":      "NRL1...",      # originating node_id
        "src_agent":     "summarise",    # originating agent name (or "")
        "dst_node":      "NRL1...",      # target node_id ("" = broadcast)
        "dst_agent":     "echo",         # target agent name  ("" = any)
        "task":          "echo",         # task identifier
        "payload":       { ... },        # task-specific data
        "reply_to":      "<uuid4>",      # msg_id this replies to ("" = new)
        "timestamp":     1234567890.0,
        "ttl":           8,
    })

Message types
-------------
TASK_REQUEST      — ask a remote agent to perform a task
TASK_RESPONSE     — successful response from a remote agent
TASK_ERROR        — error response from a remote agent
CAPABILITY_QUERY  — ask a node what agents / tasks it can handle
CAPABILITY_REPLY  — response listing agents and their capabilities
AGENT_ANNOUNCE    — a node broadcasts its available agents on joining
AGENT_WITHDRAW    — a node signals an agent has been removed
HEARTBEAT         — protocol-level keepalive (distinct from mesh PING)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION: int = 1
MESSAGE_TTL_SECS: int = 60   # messages older than this are silently dropped


# ---------------------------------------------------------------------------
# ProtocolMessageType
# ---------------------------------------------------------------------------

class ProtocolMessageType(str, Enum):
    TASK_REQUEST     = "TASK_REQUEST"
    TASK_RESPONSE    = "TASK_RESPONSE"
    TASK_ERROR       = "TASK_ERROR"
    CAPABILITY_QUERY = "CAPABILITY_QUERY"
    CAPABILITY_REPLY = "CAPABILITY_REPLY"
    AGENT_ANNOUNCE   = "AGENT_ANNOUNCE"
    AGENT_WITHDRAW   = "AGENT_WITHDRAW"
    HEARTBEAT        = "HEARTBEAT"


# ---------------------------------------------------------------------------
# AgentCapability — what a node advertises about one of its agents
# ---------------------------------------------------------------------------

@dataclass
class AgentCapability:
    """
    Describes one agent's capabilities as advertised to the mesh.

    Attributes
    ----------
    agent_name      : unique agent name (e.g. "summarise")
    version         : semver string
    tasks           : list of task strings this agent handles
    required_model  : model filename if inference is needed, else None
    """
    agent_name:     str
    version:        str           = "1.0.0"
    tasks:          List[str]     = field(default_factory=list)
    required_model: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "agent_name":     self.agent_name,
            "version":        self.version,
            "tasks":          list(self.tasks),
            "required_model": self.required_model,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentCapability":
        return cls(
            agent_name     = d["agent_name"],
            version        = d.get("version", "1.0.0"),
            tasks          = list(d.get("tasks", [])),
            required_model = d.get("required_model"),
        )

    def __repr__(self) -> str:
        return (
            f"<AgentCapability agent={self.agent_name!r} "
            f"tasks={self.tasks}>"
        )


# ---------------------------------------------------------------------------
# ProtocolMessage — the inter-node agent message
# ---------------------------------------------------------------------------

@dataclass
class ProtocolMessage:
    """
    A single inter-node agent protocol message.

    Serialised to JSON and placed into a
    ``MessageEnvelope(type=AGENT_MSG).payload`` before going on the wire.

    Attributes
    ----------
    msg_type      : ProtocolMessageType
    src_node      : NRL1... node_id of the sender
    dst_node      : NRL1... node_id of the target ("" = broadcast)
    src_agent     : name of the sending agent ("" = node-level message)
    dst_agent     : name of the target agent  ("" = any capable agent)
    task          : task identifier (e.g. "echo", "summarise")
    payload       : task-specific data dict
    reply_to      : msg_id this is a reply to ("" = new request)
    ttl           : hops remaining before the message is discarded
    msg_id        : UUID4 — unique per message
    session_id    : UUID4 — groups a request and its response(s)
    timestamp     : unix float — creation time
    proto_version : protocol version integer (currently 1)
    """
    msg_type:      ProtocolMessageType
    src_node:      str

    dst_node:      str            = ""
    src_agent:     str            = ""
    dst_agent:     str            = ""
    task:          str            = ""
    payload:       Dict[str, Any] = field(default_factory=dict)
    reply_to:      str            = ""
    ttl:           int            = 8

    msg_id:        str   = field(default_factory=lambda: str(uuid.uuid4()))
    session_id:    str   = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:     float = field(default_factory=time.time)
    proto_version: int   = PROTOCOL_VERSION

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def task_request(
        cls,
        src_node:  str,
        dst_node:  str,
        task:      str,
        payload:   Dict[str, Any],
        src_agent: str = "",
        dst_agent: str = "",
        ttl:       int = 8,
    ) -> "ProtocolMessage":
        """Create a TASK_REQUEST targeting a specific remote node/agent."""
        return cls(
            msg_type  = ProtocolMessageType.TASK_REQUEST,
            src_node  = src_node,
            dst_node  = dst_node,
            src_agent = src_agent,
            dst_agent = dst_agent,
            task      = task,
            payload   = payload,
            ttl       = ttl,
        )

    @classmethod
    def task_response(
        cls,
        src_node:   str,
        dst_node:   str,
        reply_to:   str,
        session_id: str,
        task:       str,
        payload:    Dict[str, Any],
        src_agent:  str = "",
        dst_agent:  str = "",
    ) -> "ProtocolMessage":
        """Create a TASK_RESPONSE in reply to a TASK_REQUEST."""
        return cls(
            msg_type   = ProtocolMessageType.TASK_RESPONSE,
            src_node   = src_node,
            dst_node   = dst_node,
            src_agent  = src_agent,
            dst_agent  = dst_agent,
            task       = task,
            payload    = payload,
            reply_to   = reply_to,
            session_id = session_id,
        )

    @classmethod
    def task_error(
        cls,
        src_node:   str,
        dst_node:   str,
        reply_to:   str,
        session_id: str,
        task:       str,
        error:      str,
        src_agent:  str = "",
    ) -> "ProtocolMessage":
        """Create a TASK_ERROR when an agent cannot fulfil a request."""
        return cls(
            msg_type   = ProtocolMessageType.TASK_ERROR,
            src_node   = src_node,
            dst_node   = dst_node,
            src_agent  = src_agent,
            task       = task,
            payload    = {"error": error},
            reply_to   = reply_to,
            session_id = session_id,
        )

    @classmethod
    def capability_query(
        cls,
        src_node: str,
        dst_node: str = "",
    ) -> "ProtocolMessage":
        """Ask a specific node (or broadcast) what agents it has."""
        return cls(
            msg_type = ProtocolMessageType.CAPABILITY_QUERY,
            src_node = src_node,
            dst_node = dst_node,
        )

    @classmethod
    def capability_reply(
        cls,
        src_node:     str,
        dst_node:     str,
        reply_to:     str,
        capabilities: List[AgentCapability],
    ) -> "ProtocolMessage":
        """Reply to a capability query with the local agent list."""
        return cls(
            msg_type = ProtocolMessageType.CAPABILITY_REPLY,
            src_node = src_node,
            dst_node = dst_node,
            reply_to = reply_to,
            payload  = {"capabilities": [c.to_dict() for c in capabilities]},
        )

    @classmethod
    def agent_announce(
        cls,
        src_node:     str,
        capabilities: List[AgentCapability],
    ) -> "ProtocolMessage":
        """Broadcast this node's agents to all mesh peers on joining."""
        return cls(
            msg_type = ProtocolMessageType.AGENT_ANNOUNCE,
            src_node = src_node,
            dst_node = "",   # broadcast
            payload  = {"capabilities": [c.to_dict() for c in capabilities]},
        )

    @classmethod
    def agent_withdraw(
        cls,
        src_node:    str,
        agent_names: List[str],
    ) -> "ProtocolMessage":
        """Announce that one or more local agents have been removed."""
        return cls(
            msg_type = ProtocolMessageType.AGENT_WITHDRAW,
            src_node = src_node,
            dst_node = "",
            payload  = {"agents": list(agent_names)},
        )

    @classmethod
    def heartbeat(cls, src_node: str) -> "ProtocolMessage":
        """Protocol-level keepalive (separate from mesh-transport PING)."""
        return cls(
            msg_type = ProtocolMessageType.HEARTBEAT,
            src_node = src_node,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "proto_version": self.proto_version,
            "msg_type":      self.msg_type.value,
            "msg_id":        self.msg_id,
            "session_id":    self.session_id,
            "src_node":      self.src_node,
            "src_agent":     self.src_agent,
            "dst_node":      self.dst_node,
            "dst_agent":     self.dst_agent,
            "task":          self.task,
            "payload":       dict(self.payload),
            "reply_to":      self.reply_to,
            "timestamp":     self.timestamp,
            "ttl":           self.ttl,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProtocolMessage":
        try:
            msg_type = ProtocolMessageType(d["msg_type"])
        except (KeyError, ValueError) as exc:
            raise ProtocolError(
                f"Invalid msg_type: {d.get('msg_type')!r}"
            ) from exc

        return cls(
            proto_version = d.get("proto_version", PROTOCOL_VERSION),
            msg_type      = msg_type,
            msg_id        = d.get("msg_id", str(uuid.uuid4())),
            session_id    = d.get("session_id", str(uuid.uuid4())),
            src_node      = d.get("src_node", ""),
            src_agent     = d.get("src_agent", ""),
            dst_node      = d.get("dst_node", ""),
            dst_agent     = d.get("dst_agent", ""),
            task          = d.get("task", ""),
            payload       = dict(d.get("payload", {})),
            reply_to      = d.get("reply_to", ""),
            timestamp     = d.get("timestamp", time.time()),
            ttl           = d.get("ttl", 8),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_expired(self) -> bool:
        """True if the message is older than MESSAGE_TTL_SECS seconds."""
        return (time.time() - self.timestamp) > MESSAGE_TTL_SECS

    def is_broadcast(self) -> bool:
        """True if the message has no specific destination node."""
        return not self.dst_node

    def decrement_ttl(self) -> bool:
        """
        Decrement the hop counter.

        Returns True if the message should still be forwarded,
        False if TTL has reached zero and it must be dropped.
        """
        self.ttl = max(0, self.ttl - 1)
        return self.ttl > 0

    def make_reply(
        self,
        msg_type:  ProtocolMessageType,
        payload:   Dict[str, Any],
        src_node:  str,
        src_agent: str = "",
    ) -> "ProtocolMessage":
        """
        Convenience: create a reply to this message.

        Swaps src/dst, preserves session_id, sets reply_to = self.msg_id.
        """
        return ProtocolMessage(
            msg_type   = msg_type,
            src_node   = src_node,
            dst_node   = self.src_node,
            src_agent  = src_agent,
            dst_agent  = self.src_agent,
            task       = self.task,
            payload    = payload,
            reply_to   = self.msg_id,
            session_id = self.session_id,
        )

    def __repr__(self) -> str:
        src = self.src_node[:12] if self.src_node else "?"
        dst = self.dst_node[:12] if self.dst_node else "broadcast"
        return (
            f"<ProtocolMessage {self.msg_type.value} "
            f"src={src} dst={dst} task={self.task!r}>"
        )


# ---------------------------------------------------------------------------
# ProtocolError
# ---------------------------------------------------------------------------

class ProtocolError(Exception):
    """Raised when a message cannot be parsed or routed."""
