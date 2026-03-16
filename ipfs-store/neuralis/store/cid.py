"""
neuralis.store.cid
==================
Content Identifiers (CIDs) for Neuralis local-first storage.

A CID uniquely identifies a piece of content by the cryptographic hash of
that content.  Two nodes that have the same content will always compute the
same CID — this is the foundation of content-addressed storage.

We implement a simplified CID v1 compatible with IPFS:

    CIDv1 structure:
        <version><codec><multihash>

    Where:
        version   = 0x01 (varint)
        codec     = 0x55 (raw bytes) | 0x70 (dag-pb) | 0x71 (dag-cbor)
        multihash = <hash-fn-code><digest-length><digest>
                    sha2-256 = 0x12, length = 0x20 (32 bytes)

    Wire encoding: base32 (lowercase, no padding) — the standard CIDv1 string form.
    Example: bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku

Design choices
--------------
- SHA-256 only (multihash code 0x12) — sufficient for all Neuralis use cases
- Raw codec (0x55) for arbitrary bytes; dag-pb (0x70) for structured objects
- No external IPFS library required — pure stdlib + hashlib
- CID objects are immutable and hashable (usable as dict keys / set members)
- verify(data) → bool lets any receiver check content integrity in one call
"""

from __future__ import annotations

import base64
import hashlib
import struct
from enum import IntEnum
from typing import Union


# ---------------------------------------------------------------------------
# Codec constants (IPFS multicodec table subset)
# ---------------------------------------------------------------------------

class Codec(IntEnum):
    RAW      = 0x55   # raw binary content
    DAG_PB   = 0x70   # Protocol Buffers (IPFS UnixFS)
    DAG_CBOR = 0x71   # CBOR (structured data)
    JSON     = 0x0200 # JSON


# ---------------------------------------------------------------------------
# Multihash constants
# ---------------------------------------------------------------------------

HASH_SHA2_256 = 0x12
HASH_SHA2_256_LEN = 32  # bytes


# ---------------------------------------------------------------------------
# Varint helpers (used in CID binary encoding)
# ---------------------------------------------------------------------------

def _encode_varint(n: int) -> bytes:
    """Encode a non-negative integer as a protobuf-style varint."""
    buf = []
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            break
    return bytes(buf)


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a varint from data at offset. Returns (value, new_offset)."""
    result = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, offset


# ---------------------------------------------------------------------------
# CID
# ---------------------------------------------------------------------------

class CID:
    """
    Immutable Content Identifier.

    Construction
    ------------
        cid = CID.from_bytes(raw_data)          # hash raw bytes
        cid = CID.from_str("bafkrei...")         # parse existing CID string
        cid = CID.from_binary(binary_cid)        # parse binary CID

    Usage
    -----
        print(cid)                  # "bafkrei..."
        cid.verify(data) → bool     # True if sha256(data) matches this CID
        cid.digest → bytes          # raw 32-byte SHA-256 digest
        cid.codec → Codec           # content type
        len(cid.digest) == 32       # always true for SHA-256 CIDs
    """

    __slots__ = ("_digest", "_codec", "_str_cache")

    def __init__(self, digest: bytes, codec: Codec = Codec.RAW):
        if len(digest) != HASH_SHA2_256_LEN:
            raise ValueError(
                f"CID digest must be {HASH_SHA2_256_LEN} bytes, got {len(digest)}"
            )
        self._digest = bytes(digest)
        self._codec = Codec(codec)
        self._str_cache: str | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes, codec: Codec = Codec.RAW) -> "CID":
        """Compute a CID by hashing raw bytes with SHA-256."""
        digest = hashlib.sha256(data).digest()
        return cls(digest, codec)

    @classmethod
    def from_str(cls, cid_str: str) -> "CID":
        """
        Parse a CIDv1 string (base32 lowercase, no padding).

        Raises ValueError if the string is not a valid Neuralis CID.
        """
        s = cid_str.strip()

        # Must start with 'b' (base32 multibase prefix)
        if not s.startswith("b"):
            raise ValueError(f"Expected base32 CID starting with 'b', got: {s[:8]!r}")

        # Decode base32 (add padding as needed)
        b32 = s[1:].upper()
        pad = (8 - len(b32) % 8) % 8
        try:
            binary = base64.b32decode(b32 + "=" * pad)
        except Exception as exc:
            raise ValueError(f"Invalid base32 in CID {s[:16]!r}: {exc}") from exc

        return cls.from_binary(binary)

    @classmethod
    def from_binary(cls, data: bytes) -> "CID":
        """
        Parse a CIDv1 binary encoding.

        Format: version(varint) + codec(varint) + multihash
        Multihash: hash_fn(varint) + digest_len(varint) + digest(bytes)
        """
        offset = 0

        # Version
        version, offset = _decode_varint(data, offset)
        if version != 1:
            raise ValueError(f"Only CIDv1 supported, got version {version}")

        # Codec
        codec_int, offset = _decode_varint(data, offset)
        try:
            codec = Codec(codec_int)
        except ValueError:
            raise ValueError(f"Unknown codec: 0x{codec_int:x}")

        # Multihash: hash function code
        hash_fn, offset = _decode_varint(data, offset)
        if hash_fn != HASH_SHA2_256:
            raise ValueError(f"Only SHA2-256 supported, got hash fn 0x{hash_fn:x}")

        # Multihash: digest length
        digest_len, offset = _decode_varint(data, offset)
        if digest_len != HASH_SHA2_256_LEN:
            raise ValueError(f"Expected {HASH_SHA2_256_LEN}-byte digest, got {digest_len}")

        # Digest
        if offset + digest_len > len(data):
            raise ValueError("CID binary data truncated")
        digest = data[offset: offset + digest_len]

        return cls(digest, codec)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def digest(self) -> bytes:
        """Raw SHA-256 digest (32 bytes)."""
        return self._digest

    @property
    def codec(self) -> Codec:
        """Content codec."""
        return self._codec

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_binary(self) -> bytes:
        """Encode to CIDv1 binary format."""
        return (
            _encode_varint(1)                       # version = 1
            + _encode_varint(int(self._codec))      # codec
            + _encode_varint(HASH_SHA2_256)         # hash function
            + _encode_varint(HASH_SHA2_256_LEN)     # digest length
            + self._digest                          # digest
        )

    def to_str(self) -> str:
        """
        Encode to CIDv1 string (multibase base32 lowercase, no padding).
        The leading 'b' is the multibase prefix for base32lower.
        """
        if self._str_cache is None:
            binary = self.to_binary()
            b32 = base64.b32encode(binary).decode().lower().rstrip("=")
            self._str_cache = "b" + b32
        return self._str_cache

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, data: bytes) -> bool:
        """
        Verify that data matches this CID.

        Returns True if sha256(data) == self.digest, False otherwise.
        Never raises.
        """
        try:
            return hashlib.sha256(data).digest() == self._digest
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Python protocols
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        return self.to_str()

    def __repr__(self) -> str:
        return f"<CID {self.to_str()[:24]}… codec={self._codec.name}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CID):
            return self._digest == other._digest and self._codec == other._codec
        if isinstance(other, str):
            try:
                return self == CID.from_str(other)
            except ValueError:
                return False
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._digest, int(self._codec)))

    def __lt__(self, other: "CID") -> bool:
        return self._digest < other._digest
