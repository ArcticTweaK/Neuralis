"""
neuralis.store
==============
Module 3: Local-first content-addressed storage for Neuralis.

Public API:

    from neuralis.store import IPFSStore, CID, Codec, PinType

    store = IPFSStore(node)
    await store.start()

    # Store raw bytes
    cid = await store.add(b"hello neuralis")
    data = await store.get(cid)

    # Store a file (auto-chunked)
    cid = await store.add_file("/path/to/model.gguf", name="mistral-7b")

    # Pin management
    await store.pin(cid, name="keep-this")
    await store.unpin(cid)
    pins = await store.ls()

    # Garbage collect unpinned blocks
    deleted = await store.gc()

    # Stats
    info = await store.repo_stat()

    await store.stop()
"""

from neuralis.store.cid import CID, Codec
from neuralis.store.blockstore import BlockStore, BlockstoreStats, BlockMeta
from neuralis.store.pins import PinManager, PinRecord, PinType
from neuralis.store.ipfs_store import IPFSStore, ContentNotFound, StorageLimitExceeded

__all__ = [
    "CID",
    "Codec",
    "BlockStore",
    "BlockstoreStats",
    "BlockMeta",
    "PinManager",
    "PinRecord",
    "PinType",
    "IPFSStore",
    "ContentNotFound",
    "StorageLimitExceeded",
]
