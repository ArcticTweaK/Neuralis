"""
neuralis.crypto.tokens
======================
Capability tokens for agent authorization in Neuralis.

A CapabilityToken is a signed, time-bounded authorization credential that a
node issues to grant another node (or agent) permission to perform a specific
operation.  Think of it as a lightweight, offline-verifiable JWT — except
signed with HMAC-SHA256 using the node's local HMAC key (no PKI required).

Use cases
---------
- Node A grants Node B permission to invoke a specific agent on Node A
- Node A grants temporary read access to a specific CID
- Agent A sub-delegates a task to Agent B with scoped permissions

Token structure
---------------
    header  : { "v": 1, "alg": "HS256" }
    payload : {
        "jti":       "<random 16-byte hex>",   // unique token ID
        "iss":       "NRL1...",                // issuer node_id
        "sub":       "NRL1...",                // subject node_id (grantee)
        "aud":       "NRL1...",                // audience node_id (verifier)
        "iat":       1234567890.0,             // issued at (unix float)
        "exp":       1234567890.0,             // expiry (unix float)
        "cap":       "agent:invoke:summarize", // capability string
        "scope":     { ... },                  // optional extra constraints
    }
    signature : HMAC-SHA256(b64(header).b64(payload), hmac_key)

Wire format: "<b64url_header>.<b64url_payload>.<b64url_signature>"
(intentionally similar to JWT so existing tooling can inspect the header/payload)

Capability strings
------------------
Hierarchical dot/colon notation:
    "agent:invoke:*"        — invoke any agent
    "agent:invoke:search"   — invoke only the 'search' agent
    "content:read:<cid>"    — read a specific CID
    "content:read:*"        — read any content
    "node:status"           — read node status
    "task:route:*"          — route tasks to any agent

Usage
-----
    # Issue a token (issuer side)
    token = issue_token(
        issuer_id   = node.identity.node_id,
        subject_id  = peer_node_id,
        audience_id = peer_node_id,
        capability  = "agent:invoke:search",
        hmac_key    = keystore.hmac_key,
        ttl_seconds = 300,
    )
    wire = token.to_wire()

    # Verify a token (verifier side)
    token = CapabilityToken.from_wire(wire)
    verify_token(token, hmac_key=keystore.hmac_key, expected_audience=my_node_id)
    # verify_token raises TokenError if invalid
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Raised when a token is malformed, expired, or has an invalid signature."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    # Add padding back
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# CapabilityToken
# ---------------------------------------------------------------------------


@dataclass
class CapabilityToken:
    """
    A signed capability token.

    Attributes
    ----------
    token_id    : unique random ID (jti)
    issuer_id   : NRL1... node that issued this token
    subject_id  : NRL1... node this token was issued to
    audience_id : NRL1... node that should verify this token
    issued_at   : unix float when issued
    expires_at  : unix float when this token expires
    capability  : capability string (e.g. "agent:invoke:search")
    scope       : optional dict with extra constraints
    """

    token_id: str
    issuer_id: str
    subject_id: str
    audience_id: str
    issued_at: float
    expires_at: float
    capability: str
    scope: Dict[str, Any] = field(default_factory=dict)

    TOKEN_VERSION = 1

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def ttl_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())

    # ------------------------------------------------------------------
    # Payload dict
    # ------------------------------------------------------------------

    def _payload_dict(self) -> dict:
        d = {
            "jti": self.token_id,
            "iss": self.issuer_id,
            "sub": self.subject_id,
            "aud": self.audience_id,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "cap": self.capability,
        }
        if self.scope:
            d["scope"] = self.scope
        return d

    # ------------------------------------------------------------------
    # Wire serialisation
    # ------------------------------------------------------------------

    def to_wire(self, hmac_key: bytes) -> str:
        """
        Produce the signed wire string: "<b64url_header>.<b64url_payload>.<b64url_sig>"

        Parameters
        ----------
        hmac_key : 32-byte HMAC-SHA256 key from CryptoKeyStore
        """
        header = {"v": self.TOKEN_VERSION, "alg": "HS256"}
        h_enc = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        p_enc = _b64url_encode(
            json.dumps(self._payload_dict(), separators=(",", ":")).encode()
        )
        signing_input = f"{h_enc}.{p_enc}".encode()
        sig = _hmac_sha256(hmac_key, signing_input)
        s_enc = _b64url_encode(sig)
        return f"{h_enc}.{p_enc}.{s_enc}"

    @classmethod
    def from_wire(cls, wire: str) -> "CapabilityToken":
        """
        Deserialise a token from wire string. Does NOT verify the signature
        (call verify_token for that).
        """
        parts = wire.split(".")
        if len(parts) != 3:
            raise TokenError(f"Malformed token: expected 3 parts, got {len(parts)}")

        try:
            payload_bytes = _b64url_decode(parts[1])
            payload = json.loads(payload_bytes)
        except Exception as exc:
            raise TokenError(f"Failed to decode token payload: {exc}") from exc

        try:
            return cls(
                token_id=str(payload["jti"]),
                issuer_id=str(payload["iss"]),
                subject_id=str(payload["sub"]),
                audience_id=str(payload["aud"]),
                issued_at=float(payload["iat"]),
                expires_at=float(payload["exp"]),
                capability=str(payload["cap"]),
                scope=payload.get("scope", {}),
            )
        except (KeyError, ValueError) as exc:
            raise TokenError(f"Missing required token field: {exc}") from exc

    def __repr__(self) -> str:
        status = "EXPIRED" if self.is_expired else f"TTL={self.ttl_remaining:.0f}s"
        return (
            f"<CapabilityToken {self.token_id[:8]}… "
            f"cap={self.capability!r} "
            f"sub={self.subject_id[:12]}… {status}>"
        )


# ---------------------------------------------------------------------------
# issue_token — factory function
# ---------------------------------------------------------------------------


def issue_token(
    issuer_id: str,
    subject_id: str,
    audience_id: str,
    capability: str,
    hmac_key: bytes,
    ttl_seconds: float = 300.0,
    scope: Optional[Dict[str, Any]] = None,
) -> "SignedToken":
    """
    Issue a new capability token.

    Parameters
    ----------
    issuer_id   : NRL1... node issuing the token (usually local node)
    subject_id  : NRL1... node the token is issued to (grantee)
    audience_id : NRL1... node that will verify this token (usually same as subject)
    capability  : capability string
    hmac_key    : 32-byte HMAC key from CryptoKeyStore
    ttl_seconds : how long the token is valid (default 5 minutes)
    scope       : optional extra constraints dict

    Returns
    -------
    SignedToken with .token (CapabilityToken) and .wire (str)
    """
    now = time.time()
    token = CapabilityToken(
        token_id=os.urandom(16).hex(),
        issuer_id=issuer_id,
        subject_id=subject_id,
        audience_id=audience_id,
        issued_at=now,
        expires_at=now + ttl_seconds,
        capability=capability,
        scope=scope or {},
    )
    wire = token.to_wire(hmac_key)
    return SignedToken(token=token, wire=wire)


@dataclass
class SignedToken:
    """Container for a freshly issued token and its wire representation."""

    token: CapabilityToken
    wire: str

    def __repr__(self) -> str:
        return f"<SignedToken {repr(self.token)}>"


# ---------------------------------------------------------------------------
# verify_token — verify signature + claims
# ---------------------------------------------------------------------------


def verify_token(
    token_or_wire: "str | CapabilityToken",
    hmac_key: bytes,
    expected_audience: Optional[str] = None,
    expected_issuer: Optional[str] = None,
    required_capability: Optional[str] = None,
    wire: Optional[str] = None,
) -> CapabilityToken:
    """
    Verify a capability token's signature and claims.

    Parameters
    ----------
    token_or_wire       : either a wire string or a CapabilityToken + wire param
    hmac_key            : 32-byte HMAC key from CryptoKeyStore
    expected_audience   : if set, token.audience_id must match
    expected_issuer     : if set, token.issuer_id must match
    required_capability : if set, token.capability must match (supports '*' wildcard suffix)
    wire                : wire string if token_or_wire is a CapabilityToken

    Returns
    -------
    The verified CapabilityToken.

    Raises
    ------
    TokenError on any failure.
    """
    if isinstance(token_or_wire, str):
        wire_str = token_or_wire
        token = CapabilityToken.from_wire(wire_str)
    else:
        token = token_or_wire
        wire_str = wire
        if not wire_str:
            raise TokenError("Must provide wire string when passing a CapabilityToken")

    # Re-derive expected signature
    parts = wire_str.split(".")
    if len(parts) != 3:
        raise TokenError("Malformed token wire format")

    signing_input = f"{parts[0]}.{parts[1]}".encode()
    expected_sig = _hmac_sha256(hmac_key, signing_input)
    actual_sig = _b64url_decode(parts[2])

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise TokenError("Token HMAC signature is invalid")

    # Expiry check
    if token.is_expired:
        raise TokenError(f"Token expired {time.time() - token.expires_at:.1f}s ago")

    # Issued-at sanity (not more than 24h in the past, not in the future)
    age = time.time() - token.issued_at
    if age > 86400:
        raise TokenError(f"Token issued_at is too old: {age:.0f}s")
    if age < -60:
        raise TokenError(f"Token issued_at is in the future: {-age:.0f}s")

    # Audience check
    if expected_audience and token.audience_id != expected_audience:
        raise TokenError(
            f"Token audience mismatch: expected {expected_audience}, got {token.audience_id}"
        )

    # Issuer check
    if expected_issuer and token.issuer_id != expected_issuer:
        raise TokenError(
            f"Token issuer mismatch: expected {expected_issuer}, got {token.issuer_id}"
        )

    # Capability check (supports wildcard suffix: "agent:invoke:*")
    if required_capability:
        if not _capability_matches(token.capability, required_capability):
            raise TokenError(
                f"Token capability {token.capability!r} does not satisfy {required_capability!r}"
            )

    return token


def _capability_matches(token_cap: str, required: str) -> bool:
    """
    Check if token_cap satisfies the required capability.

    Rules:
    - Exact match:              "agent:invoke:search" satisfies "agent:invoke:search"
    - Prefix wildcard:          "agent:invoke:*"      satisfies "agent:invoke:search"
    - Full wildcard on token:   "*"                   satisfies anything
    """
    if token_cap == "*" or token_cap == required:
        return True
    if token_cap.endswith(":*"):
        prefix = token_cap[:-2]
        return required.startswith(prefix)
    return False
