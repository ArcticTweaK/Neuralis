"""
neuralis.protocol
=================
Agent-protocol layer for the Neuralis mesh (Module 5).

Public API
----------
    from neuralis.protocol import (
        ProtocolMessage,
        ProtocolMessageType,
        AgentCapability,
        ProtocolError,
        ProtocolRouter,
        NoRouteError,
        RemoteNodeInfo,
        ProtocolCodec,
        encode,
        decode,
        PROTOCOL_VERSION,
    )
"""

from neuralis.protocol.messages import (
    PROTOCOL_VERSION,
    AgentCapability,
    ProtocolError,
    ProtocolMessage,
    ProtocolMessageType,
)
from neuralis.protocol.router import (
    NoRouteError,
    PendingRequest,
    ProtocolRouter,
    RemoteNodeInfo,
)
from neuralis.protocol.codec import (
    ProtocolCodec,
    decode,
    encode,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AgentCapability",
    "ProtocolError",
    "ProtocolMessage",
    "ProtocolMessageType",
    "ProtocolRouter",
    "NoRouteError",
    "PendingRequest",
    "RemoteNodeInfo",
    "ProtocolCodec",
    "encode",
    "decode",
]
