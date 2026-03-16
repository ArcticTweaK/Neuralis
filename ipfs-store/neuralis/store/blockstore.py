"""
neuralis.store.blockstore
=========================
Flat-file content-addressed block store for Neuralis.

Architecture
------------
The block store is the lowest layer of Neuralis storage.  It maps CIDs to
raw byte blocks on disk.  Everything above it (pin manager, file importer,
DAG builder) reads and writes through this interface.

Storage layout on disk::

    <repo_root>/
    ├── blocks/
    │   ├── ab/
    │   │   └── cdef...  (filename = full CID string, dir = first 2 chars)
    │   └── ...
    ├── meta/
    │   └── blockstore.json   (stats: total blocks, total bytes, etc.)
    └── pins/
        └── pins.json         (managed by PinManager — not this module)

The two-character sharding directory (ab/ from bafkreiabc...) prevents
filesystems from choking on directories with millions of entries.

Design notes
------------
- All I/O is synchronous (for simplicity and portability).  The async wrappers
  in IPFSStore run blockstore calls in a thread executor when needed.
- Writes are atomic: data is written to a temp file then renamed into place.
- Each block is stored exactly once regardless of how many times it is pinned.
- A block can be deleted only if it is not pinned (enforced by IPFSStore).
- No compression — content is stored raw.  Deduplication comes from CIDs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

from neuralis.store.cid import CID, Codec

logger = logging.getLogger(__name__)

# Subdirectory for block files
BLOCKS_DIR = "blocks"
META_DIR   = "meta"
META_FILE  = "blockstore.json"

# Max block size (16 MB — prevents accidental giant writes)
MAX_BLOCK_SIZE = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# BlockMeta
# ---------------------------------------------------------------------------

@dataclass
class BlockMeta:
    """Metadata about a stored block."""
    cid: str            # CID string
    size: int           # bytes
    added_at: float     # unix timestamp
    codec: str          # codec name


# ---------------------------------------------------------------------------
# BlockstoreStats
# ---------------------------------------------------------------------------

@dataclass
class BlockstoreStats:
    """Aggregate statistics for the block store."""
    total_blocks: int = 0
    total_bytes: int = 0
    repo_path: str = ""
    last_gc_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_blocks": self.total_blocks,
            "total_bytes": self.total_bytes,
            "repo_path": self.repo_path,
            "last_gc_at": self.last_gc_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BlockstoreStats":
        return cls(
            total_blocks=d.get("total_blocks", 0),
            total_bytes=d.get("total_bytes", 0),
            repo_path=d.get("repo_path", ""),
            last_gc_at=d.get("last_gc_at", 0.0),
        )


# ---------------------------------------------------------------------------
# BlockStore
# ---------------------------------------------------------------------------

class BlockStore:
    """
    Flat-file content-addressed block store.

    All public methods operate on CID objects (not strings) to prevent
    accidental mixing of string/CID types.

    Usage
    -----
        store = BlockStore(Path("~/.neuralis/ipfs"))
        store.open()

        cid = store.put(b"hello neuralis")
        data = store.get(cid)
        assert store.has(cid)
        store.delete(cid)

        store.close()
    """

    def __init__(self, repo_path: Path):
        self._repo = Path(repo_path)
        self._blocks_dir = self._repo / BLOCKS_DIR
        self._meta_dir = self._repo / META_DIR
        self._meta_file = self._meta_dir / META_FILE
        self._stats = BlockstoreStats(repo_path=str(repo_path))
        self._open = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Initialise the repo directories and load stats."""
        if self._open:
            return
        self._blocks_dir.mkdir(parents=True, exist_ok=True)
        self._meta_dir.mkdir(parents=True, exist_ok=True)
        self._load_stats()
        self._open = True
        logger.info("BlockStore opened at %s (%d blocks, %d bytes)",
                    self._repo, self._stats.total_blocks, self._stats.total_bytes)

    def close(self) -> None:
        """Flush stats to disk."""
        if not self._open:
            return
        self._save_stats()
        self._open = False
        logger.debug("BlockStore closed")

    def __enter__(self) -> "BlockStore":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def put(self, data: bytes, codec: Codec = Codec.RAW) -> CID:
        """
        Store a block and return its CID.

        If the block already exists (same CID), this is a no-op — the
        existing block is returned without writing to disk again.

        Parameters
        ----------
        data   : raw block bytes (max 16 MB)
        codec  : content codec (default RAW)

        Returns
        -------
        CID  — the content identifier for the stored block

        Raises
        ------
        ValueError  — if data exceeds MAX_BLOCK_SIZE
        RuntimeError — if the store is not open
        """
        self._check_open()
        if len(data) > MAX_BLOCK_SIZE:
            raise ValueError(
                f"Block too large: {len(data)} bytes > {MAX_BLOCK_SIZE} max"
            )

        cid = CID.from_bytes(data, codec)
        path = self._block_path(cid)

        if path.exists():
            logger.debug("Block already exists: %s", cid)
            return cid

        # Atomic write: temp file → rename
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        self._stats.total_blocks += 1
        self._stats.total_bytes += len(data)
        self._save_stats()

        logger.debug("Block stored: %s (%d bytes)", cid, len(data))
        return cid

    def get(self, cid: CID) -> bytes:
        """
        Retrieve a block by CID.

        Raises
        ------
        KeyError   — if the block does not exist
        ValueError — if the stored data does not match the CID (corruption)
        """
        self._check_open()
        path = self._block_path(cid)

        if not path.exists():
            raise KeyError(f"Block not found: {cid}")

        data = path.read_bytes()

        # Integrity check
        if not cid.verify(data):
            raise ValueError(
                f"Block integrity failure for {cid} — "
                f"stored data does not match CID digest"
            )

        return data

    def has(self, cid: CID) -> bool:
        """Return True if the block exists in the store."""
        self._check_open()
        return self._block_path(cid).exists()

    def delete(self, cid: CID) -> bool:
        """
        Delete a block by CID.

        Returns True if the block was deleted, False if it didn't exist.
        Does NOT check pins — the caller (IPFSStore / PinManager) is
        responsible for ensuring the block is not pinned before deleting.
        """
        self._check_open()
        path = self._block_path(cid)

        if not path.exists():
            return False

        size = path.stat().st_size
        path.unlink()

        self._stats.total_blocks = max(0, self._stats.total_blocks - 1)
        self._stats.total_bytes = max(0, self._stats.total_bytes - size)
        self._save_stats()

        logger.debug("Block deleted: %s", cid)
        return True

    def list_cids(self) -> Iterator[CID]:
        """
        Iterate over all CIDs in the block store.

        Yields CID objects in no particular order.
        Skips any files that can't be parsed as valid CIDs.
        """
        self._check_open()
        if not self._blocks_dir.exists():
            return

        for shard_dir in self._blocks_dir.iterdir():
            if not shard_dir.is_dir():
                continue
            for block_file in shard_dir.iterdir():
                if block_file.name.startswith("."):
                    continue
                try:
                    yield CID.from_str(block_file.name)
                except ValueError:
                    logger.debug("Skipping non-CID file: %s", block_file)

    def stat(self, cid: CID) -> Optional[BlockMeta]:
        """Return metadata about a stored block, or None if not found."""
        self._check_open()
        path = self._block_path(cid)
        if not path.exists():
            return None
        st = path.stat()
        return BlockMeta(
            cid=str(cid),
            size=st.st_size,
            added_at=st.st_mtime,
            codec=cid.codec.name,
        )

    # ------------------------------------------------------------------
    # Stats & maintenance
    # ------------------------------------------------------------------

    def stats(self) -> BlockstoreStats:
        """Return aggregate statistics."""
        self._check_open()
        return self._stats

    def recount(self) -> BlockstoreStats:
        """
        Recount blocks and bytes by walking the blocks directory.

        Use after manual edits or crash recovery to re-sync stats.
        """
        self._check_open()
        total_blocks = 0
        total_bytes = 0
        for cid in self.list_cids():
            path = self._block_path(cid)
            if path.exists():
                total_blocks += 1
                total_bytes += path.stat().st_size
        self._stats.total_blocks = total_blocks
        self._stats.total_bytes = total_bytes
        self._save_stats()
        return self._stats

    def gc_orphans(self, pinned_cids: set) -> int:
        """
        Delete all blocks that are NOT in pinned_cids.

        This is the garbage collector.  Called by IPFSStore.gc().

        Returns
        -------
        int  — number of blocks deleted
        """
        self._check_open()
        deleted = 0
        for cid in list(self.list_cids()):
            if cid not in pinned_cids:
                if self.delete(cid):
                    deleted += 1
        self._stats.last_gc_at = time.time()
        self._save_stats()
        logger.info("GC: deleted %d orphan blocks", deleted)
        return deleted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _block_path(self, cid: CID) -> Path:
        """
        Compute the on-disk path for a CID.

        Uses the first 2 characters of the CID string (after the 'b' prefix)
        as a sharding directory.
        """
        cid_str = str(cid)
        shard = cid_str[1:3]   # skip 'b', take next 2 chars
        return self._blocks_dir / shard / cid_str

    def _check_open(self) -> None:
        if not self._open:
            raise RuntimeError("BlockStore is not open — call open() first")

    def _load_stats(self) -> None:
        if self._meta_file.exists():
            try:
                data = json.loads(self._meta_file.read_text())
                self._stats = BlockstoreStats.from_dict(data)
                self._stats.repo_path = str(self._repo)
            except Exception as exc:
                logger.warning("Could not load blockstore stats: %s", exc)
        else:
            # Fresh repo — walk to count existing blocks
            self._stats = BlockstoreStats(repo_path=str(self._repo))

    def _save_stats(self) -> None:
        try:
            self._meta_dir.mkdir(parents=True, exist_ok=True)
            self._meta_file.write_text(
                json.dumps(self._stats.to_dict(), indent=2)
            )
        except Exception as exc:
            logger.warning("Could not save blockstore stats: %s", exc)

    def __repr__(self) -> str:
        return (
            f"<BlockStore path={self._repo} "
            f"blocks={self._stats.total_blocks} "
            f"bytes={self._stats.total_bytes} "
            f"open={self._open}>"
        )
