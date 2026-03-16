"""
tests/test_module3.py
=====================
Full test suite for Module 3: ipfs-store

Tests cover:
- CID: construction, serialisation, verification, codecs, edge cases
- BlockStore: put/get/has/delete, atomicity, integrity, stats, GC
- PinManager: pin/unpin/is_pinned, persistence, queries, tags
- IPFSStore: async API, add/get, add_file/get_file, gc, stat, repo_stat
- Integration: full pipeline, chunking, capacity enforcement

Run with:
    pytest neuralis-node/tests/test_module3.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from neuralis.store.cid import (
    CID, Codec,
    _encode_varint, _decode_varint,
)
from neuralis.store.blockstore import BlockStore, BlockstoreStats, MAX_BLOCK_SIZE
from neuralis.store.pins import PinManager, PinRecord, PinType
from neuralis.store.ipfs_store import IPFSStore, ContentNotFound, StorageLimitExceeded
from neuralis.config import NodeConfig
from neuralis.identity import NodeIdentity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_node(tmp_path: Path):
    kd = tmp_path / "identity"
    ident = NodeIdentity.create_new(key_dir=kd)
    cfg = NodeConfig.defaults()
    cfg.identity.key_dir = str(kd)
    cfg.storage.ipfs_repo_path = str(tmp_path / "ipfs")
    cfg.storage.max_storage_gb = 1.0
    cfg.storage.auto_pin_local = True
    cfg.logging.log_dir = str(tmp_path / "logs")
    cfg.agents.agents_dir = str(tmp_path / "agents")
    cfg.agents.models_dir = str(tmp_path / "models")
    node = MagicMock()
    node.identity = ident
    node.config = cfg
    node.register_subsystem = MagicMock()
    node.on_shutdown = MagicMock()
    return node


# ===========================================================================
# 1. Varint helpers
# ===========================================================================

class TestVarint:
    def test_encode_small(self):
        assert _encode_varint(0) == b"\x00"
        assert _encode_varint(1) == b"\x01"
        assert _encode_varint(127) == b"\x7f"

    def test_encode_multibyte(self):
        # 128 = 0x80 encodes as two bytes in varint
        encoded = _encode_varint(128)
        assert len(encoded) == 2

    def test_round_trip(self):
        for n in [0, 1, 127, 128, 255, 256, 1000, 65535, 0x55, 0x70, 0x71]:
            encoded = _encode_varint(n)
            decoded, _ = _decode_varint(encoded)
            assert decoded == n, f"round-trip failed for {n}"

    def test_decode_with_offset(self):
        data = b"\x00" + _encode_varint(42)
        val, new_offset = _decode_varint(data, 1)
        assert val == 42


# ===========================================================================
# 2. CID
# ===========================================================================

class TestCID:
    def test_from_bytes_returns_cid(self):
        cid = CID.from_bytes(b"hello")
        assert isinstance(cid, CID)
        assert cid.codec == Codec.RAW

    def test_digest_is_sha256(self):
        data = b"neuralis"
        cid = CID.from_bytes(data)
        expected = hashlib.sha256(data).digest()
        assert cid.digest == expected

    def test_digest_length(self):
        cid = CID.from_bytes(b"test")
        assert len(cid.digest) == 32

    def test_to_str_starts_with_b(self):
        cid = CID.from_bytes(b"test")
        assert cid.to_str().startswith("b")

    def test_str_round_trip(self):
        cid = CID.from_bytes(b"round trip test")
        s = str(cid)
        restored = CID.from_str(s)
        assert restored == cid

    def test_binary_round_trip(self):
        cid = CID.from_bytes(b"binary round trip")
        binary = cid.to_binary()
        restored = CID.from_binary(binary)
        assert restored == cid

    def test_verify_correct_data(self):
        data = b"hello neuralis"
        cid = CID.from_bytes(data)
        assert cid.verify(data) is True

    def test_verify_wrong_data(self):
        cid = CID.from_bytes(b"original")
        assert cid.verify(b"tampered") is False

    def test_verify_empty(self):
        cid = CID.from_bytes(b"")
        assert cid.verify(b"") is True
        assert cid.verify(b"x") is False

    def test_equality(self):
        cid1 = CID.from_bytes(b"same")
        cid2 = CID.from_bytes(b"same")
        cid3 = CID.from_bytes(b"different")
        assert cid1 == cid2
        assert cid1 != cid3

    def test_string_equality(self):
        cid = CID.from_bytes(b"test")
        assert cid == str(cid)

    def test_hashable(self):
        cid1 = CID.from_bytes(b"a")
        cid2 = CID.from_bytes(b"b")
        s = {cid1, cid2, cid1}
        assert len(s) == 2

    def test_usable_as_dict_key(self):
        cid = CID.from_bytes(b"key")
        d = {cid: "value"}
        assert d[cid] == "value"

    def test_different_codecs_not_equal(self):
        cid1 = CID.from_bytes(b"data", Codec.RAW)
        cid2 = CID.from_bytes(b"data", Codec.DAG_CBOR)
        assert cid1 != cid2

    def test_codec_preserved(self):
        cid = CID.from_bytes(b"dag", Codec.DAG_CBOR)
        s = str(cid)
        restored = CID.from_str(s)
        assert restored.codec == Codec.DAG_CBOR

    def test_deterministic(self):
        cids = [CID.from_bytes(b"deterministic") for _ in range(5)]
        assert all(c == cids[0] for c in cids)

    def test_unique_for_different_inputs(self):
        cids = {CID.from_bytes(bytes([i])) for i in range(20)}
        assert len(cids) == 20

    def test_from_str_invalid_prefix(self):
        with pytest.raises(ValueError, match="base32"):
            CID.from_str("znotbase32")

    def test_from_str_invalid_base32(self):
        with pytest.raises(ValueError):
            CID.from_str("b!!!!invalid!!!!")

    def test_repr_contains_cid(self):
        cid = CID.from_bytes(b"repr test")
        r = repr(cid)
        assert "CID" in r
        assert "RAW" in r

    def test_wrong_digest_length_raises(self):
        with pytest.raises(ValueError, match="digest"):
            CID(b"short", Codec.RAW)

    def test_all_codecs_round_trip(self):
        for codec in Codec:
            cid = CID.from_bytes(b"codec test", codec)
            s = str(cid)
            restored = CID.from_str(s)
            assert restored.codec == codec


# ===========================================================================
# 3. BlockStore
# ===========================================================================

class TestBlockStore:
    def test_open_creates_dirs(self, tmp_path):
        store = BlockStore(tmp_path / "repo")
        store.open()
        assert (tmp_path / "repo" / "blocks").exists()
        assert (tmp_path / "repo" / "meta").exists()
        store.close()

    def test_put_returns_cid(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = store.put(b"hello")
            assert isinstance(cid, CID)

    def test_put_is_deterministic(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid1 = store.put(b"same data")
            cid2 = store.put(b"same data")
            assert cid1 == cid2

    def test_put_idempotent(self, tmp_path):
        with BlockStore(tmp_path) as store:
            store.put(b"data")
            store.put(b"data")  # second write is no-op
            assert store.stats().total_blocks == 1

    def test_get_returns_data(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = store.put(b"retrieve me")
            data = store.get(cid)
            assert data == b"retrieve me"

    def test_get_missing_raises_keyerror(self, tmp_path):
        with BlockStore(tmp_path) as store:
            phantom = CID.from_bytes(b"not here")
            with pytest.raises(KeyError):
                store.get(phantom)

    def test_has_true_after_put(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = store.put(b"present")
            assert store.has(cid) is True

    def test_has_false_for_missing(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = CID.from_bytes(b"absent")
            assert store.has(cid) is False

    def test_delete_removes_block(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = store.put(b"delete me")
            assert store.delete(cid) is True
            assert store.has(cid) is False

    def test_delete_missing_returns_false(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = CID.from_bytes(b"not here")
            assert store.delete(cid) is False

    def test_stats_track_blocks(self, tmp_path):
        with BlockStore(tmp_path) as store:
            store.put(b"one")
            store.put(b"two")
            store.put(b"three")
            assert store.stats().total_blocks == 3

    def test_stats_track_bytes(self, tmp_path):
        with BlockStore(tmp_path) as store:
            data = b"x" * 100
            store.put(data)
            assert store.stats().total_bytes == 100

    def test_stats_decrease_on_delete(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = store.put(b"y" * 50)
            store.delete(cid)
            assert store.stats().total_blocks == 0

    def test_stats_persist_across_reopen(self, tmp_path):
        repo = tmp_path / "repo"
        with BlockStore(repo) as store:
            store.put(b"persist me")

        with BlockStore(repo) as store2:
            assert store2.stats().total_blocks == 1

    def test_list_cids(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cids = {store.put(bytes([i])) for i in range(5)}
            listed = set(store.list_cids())
            assert cids == listed

    def test_stat_returns_meta(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = store.put(b"z" * 42)
            meta = store.stat(cid)
            assert meta is not None
            assert meta.size == 42
            assert meta.codec == "RAW"

    def test_stat_missing_returns_none(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid = CID.from_bytes(b"missing")
            assert store.stat(cid) is None

    def test_gc_deletes_orphans(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid_keep = store.put(b"keep")
            cid_orphan = store.put(b"orphan")
            deleted = store.gc_orphans({cid_keep})
            assert deleted == 1
            assert store.has(cid_keep) is True
            assert store.has(cid_orphan) is False

    def test_gc_all_pinned_deletes_nothing(self, tmp_path):
        with BlockStore(tmp_path) as store:
            cid1 = store.put(b"a")
            cid2 = store.put(b"b")
            deleted = store.gc_orphans({cid1, cid2})
            assert deleted == 0

    def test_block_too_large_raises(self, tmp_path):
        with BlockStore(tmp_path) as store:
            with pytest.raises(ValueError, match="too large"):
                store.put(b"x" * (MAX_BLOCK_SIZE + 1))

    def test_integrity_check_on_get(self, tmp_path):
        """Corrupt a block file on disk and verify get() raises."""
        with BlockStore(tmp_path) as store:
            cid = store.put(b"integrity test")
            path = store._block_path(cid)
            path.write_bytes(b"corrupted data that wont match the hash")
            with pytest.raises(ValueError, match="integrity"):
                store.get(cid)

    def test_context_manager(self, tmp_path):
        with BlockStore(tmp_path) as store:
            assert store._open is True
        assert store._open is False

    def test_operations_require_open(self, tmp_path):
        store = BlockStore(tmp_path)
        cid = CID.from_bytes(b"test")
        with pytest.raises(RuntimeError):
            store.put(b"data")
        with pytest.raises(RuntimeError):
            store.get(cid)
        with pytest.raises(RuntimeError):
            store.has(cid)

    def test_recount(self, tmp_path):
        repo = tmp_path / "repo"
        with BlockStore(repo) as store:
            store.put(b"a")
            store.put(b"b")
            store._stats.total_blocks = 0   # corrupt in-memory stats
            store.recount()
            assert store.stats().total_blocks == 2

    def test_repr(self, tmp_path):
        store = BlockStore(tmp_path)
        assert "BlockStore" in repr(store)


# ===========================================================================
# 4. PinManager
# ===========================================================================

class TestPinManager:
    def test_open_creates_dirs(self, tmp_path):
        pm = PinManager(tmp_path)
        pm.open()
        assert (tmp_path / "pins").exists()
        pm.close()

    def test_pin_and_is_pinned(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"pin me")
            pm.pin(cid)
            assert pm.is_pinned(cid) is True

    def test_pin_idempotent(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"idempotent")
            pm.pin(cid)
            pm.pin(cid)
            assert pm.count() == 1

    def test_unpin(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"unpin me")
            pm.pin(cid)
            assert pm.unpin(cid) is True
            assert pm.is_pinned(cid) is False

    def test_unpin_missing_returns_false(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"not pinned")
            assert pm.unpin(cid) is False

    def test_pin_with_name(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"named")
            pm.pin(cid, name="my-content")
            record = pm.get_pin(cid)
            assert record.name == "my-content"

    def test_pin_with_tags(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"tagged")
            pm.pin(cid, tags=["models", "local"])
            record = pm.get_pin(cid)
            assert "models" in record.tags

    def test_pin_type_recursive_default(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"recursive")
            pm.pin(cid)
            assert pm.get_pin(cid).pin_type == PinType.RECURSIVE

    def test_pin_type_direct(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"direct")
            pm.pin(cid, pin_type=PinType.DIRECT)
            assert pm.get_pin(cid).pin_type == PinType.DIRECT

    def test_pinned_cids_set(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cids = {CID.from_bytes(bytes([i])) for i in range(5)}
            for cid in cids:
                pm.pin(cid)
            assert pm.pinned_cids() == cids

    def test_list_pins(self, tmp_path):
        with PinManager(tmp_path) as pm:
            pm.pin(CID.from_bytes(b"a"))
            pm.pin(CID.from_bytes(b"b"))
            pm.pin(CID.from_bytes(b"c"), pin_type=PinType.DIRECT)
            all_pins = pm.list_pins()
            assert len(all_pins) == 3
            recursive = pm.list_pins(pin_type=PinType.RECURSIVE)
            assert len(recursive) == 2
            direct = pm.list_pins(pin_type=PinType.DIRECT)
            assert len(direct) == 1

    def test_list_pins_by_tag(self, tmp_path):
        with PinManager(tmp_path) as pm:
            pm.pin(CID.from_bytes(b"x"), tags=["alpha"])
            pm.pin(CID.from_bytes(b"y"), tags=["beta"])
            pm.pin(CID.from_bytes(b"z"), tags=["alpha", "beta"])
            alpha = pm.list_pins(tag="alpha")
            assert len(alpha) == 2

    def test_update_pin_name(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"update")
            pm.pin(cid, name="old")
            pm.update_pin(cid, name="new")
            assert pm.get_pin(cid).name == "new"

    def test_count(self, tmp_path):
        with PinManager(tmp_path) as pm:
            for i in range(7):
                pm.pin(CID.from_bytes(bytes([i])))
            assert pm.count() == 7

    def test_total_pinned_bytes(self, tmp_path):
        with PinManager(tmp_path) as pm:
            pm.pin(CID.from_bytes(b"a"), size=100)
            pm.pin(CID.from_bytes(b"b"), size=200)
            assert pm.total_pinned_bytes() == 300

    def test_persistence_across_reopen(self, tmp_path):
        cid = CID.from_bytes(b"persist")
        with PinManager(tmp_path) as pm:
            pm.pin(cid, name="saved")

        with PinManager(tmp_path) as pm2:
            assert pm2.is_pinned(cid)
            assert pm2.get_pin(cid).name == "saved"

    def test_unpin_persists(self, tmp_path):
        cid = CID.from_bytes(b"unpin persist")
        with PinManager(tmp_path) as pm:
            pm.pin(cid)
            pm.unpin(cid)

        with PinManager(tmp_path) as pm2:
            assert not pm2.is_pinned(cid)

    def test_get_pin_missing_returns_none(self, tmp_path):
        with PinManager(tmp_path) as pm:
            cid = CID.from_bytes(b"absent")
            assert pm.get_pin(cid) is None

    def test_operations_require_open(self, tmp_path):
        pm = PinManager(tmp_path)
        cid = CID.from_bytes(b"test")
        with pytest.raises(RuntimeError):
            pm.pin(cid)
        with pytest.raises(RuntimeError):
            pm.is_pinned(cid)

    def test_repr(self, tmp_path):
        pm = PinManager(tmp_path)
        assert "PinManager" in repr(pm)


# ===========================================================================
# 5. IPFSStore (async)
# ===========================================================================

class TestIPFSStore:
    @pytest.mark.asyncio
    async def test_start_stop(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        assert store._running is True
        await store.stop()
        assert store._running is False

    @pytest.mark.asyncio
    async def test_add_returns_cid(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"hello neuralis")
        assert isinstance(cid, CID)
        await store.stop()

    @pytest.mark.asyncio
    async def test_add_get_round_trip(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        data = b"round trip content"
        cid = await store.add(data)
        retrieved = await store.get(cid)
        assert retrieved == data
        await store.stop()

    @pytest.mark.asyncio
    async def test_get_missing_raises(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        phantom = CID.from_bytes(b"not stored")
        with pytest.raises(ContentNotFound):
            await store.get(phantom)
        await store.stop()

    @pytest.mark.asyncio
    async def test_auto_pin_on_add(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"auto-pin me")
        assert await store.is_pinned(cid) is True
        await store.stop()

    @pytest.mark.asyncio
    async def test_explicit_pin_unpin(self, tmp_path):
        node = make_node(tmp_path)
        node.config.storage.auto_pin_local = False
        store = IPFSStore(store._node if False else node)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"manual pin", pin=True)
        assert await store.is_pinned(cid)
        assert await store.unpin(cid) is True
        assert await store.is_pinned(cid) is False
        await store.stop()

    @pytest.mark.asyncio
    async def test_has(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"has test")
        absent = CID.from_bytes(b"absent")
        assert await store.has(cid) is True
        assert await store.has(absent) is False
        await store.stop()

    @pytest.mark.asyncio
    async def test_ls(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        await store.add(b"one", name="first")
        await store.add(b"two", name="second")
        pins = await store.ls()
        assert len(pins) == 2
        names = {p["name"] for p in pins}
        assert "first" in names and "second" in names
        await store.stop()

    @pytest.mark.asyncio
    async def test_gc_removes_unpinned(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"gc target", pin=False)
        # Manually ensure not pinned
        store._pins.unpin(cid) if store._pins.is_pinned(cid) else None
        deleted = await store.gc()
        assert deleted >= 1
        await store.stop()

    @pytest.mark.asyncio
    async def test_gc_keeps_pinned(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"keep this", pin=True)
        deleted = await store.gc()
        assert deleted == 0
        assert await store.has(cid) is True
        await store.stop()

    @pytest.mark.asyncio
    async def test_stat_existing(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        data = b"stat me" * 10
        cid = await store.add(data, name="stat-test")
        info = await store.stat(cid)
        assert info["exists"] is True
        assert info["size"] == len(data)
        assert info["pinned"] is True
        assert info["pin_name"] == "stat-test"
        await store.stop()

    @pytest.mark.asyncio
    async def test_stat_missing(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        phantom = CID.from_bytes(b"phantom")
        info = await store.stat(phantom)
        assert info["exists"] is False
        assert info["pinned"] is False
        await store.stop()

    @pytest.mark.asyncio
    async def test_repo_stat(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        await store.add(b"data1")
        await store.add(b"data2")
        rs = await store.repo_stat()
        assert rs["total_blocks"] >= 2
        assert rs["total_pins"] >= 2
        assert rs["max_bytes"] > 0
        assert rs["free_bytes"] >= 0
        assert "repo_path" in rs
        await store.stop()

    @pytest.mark.asyncio
    async def test_add_file(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()

        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"file content " * 100)

        manifest_cid = await store.add_file(test_file, name="test.txt")
        assert isinstance(manifest_cid, CID)
        assert await store.is_pinned(manifest_cid)

        await store.stop()

    @pytest.mark.asyncio
    async def test_add_file_get_file_round_trip(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()

        original = os.urandom(1024 * 50)   # 50 KB
        test_file = tmp_path / "random.bin"
        test_file.write_bytes(original)

        manifest_cid = await store.add_file(test_file)
        recovered = await store.get_file(manifest_cid)
        assert recovered == original

        await store.stop()

    @pytest.mark.asyncio
    async def test_add_file_chunked(self, tmp_path):
        """File larger than chunk_size should produce multiple chunks."""
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()

        chunk_size = 1024
        data = os.urandom(chunk_size * 5 + 100)  # 5+ chunks
        test_file = tmp_path / "chunked.bin"
        test_file.write_bytes(data)

        manifest_cid = await store.add_file(test_file, chunk_size=chunk_size)

        # Verify manifest has correct chunk count
        manifest_bytes = await store.get(manifest_cid)
        manifest = json.loads(manifest_bytes)
        assert manifest["type"] == "file"
        assert len(manifest["chunks"]) == 6   # 5 full + 1 partial

        recovered = await store.get_file(manifest_cid)
        assert recovered == data

        await store.stop()

    @pytest.mark.asyncio
    async def test_add_file_not_found(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        with pytest.raises(FileNotFoundError):
            await store.add_file(tmp_path / "nonexistent.bin")
        await store.stop()

    @pytest.mark.asyncio
    async def test_storage_limit_enforced(self, tmp_path):
        node = make_node(tmp_path)
        node.config.storage.max_storage_gb = 0.000001  # ~1 KB limit
        store = IPFSStore(node)
        await store.start()
        with pytest.raises(StorageLimitExceeded):
            await store.add(b"x" * 10_000)
        await store.stop()

    @pytest.mark.asyncio
    async def test_add_empty_bytes(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"")
        data = await store.get(cid)
        assert data == b""
        await store.stop()

    @pytest.mark.asyncio
    async def test_node_registration(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        node.register_subsystem.assert_called_with("store", store)
        node.on_shutdown.assert_called()
        await store.stop()

    @pytest.mark.asyncio
    async def test_not_running_raises(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        with pytest.raises(RuntimeError):
            await store.add(b"not running")

    @pytest.mark.asyncio
    async def test_add_with_tags(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        cid = await store.add(b"tagged", tags=["model", "local"])
        info = await store.stat(cid)
        assert "model" in info["pin_tags"]
        await store.stop()

    @pytest.mark.asyncio
    async def test_ls_filter_by_tag(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()
        await store.add(b"alpha", tags=["alpha"])
        await store.add(b"beta", tags=["beta"])
        await store.add(b"both", tags=["alpha", "beta"])

        loop = asyncio.get_event_loop()
        alpha_pins = store._pins.list_pins(tag="alpha")
        assert len(alpha_pins) == 2

        await store.stop()

    @pytest.mark.asyncio
    async def test_repr(self, tmp_path):
        node = make_node(tmp_path)
        store = IPFSStore(node)
        assert "IPFSStore" in repr(store)


# ===========================================================================
# 6. Integration
# ===========================================================================

class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, tmp_path):
        """Complete add → pin → gc → verify pipeline."""
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()

        # Add several items
        data_a = b"content alpha"
        data_b = b"content beta"
        data_c = b"content gamma"

        cid_a = await store.add(data_a, name="alpha", tags=["keep"])
        cid_b = await store.add(data_b, name="beta", tags=["keep"])
        cid_c = await store.add(data_c, name="gamma", pin=True)

        # All pinned — GC should delete nothing
        deleted = await store.gc()
        assert deleted == 0

        # Unpin gamma
        await store.unpin(cid_c)
        deleted = await store.gc()
        assert deleted == 1
        assert not await store.has(cid_c)

        # Alpha and beta still intact
        assert await store.get(cid_a) == data_a
        assert await store.get(cid_b) == data_b

        # repo_stat reflects reality
        rs = await store.repo_stat()
        assert rs["total_blocks"] == 2
        assert rs["total_pins"] == 2

        await store.stop()

    @pytest.mark.asyncio
    async def test_cid_integrity_end_to_end(self, tmp_path):
        """Store, retrieve, and verify CID integrity."""
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()

        data = os.urandom(4096)
        cid = await store.add(data)

        # CID correctly identifies content
        assert cid.verify(data)
        assert not cid.verify(b"wrong")

        retrieved = await store.get(cid)
        assert cid.verify(retrieved)

        await store.stop()

    @pytest.mark.asyncio
    async def test_large_file_chunked_and_reassembled(self, tmp_path):
        """1 MB file, 256 KB chunks, full round-trip."""
        node = make_node(tmp_path)
        store = IPFSStore(node)
        await store.start()

        data = os.urandom(1024 * 1024)   # 1 MB
        test_file = tmp_path / "large.bin"
        test_file.write_bytes(data)

        manifest_cid = await store.add_file(test_file, name="large.bin")
        recovered = await store.get_file(manifest_cid)

        assert recovered == data
        assert len(recovered) == 1024 * 1024

        # Manifest is pinned
        assert await store.is_pinned(manifest_cid)

        # repo has manifest + 4 chunks = 5 blocks
        rs = await store.repo_stat()
        assert rs["total_blocks"] == 5

        await store.stop()

    @pytest.mark.asyncio
    async def test_blockstore_and_pinmanager_independent(self, tmp_path):
        """BlockStore and PinManager work correctly when used directly."""
        repo = tmp_path / "repo"

        with BlockStore(repo) as bs:
            cid1 = bs.put(b"block one")
            cid2 = bs.put(b"block two")

        with PinManager(repo) as pm:
            pm.pin(cid1, name="pinned")
            assert pm.is_pinned(cid1)
            assert not pm.is_pinned(cid2)
            pinned = pm.pinned_cids()

        with BlockStore(repo) as bs:
            deleted = bs.gc_orphans(pinned)
            assert deleted == 1
            assert bs.has(cid1)
            assert not bs.has(cid2)

    def test_cid_set_operations(self):
        """CIDs work correctly in sets for GC operations."""
        cids = {CID.from_bytes(bytes([i])) for i in range(10)}
        pinned = {CID.from_bytes(bytes([i])) for i in range(5)}
        orphans = cids - pinned
        assert len(orphans) == 5
        assert all(c not in pinned for c in orphans)
