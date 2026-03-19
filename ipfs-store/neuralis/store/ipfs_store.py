"""
neuralis.store.ipfs_store
=========================
IPFSStore — the main content storage interface for Neuralis.

IPFSStore is the Module 3 subsystem registered on the Node.  It provides:

    - add(data)          → CID    store bytes, auto-pin, return CID
    - add_file(path)     → CID    read a file, chunk it, store, return CID
    - get(cid)           → bytes  retrieve content by CID
    - pin(cid)                    protect from GC
    - unpin(cid)                  allow GC to reclaim
    - is_pinned(cid)     → bool
    - ls()               → list   all pinned CIDs + metadata
    - gc()               → int    delete unpinned blocks, return count
    - stat(cid)          → dict   size, codec, pinned, added_at
    - repo_stat()        → dict   total blocks, bytes, pins, free space

File chunking (add_file)
------------------------
Large files are split into 256 KB chunks.  Each chunk is stored as a
separate block.  A small JSON manifest is stored as an additional block
linking the chunks in order.  The manifest CID is what gets pinned and
returned — retrieving it gives back the full file.

Manifest format (dag-cbor codec, stored as JSON for simplicity):
    {
        "type": "file",
        "name": "<filename>",
        "size": <total bytes>,
        "chunks": ["<cid1>", "<cid2>", ...],
        "added_at": <unix float>
    }

Integration with Node
---------------------
    store = IPFSStore(node)
    await store.start()
    cid = await store.add(b"hello world")
    data = await store.get(cid)
    await store.stop()

The async interface wraps synchronous blockstore/pin operations in
asyncio.get_event_loop().run_in_executor() for non-blocking I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Union

from neuralis.store.blockstore import BlockStore, BlockstoreStats
from neuralis.store.cid import CID, Codec
from neuralis.store.pins import PinManager, PinRecord, PinType

logger = logging.getLogger(__name__)

# Default chunk size for file splitting: 256 KB
CHUNK_SIZE = 256 * 1024

# Max file size for add_file: 2 GB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ContentNotFound(Exception):
    """Raised when a CID is not in the local store."""


class StorageLimitExceeded(Exception):
    """Raised when adding content would exceed the configured storage limit."""


# ---------------------------------------------------------------------------
# IPFSStore
# ---------------------------------------------------------------------------


class IPFSStore:
    """
    Neuralis local-first content store.

    This is the Module 3 subsystem — it wraps BlockStore + PinManager
    behind a clean async API and integrates with the Node lifecycle.

    The store is purely local.  Network replication (fetching CIDs from
    peers) is wired up in Module 5 (agent-protocol) once the mesh is live.

    Parameters
    ----------
    node  : neuralis.node.Node  — the running node (provides config)
    """

    def __init__(self, node):
        self._node = node
        self._config = node.config.storage
        repo_path = Path(self._config.ipfs_repo_path)

        self._blockstore = BlockStore(repo_path)
        self._pins = PinManager(repo_path)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ipfs-io")
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the store and register with the node."""
        if self._running:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._blockstore.open)
        await loop.run_in_executor(self._executor, self._pins.open)

        self._running = True

        self._node.register_subsystem("ipfs", self)
        self._node.on_shutdown(self.stop)

        stats = self._blockstore.stats()
        logger.info(
            "IPFSStore started | repo=%s | blocks=%d | pins=%d",
            self._config.ipfs_repo_path,
            stats.total_blocks,
            self._pins.count(),
        )

    async def stop(self) -> None:
        """Flush and close the store."""
        if not self._running:
            return
        self._running = False

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._blockstore.close)
        await loop.run_in_executor(self._executor, self._pins.close)
        self._executor.shutdown(wait=False)

        logger.info("IPFSStore stopped")

    # ------------------------------------------------------------------
    # Add content
    # ------------------------------------------------------------------

    async def add(
        self,
        data: bytes,
        codec: Codec = Codec.RAW,
        pin: bool = True,
        name: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> CID:
        """
        Store bytes and return the CID.

        Parameters
        ----------
        data   : bytes to store
        codec  : content codec (default RAW)
        pin    : if True (default), pin the CID immediately
        name   : optional label for the pin
        tags   : optional tags for the pin

        Returns
        -------
        CID

        Raises
        ------
        StorageLimitExceeded — if adding would exceed max_storage_gb
        """
        self._check_running()
        await self._check_capacity(len(data))

        loop = asyncio.get_event_loop()
        cid = await loop.run_in_executor(
            self._executor,
            lambda: self._blockstore.put(data, codec),
        )

        if pin or self._config.auto_pin_local:
            await loop.run_in_executor(
                self._executor,
                lambda: self._pins.pin(cid, name=name, size=len(data), tags=tags or []),
            )

        logger.debug("add: %s (%d bytes)", cid, len(data))
        return cid

    async def add_file(
        self,
        path: Union[str, Path],
        name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        chunk_size: int = CHUNK_SIZE,
    ) -> CID:
        """
        Store a file from disk, chunked into CHUNK_SIZE blocks.

        Returns the CID of the file manifest (which links all chunks).

        Parameters
        ----------
        path       : path to the file on disk
        name       : optional label (defaults to filename)
        tags       : optional tags
        chunk_size : override the default 256 KB chunk size

        Returns
        -------
        CID  — the manifest CID (pin this to retain the whole file)

        Raises
        ------
        FileNotFoundError  — if the file doesn't exist
        StorageLimitExceeded — if the file is too large for remaining capacity
        """
        self._check_running()
        file_path = Path(path)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = file_path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            raise ValueError(f"File too large: {file_size} bytes > {MAX_FILE_SIZE} max")

        await self._check_capacity(file_size)

        display_name = name or file_path.name
        loop = asyncio.get_event_loop()

        # Chunk the file and store each chunk
        chunk_cids: List[str] = []

        def _store_file() -> List[str]:
            cids = []
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    cid = self._blockstore.put(chunk, Codec.RAW)
                    cids.append(str(cid))
            return cids

        chunk_cids = await loop.run_in_executor(self._executor, _store_file)

        # Build and store the manifest
        manifest = {
            "type": "file",
            "name": display_name,
            "size": file_size,
            "chunks": chunk_cids,
            "chunk_size": chunk_size,
            "added_at": time.time(),
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode()
        manifest_cid = await loop.run_in_executor(
            self._executor,
            lambda: self._blockstore.put(manifest_bytes, Codec.DAG_CBOR),
        )

        # Pin the manifest (chunk blocks are implicitly retained via the manifest)
        await loop.run_in_executor(
            self._executor,
            lambda: self._pins.pin(
                manifest_cid,
                pin_type=PinType.RECURSIVE,
                name=display_name,
                size=file_size,
                tags=tags or [],
            ),
        )

        logger.info(
            "add_file: %s → %s (%d chunks, %d bytes)",
            display_name,
            manifest_cid,
            len(chunk_cids),
            file_size,
        )
        return manifest_cid

    # ------------------------------------------------------------------
    # Retrieve content
    # ------------------------------------------------------------------

    async def get(self, cid: CID) -> bytes:
        """
        Retrieve content by CID.

        For manifest CIDs (files added via add_file), this returns
        the raw manifest JSON.  Use get_file() to reassemble the full file.

        Raises
        ------
        ContentNotFound  — if the CID is not in the local store
        ValueError       — if the stored block is corrupted
        """
        self._check_running()
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                self._executor,
                lambda: self._blockstore.get(cid),
            )
            return data
        except KeyError:
            raise ContentNotFound(f"Content not found locally: {cid}")

    async def get_file(self, manifest_cid: CID) -> bytes:
        """
        Retrieve and reassemble a chunked file by its manifest CID.

        Returns the full file bytes (assembled from all chunks).

        Raises
        ------
        ContentNotFound  — if any chunk is missing
        ValueError       — if the manifest is malformed
        """
        self._check_running()

        # Get the manifest
        manifest_bytes = await self.get(manifest_cid)
        try:
            manifest = json.loads(manifest_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Malformed manifest at {manifest_cid}: {exc}") from exc

        if manifest.get("type") != "file":
            raise ValueError(f"CID {manifest_cid} is not a file manifest")

        chunk_cid_strs = manifest.get("chunks", [])
        if not chunk_cid_strs:
            return b""

        # Reassemble chunks
        parts = []
        for cid_str in chunk_cid_strs:
            chunk_cid = CID.from_str(cid_str)
            chunk = await self.get(chunk_cid)
            parts.append(chunk)

        return b"".join(parts)

    # ------------------------------------------------------------------
    # Pin management
    # ------------------------------------------------------------------

    async def pin(
        self,
        cid: CID,
        name: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> PinRecord:
        """Pin a CID. Idempotent."""
        self._check_running()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._pins.pin(cid, name=name, tags=tags or []),
        )

    async def unpin(self, cid: CID) -> bool:
        """
        Remove a pin.  The block will be eligible for GC.

        Returns True if unpinned, False if it wasn't pinned.
        """
        self._check_running()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._pins.unpin(cid),
        )

    async def is_pinned(self, cid: CID) -> bool:
        """Return True if the CID is pinned."""
        self._check_running()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._pins.is_pinned(cid),
        )

    async def list_pins(self):
        """Alias for ls() — called by canvas-api routes."""
        return await self.ls()

    async def ls(
        self,
        pin_type: Optional[PinType] = None,
        tag: Optional[str] = None,
    ) -> List[dict]:
        """
        List all pinned content.

        Returns a list of dicts with cid, name, size, type, tags, pinned_at.
        """
        self._check_running()
        loop = asyncio.get_event_loop()
        records = await loop.run_in_executor(
            self._executor,
            lambda: self._pins.list_pins(pin_type=pin_type, tag=tag),
        )
        return [
            {
                "cid": r.cid,
                "name": r.name,
                "size": r.size,
                "type": r.pin_type.value,
                "tags": r.tags,
                "pinned_at": r.pinned_at,
            }
            for r in records
        ]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def gc(self) -> int:
        """
        Run garbage collection — delete all unpinned blocks.

        Returns the number of blocks deleted.
        """
        self._check_running()
        loop = asyncio.get_event_loop()

        pinned = await loop.run_in_executor(
            self._executor,
            self._pins.pinned_cids,
        )
        deleted = await loop.run_in_executor(
            self._executor,
            lambda: self._blockstore.gc_orphans(pinned),
        )
        logger.info("GC complete: %d blocks deleted", deleted)
        return deleted

    async def stat(self, cid: CID) -> dict:
        """
        Return metadata about a specific CID.

        Returns
        -------
        dict with keys: cid, size, codec, pinned, pin_name, added_at, exists
        """
        self._check_running()
        loop = asyncio.get_event_loop()

        block_meta = await loop.run_in_executor(
            self._executor,
            lambda: self._blockstore.stat(cid),
        )
        pin_record = await loop.run_in_executor(
            self._executor,
            lambda: self._pins.get_pin(cid),
        )

        return {
            "cid": str(cid),
            "exists": block_meta is not None,
            "size": block_meta.size if block_meta else None,
            "codec": block_meta.codec if block_meta else None,
            "added_at": block_meta.added_at if block_meta else None,
            "pinned": pin_record is not None,
            "pin_name": pin_record.name if pin_record else None,
            "pin_tags": pin_record.tags if pin_record else [],
        }

    async def repo_stat(self) -> dict:
        """
        Return aggregate repository statistics.

        Returns
        -------
        dict with keys: total_blocks, total_bytes, total_pins,
                        pinned_bytes, max_bytes, free_bytes, repo_path
        """
        self._check_running()
        loop = asyncio.get_event_loop()

        bs_stats = await loop.run_in_executor(
            self._executor,
            self._blockstore.stats,
        )
        pin_count = await loop.run_in_executor(
            self._executor,
            self._pins.count,
        )
        pinned_bytes = await loop.run_in_executor(
            self._executor,
            self._pins.total_pinned_bytes,
        )

        max_bytes = int(self._config.max_storage_gb * 1024**3)
        return {
            "total_blocks": bs_stats.total_blocks,
            "total_bytes": bs_stats.total_bytes,
            "total_pins": pin_count,
            "pinned_bytes": pinned_bytes,
            "max_bytes": max_bytes,
            "free_bytes": max(0, max_bytes - bs_stats.total_bytes),
            "repo_path": bs_stats.repo_path,
            "last_gc_at": bs_stats.last_gc_at,
        }

    async def has(self, cid: CID) -> bool:
        """Return True if the block exists locally (pinned or not)."""
        self._check_running()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._blockstore.has(cid),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_running(self) -> None:
        if not self._running:
            raise RuntimeError("IPFSStore is not running — call start() first")

    async def _check_capacity(self, incoming_bytes: int) -> None:
        """Raise StorageLimitExceeded if there isn't enough space."""
        max_bytes = int(self._config.max_storage_gb * 1024**3)
        loop = asyncio.get_event_loop()
        stats = await loop.run_in_executor(
            self._executor,
            self._blockstore.stats,
        )
        if stats.total_bytes + incoming_bytes > max_bytes:
            raise StorageLimitExceeded(
                f"Storage limit exceeded: "
                f"{stats.total_bytes + incoming_bytes} bytes would exceed "
                f"{max_bytes} bytes ({self._config.max_storage_gb} GB max)"
            )

    def __repr__(self) -> str:
        return (
            f"<IPFSStore running={self._running} "
            f"repo={self._config.ipfs_repo_path}>"
        )
