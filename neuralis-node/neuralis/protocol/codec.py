"""
neuralis.protocol.codec
=======================
Encode and decode ProtocolMessages to and from the mesh wire format.

The codec is the glue between:
  - ``ProtocolMessage``  (Module 5 — agent-protocol)
  - ``MessageEnvelope``  (Module 2 — mesh-transport)

All agent messages travel across the mesh as::

    MessageEnvelope(type=AGENT_MSG, payload=<ProtocolMessage.to_dict()>)

This module provides two pure functions and one helper class:

  encode(msg) → dict
      Convert a ProtocolMessage to the dict that goes into
      MessageEnvelope.payload.  This is the canonical serialisation.

  decode(payload) → ProtocolMessage
      Parse a ProtocolMessage from a raw MessageEnvelope.payload dict.
      Raises ProtocolError on invalid input.

  ProtocolCodec
      A stateful helper that also validates protocol version compatibility
      and tracks decode error counts (useful for the Canvas API status).

Version negotiation
-------------------
If the incoming ``proto_version`` is higher than ``PROTOCOL_VERSION``,
the codec accepts it but logs a warning — we may be missing new fields.
If it is zero or negative, the codec rejects it with ProtocolError.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from neuralis.protocol.messages import (
    PROTOCOL_VERSION,
    ProtocolError,
    ProtocolMessage,
    ProtocolMessageType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure encode / decode functions
# ---------------------------------------------------------------------------

def encode(msg: ProtocolMessage) -> dict:
    """
    Encode a ProtocolMessage to a plain dict for use as a
    ``MessageEnvelope.payload``.

    This is a thin wrapper around ``ProtocolMessage.to_dict()`` that
    exists so callers don't need to import ProtocolMessage directly.

    Parameters
    ----------
    msg : ProtocolMessage

    Returns
    -------
    dict — JSON-serialisable payload dict
    """
    return msg.to_dict()


def decode(payload: dict) -> ProtocolMessage:
    """
    Decode a ``MessageEnvelope.payload`` dict into a ProtocolMessage.

    Parameters
    ----------
    payload : dict — the raw payload from a received MessageEnvelope

    Returns
    -------
    ProtocolMessage

    Raises
    ------
    ProtocolError — if the payload is missing required fields or contains
                    an unrecognised message type
    TypeError     — if payload is not a dict
    """
    if not isinstance(payload, dict):
        raise ProtocolError(
            f"payload must be a dict, got {type(payload).__name__}"
        )

    version = payload.get("proto_version", 0)
    if not isinstance(version, int) or version <= 0:
        raise ProtocolError(f"Invalid proto_version: {version!r}")

    if version > PROTOCOL_VERSION:
        logger.warning(
            "Incoming proto_version=%d is newer than local version=%d; "
            "some fields may be ignored",
            version, PROTOCOL_VERSION,
        )

    return ProtocolMessage.from_dict(payload)


# ---------------------------------------------------------------------------
# ProtocolCodec — stateful helper with error tracking
# ---------------------------------------------------------------------------

class ProtocolCodec:
    """
    Stateful encode/decode helper for one node's protocol layer.

    Wraps the ``encode`` / ``decode`` functions with:
      - Error counters for monitoring
      - Version-mismatch logging
      - A ``decode_safe`` method that never raises (returns None on error)

    Attributes
    ----------
    decode_errors   : total number of failed decode attempts
    encode_errors   : total number of failed encode attempts
    messages_in     : total messages successfully decoded
    messages_out    : total messages successfully encoded
    """

    def __init__(self) -> None:
        self.decode_errors:  int = 0
        self.encode_errors:  int = 0
        self.messages_in:    int = 0
        self.messages_out:   int = 0

    def encode(self, msg: ProtocolMessage) -> dict:
        """
        Encode a ProtocolMessage to a payload dict.

        Raises ProtocolError / TypeError on failure.
        Increments ``messages_out`` on success.
        """
        try:
            result = encode(msg)
            self.messages_out += 1
            return result
        except Exception as exc:
            self.encode_errors += 1
            raise ProtocolError(f"Encode failed: {exc}") from exc

    def decode(self, payload: dict) -> ProtocolMessage:
        """
        Decode a payload dict to a ProtocolMessage.

        Raises ProtocolError / TypeError on failure.
        Increments ``messages_in`` on success.
        """
        try:
            result = decode(payload)
            self.messages_in += 1
            return result
        except Exception as exc:
            self.decode_errors += 1
            raise

    def decode_safe(self, payload: dict) -> Optional[ProtocolMessage]:
        """
        Decode a payload dict, returning None on any error.

        Use this in receive loops where a single bad message should not
        crash the handler.
        """
        try:
            return self.decode(payload)
        except Exception as exc:
            logger.debug("decode_safe: dropped malformed message: %s", exc)
            return None

    def stats(self) -> Dict[str, int]:
        return {
            "messages_in":    self.messages_in,
            "messages_out":   self.messages_out,
            "decode_errors":  self.decode_errors,
            "encode_errors":  self.encode_errors,
        }

    def __repr__(self) -> str:
        return (
            f"<ProtocolCodec in={self.messages_in} out={self.messages_out} "
            f"err_in={self.decode_errors} err_out={self.encode_errors}>"
        )
