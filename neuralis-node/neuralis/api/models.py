"""
neuralis.api.models
===================
Pydantic request / response models for the Canvas API.

Every endpoint input and output is typed here.  The Canvas UI (Module 7)
consumes these shapes directly — changing a model here is a contract change
with the frontend.

Design rules
------------
- All IDs are strings (NRL1... node IDs, UUID4s, CID strings)
- All timestamps are Unix floats
- Optional fields always have a default (None or empty collection)
- No model imports anything from neuralis.mesh / neuralis.agents — the API
  layer must remain independently importable for testing
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import time


# ===========================================================================
# Node
# ===========================================================================

class NodeStatusResponse(BaseModel):
    """GET /api/node/status"""
    node_id:          str
    peer_id:          str
    alias:            Optional[str]
    public_key:       str
    state:            str
    boot_time:        float
    uptime_seconds:   float
    subsystems:       List[str]
    listen_addresses: List[str]
    mdns_enabled:     bool
    dht_enabled:      bool
    max_peers:        int
    telemetry_enabled: bool = False


class NodeAliasRequest(BaseModel):
    """PATCH /api/node/alias"""
    alias: str = Field(..., min_length=1, max_length=64)


class NodeAliasResponse(BaseModel):
    alias:   str
    node_id: str


# ===========================================================================
# Peers
# ===========================================================================

class PeerResponse(BaseModel):
    """One peer entry returned in peer list endpoints."""
    node_id:        str
    peer_id:        str
    alias:          Optional[str]      = None
    status:         str
    addresses:      List[str]          = Field(default_factory=list)
    last_seen:      Optional[float]    = None
    last_ping_ms:   Optional[float]    = None
    failed_attempts: int               = 0


class PeerListResponse(BaseModel):
    """GET /api/peers"""
    peers:     List[PeerResponse]
    total:     int
    connected: int


class PeerConnectRequest(BaseModel):
    """POST /api/peers/connect"""
    multiaddr: str = Field(..., description="Full multiaddr e.g. /ip4/1.2.3.4/tcp/7101/p2p/NRL1...")


class PeerConnectResponse(BaseModel):
    success:  bool
    node_id:  Optional[str] = None
    message:  str           = ""


class PeerDisconnectResponse(BaseModel):
    success:  bool
    message:  str = ""


# ===========================================================================
# Content / IPFS
# ===========================================================================

class ContentAddRequest(BaseModel):
    """POST /api/content — add raw text/JSON content."""
    data:    str  = Field(..., description="UTF-8 content to store")
    pin:     bool = True
    name:    Optional[str] = None


class ContentAddResponse(BaseModel):
    cid:      str
    size:     int
    pinned:   bool


class ContentGetResponse(BaseModel):
    cid:   str
    data:  str
    size:  int


class PinRequest(BaseModel):
    """POST /api/content/{cid}/pin"""
    name: Optional[str] = None


class PinResponse(BaseModel):
    cid:    str
    pinned: bool


class PinListResponse(BaseModel):
    pins:  List[Dict[str, Any]]
    total: int


class StorageStatsResponse(BaseModel):
    total_blocks:   int
    total_bytes:    int
    pinned_count:   int
    max_bytes:      int
    used_percent:   float


# ===========================================================================
# Agents
# ===========================================================================

class AgentResponse(BaseModel):
    """One agent entry."""
    name:           str
    version:        str
    state:          str
    capabilities:   List[str]          = Field(default_factory=list)
    required_model: Optional[str]      = None
    stats:          Dict[str, Any]     = Field(default_factory=dict)


class AgentListResponse(BaseModel):
    """GET /api/agents"""
    agents: List[AgentResponse]
    total:  int


class TaskRequest(BaseModel):
    """POST /api/agents/task — dispatch a task to a local agent."""
    task:      str                = Field(..., description="Task name e.g. 'echo'")
    payload:   Dict[str, Any]    = Field(default_factory=dict)
    target:    Optional[str]     = Field(None, description="Specific agent name")
    timeout:   float             = 10.0


class TaskResponse(BaseModel):
    request_id: str
    agent:      str
    status:     str
    data:       Dict[str, Any]   = Field(default_factory=dict)
    error:      Optional[str]    = None
    duration_ms: Optional[float] = None


class AgentReloadResponse(BaseModel):
    added:   List[str]
    updated: List[str]
    removed: List[str]


# ===========================================================================
# Protocol / Remote nodes
# ===========================================================================

class RemoteNodeResponse(BaseModel):
    """One remote node from the protocol router's capability table."""
    node_id:   str
    agents:    List[str]
    tasks:     List[str]
    last_seen: float


class RemoteNodeListResponse(BaseModel):
    """GET /api/protocol/nodes"""
    nodes: List[RemoteNodeResponse]
    total: int


class RemoteTaskRequest(BaseModel):
    """POST /api/protocol/task — route a task to a remote node."""
    task:      str
    payload:   Dict[str, Any] = Field(default_factory=dict)
    dst_node:  str            = Field("", description="Target node_id; empty = auto-select")
    timeout:   float          = 30.0


class RemoteTaskResponse(BaseModel):
    src_node:   str
    dst_node:   str
    task:       str
    payload:    Dict[str, Any] = Field(default_factory=dict)
    session_id: str
    msg_type:   str


# ===========================================================================
# Generic
# ===========================================================================

class OkResponse(BaseModel):
    ok:      bool = True
    message: str  = ""


class ErrorResponse(BaseModel):
    ok:      bool = False
    error:   str
    detail:  Optional[str] = None
