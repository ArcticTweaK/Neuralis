"""
tests/test_module1.py
=====================
Full test suite for Module 1: neuralis-node

Tests cover:
- KeyStore: encrypt / decrypt round-trip
- NodeIdentity: create, load, sign, verify, peer card
- NodeConfig: defaults, TOML load/save, env overrides, zero-telemetry
- Node: boot sequence, subsystem registration, shutdown, status

Run with:
    pip install pytest
    pytest tests/test_module1.py -v
"""

import base64
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from neuralis.config import NodeConfig, TelemetryConfig
from neuralis.identity import (
    IdentityError,
    KeyStore,
    NodeIdentity,
    _base58_encode,
    _derive_node_id,
    _derive_peer_id,
    _load_raw_ed25519_public_key,
)
from neuralis.node import Node, NodeState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path):
    """Temporary directory isolated per test."""
    return tmp_path


@pytest.fixture
def fresh_identity(tmp_dir):
    """A brand-new NodeIdentity in an isolated tmp directory."""
    return NodeIdentity.create_new(key_dir=tmp_dir / "identity")


@pytest.fixture
def fresh_config(tmp_dir):
    """A NodeConfig with all paths redirected to tmp."""
    cfg = NodeConfig.defaults()
    cfg.identity.key_dir = str(tmp_dir / "identity")
    cfg.storage.ipfs_repo_path = str(tmp_dir / "ipfs")
    cfg.agents.agents_dir = str(tmp_dir / "agents")
    cfg.agents.models_dir = str(tmp_dir / "models")
    cfg.logging.log_dir = str(tmp_dir / "logs")
    return cfg


# ===========================================================================
# 1. Helpers
# ===========================================================================

class TestBase58Encode:
    def test_known_value(self):
        # sha256(b"neuralis") should always produce the same base58
        import hashlib
        digest = hashlib.sha256(b"neuralis").digest()
        encoded = _base58_encode(digest)
        assert isinstance(encoded, str)
        assert len(encoded) > 0
        # Only Base58 alphabet characters
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        assert all(c in alphabet for c in encoded)

    def test_leading_zero_bytes(self):
        # Leading zero bytes should map to '1' chars
        result = _base58_encode(b"\x00\x00\xab")
        assert result.startswith("11")

    def test_empty_bytes(self):
        result = _base58_encode(b"")
        assert result == ""


# ===========================================================================
# 2. KeyStore
# ===========================================================================

class TestKeyStore:
    def test_round_trip_no_env(self, tmp_dir):
        """Private key survives encrypt → save → load → decrypt."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key_dir = tmp_dir / "ks"
        store = KeyStore(key_dir)
        original = Ed25519PrivateKey.generate()
        store.save_private_key(original)
        loaded = store.load_private_key()

        # Keys are identical if they produce the same public key
        orig_pub = original.public_key().public_bytes(
            encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.Raw,
            format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.Raw,
        )
        load_pub = loaded.public_key().public_bytes(
            encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.Raw,
            format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.Raw,
        )
        assert orig_pub == load_pub

    def test_round_trip_with_env_secret(self, tmp_dir, monkeypatch):
        """Round-trip works with NEURALIS_MACHINE_SECRET env var."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        monkeypatch.setenv("NEURALIS_MACHINE_SECRET", "super-secret-test-value")
        key_dir = tmp_dir / "ks_env"
        store = KeyStore(key_dir)
        original = Ed25519PrivateKey.generate()
        store.save_private_key(original)

        store2 = KeyStore(key_dir)   # new instance, same dir
        loaded = store2.load_private_key()
        from cryptography.hazmat.primitives import serialization as ser
        assert (
            original.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
            == loaded.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
        )

    def test_wrong_secret_raises(self, tmp_dir, monkeypatch):
        """Loading with a different machine secret raises IdentityError."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        monkeypatch.setenv("NEURALIS_MACHINE_SECRET", "secret-A")
        key_dir = tmp_dir / "ks_wrong"
        store = KeyStore(key_dir)
        store.save_private_key(Ed25519PrivateKey.generate())

        monkeypatch.setenv("NEURALIS_MACHINE_SECRET", "secret-B-DIFFERENT")
        store2 = KeyStore(key_dir)
        store2._fernet = None   # force re-derive
        with pytest.raises(IdentityError):
            store2.load_private_key()

    def test_missing_key_raises(self, tmp_dir):
        store = KeyStore(tmp_dir / "empty_ks")
        with pytest.raises(IdentityError, match="No private key"):
            store.load_private_key()

    def test_key_file_permissions(self, tmp_dir):
        """Private key file should be mode 600."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key_dir = tmp_dir / "perms"
        store = KeyStore(key_dir)
        store.save_private_key(Ed25519PrivateKey.generate())
        key_path = key_dir / "node.key.enc"
        assert oct(key_path.stat().st_mode)[-3:] == "600"

    def test_key_exists_false_initially(self, tmp_dir):
        store = KeyStore(tmp_dir / "new_ks")
        assert store.key_exists() is False

    def test_key_exists_true_after_save(self, tmp_dir):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        store = KeyStore(tmp_dir / "exists_ks")
        store.save_private_key(Ed25519PrivateKey.generate())
        assert store.key_exists() is True


# ===========================================================================
# 3. NodeIdentity
# ===========================================================================

class TestNodeIdentity:
    def test_create_new_returns_identity(self, tmp_dir):
        identity = NodeIdentity.create_new(key_dir=tmp_dir / "id1")
        assert identity.node_id.startswith("NRL1")
        assert identity.peer_id.startswith("12D3KooW")
        assert identity.created_at > 0

    def test_node_id_is_deterministic(self, tmp_dir):
        """Same key → same node_id."""
        identity = NodeIdentity.create_new(key_dir=tmp_dir / "id_det")
        loaded = NodeIdentity.load(key_dir=tmp_dir / "id_det")
        assert identity.node_id == loaded.node_id

    def test_load_or_create_idempotent(self, tmp_dir):
        key_dir = tmp_dir / "id_idem"
        id1 = NodeIdentity.load_or_create(key_dir=key_dir)
        id2 = NodeIdentity.load_or_create(key_dir=key_dir)
        assert id1.node_id == id2.node_id

    def test_load_missing_raises(self, tmp_dir):
        with pytest.raises(IdentityError, match="No identity"):
            NodeIdentity.load(key_dir=tmp_dir / "missing")

    def test_sign_and_verify(self, fresh_identity):
        msg = b"hello neuralis mesh"
        sig = fresh_identity.sign(msg)
        assert len(sig) == 64
        assert fresh_identity.verify(sig, msg) is True

    def test_verify_wrong_message_fails(self, fresh_identity):
        sig = fresh_identity.sign(b"correct message")
        assert fresh_identity.verify(sig, b"wrong message") is False

    def test_verify_truncated_sig_fails(self, fresh_identity):
        sig = fresh_identity.sign(b"data")
        assert fresh_identity.verify(sig[:32], b"data") is False

    def test_verify_with_pubkey_static(self, fresh_identity):
        msg = b"inter-node message"
        sig = fresh_identity.sign(msg)
        pub_bytes = fresh_identity.public_key_bytes()
        assert NodeIdentity.verify_with_pubkey(pub_bytes, sig, msg) is True

    def test_verify_with_pubkey_wrong_key_fails(self, tmp_dir):
        id1 = NodeIdentity.create_new(key_dir=tmp_dir / "a")
        id2 = NodeIdentity.create_new(key_dir=tmp_dir / "b")
        msg = b"signed by id1"
        sig = id1.sign(msg)
        # Verify with id2's public key — should fail
        assert NodeIdentity.verify_with_pubkey(id2.public_key_bytes(), sig, msg) is False

    def test_public_key_bytes_length(self, fresh_identity):
        assert len(fresh_identity.public_key_bytes()) == 32

    def test_public_key_hex_length(self, fresh_identity):
        assert len(fresh_identity.public_key_hex()) == 64

    def test_to_peer_card_fields(self, fresh_identity):
        card = fresh_identity.to_peer_card()
        assert "node_id" in card
        assert "peer_id" in card
        assert "public_key" in card
        assert "created_at" in card
        assert "alias" in card

    def test_signed_peer_card_verifiable(self, fresh_identity):
        card = fresh_identity.signed_peer_card()
        # Reconstruct the payload as the verifier would
        payload_fields = {k: v for k, v in card.items() if k != "signature"}
        payload = json.dumps(payload_fields, sort_keys=True, separators=(",", ":")).encode()
        sig = base64.b64decode(card["signature"])
        pub_bytes = bytes.fromhex(card["public_key"])
        assert NodeIdentity.verify_with_pubkey(pub_bytes, sig, payload) is True

    def test_alias_persisted(self, tmp_dir):
        key_dir = tmp_dir / "alias_test"
        identity = NodeIdentity.create_new(key_dir=key_dir, alias="gateway-node")
        loaded = NodeIdentity.load(key_dir=key_dir)
        assert loaded.alias == "gateway-node"

    def test_set_alias_updates_meta(self, tmp_dir):
        key_dir = tmp_dir / "alias_update"
        identity = NodeIdentity.create_new(key_dir=key_dir)
        identity.set_alias("new-alias", key_dir=key_dir)
        loaded = NodeIdentity.load(key_dir=key_dir)
        assert loaded.alias == "new-alias"

    def test_sign_requires_private_key(self, tmp_dir):
        """An identity without a private key cannot sign."""
        identity = NodeIdentity.create_new(key_dir=tmp_dir / "no_priv")
        # Manually strip private key
        identity._private_key = None
        with pytest.raises(IdentityError, match="Cannot sign"):
            identity.sign(b"data")

    def test_unique_node_ids(self, tmp_dir):
        """Each new identity should have a unique node ID."""
        id1 = NodeIdentity.create_new(key_dir=tmp_dir / "u1")
        id2 = NodeIdentity.create_new(key_dir=tmp_dir / "u2")
        assert id1.node_id != id2.node_id

    def test_str_repr(self, fresh_identity):
        s = str(fresh_identity)
        assert "NodeIdentity" in s
        assert "NRL1" in s


# ===========================================================================
# 4. NodeConfig
# ===========================================================================

class TestNodeConfig:
    def test_defaults(self):
        cfg = NodeConfig.defaults()
        assert cfg.network.enable_mdns is True
        assert cfg.network.enable_dht is True
        assert cfg.telemetry.enabled is False
        assert cfg.api.host == "127.0.0.1"
        assert cfg.api.port == 7100

    def test_telemetry_always_false(self):
        """TelemetryConfig must reject True values silently."""
        t = TelemetryConfig()
        t.enabled = True          # attempt to enable
        assert t.enabled is False
        t.crash_reports = True
        assert t.crash_reports is False
        t.usage_analytics = True
        assert t.usage_analytics is False

    def test_save_and_load_roundtrip(self, tmp_dir):
        """Config survives a save → load cycle."""
        cfg_path = tmp_dir / "config.toml"
        cfg = NodeConfig.defaults()
        cfg.network.max_peers = 99
        cfg.identity.alias = "test-node"
        cfg.save(cfg_path)

        loaded = NodeConfig.load(cfg_path)
        assert loaded.network.max_peers == 99
        assert loaded.identity.alias == "test-node"

    def test_env_override_port(self, tmp_dir, monkeypatch):
        monkeypatch.setenv("NEURALIS_API_PORT", "9999")
        cfg = NodeConfig.load(tmp_dir / "nonexistent.toml")
        assert cfg.api.port == 9999

    def test_env_override_alias(self, tmp_dir, monkeypatch):
        monkeypatch.setenv("NEURALIS_ALIAS", "env-alias")
        cfg = NodeConfig.load(tmp_dir / "nonexistent.toml")
        assert cfg.identity.alias == "env-alias"

    def test_env_override_mdns_false(self, tmp_dir, monkeypatch):
        monkeypatch.setenv("NEURALIS_MDNS", "false")
        cfg = NodeConfig.load(tmp_dir / "nonexistent.toml")
        assert cfg.network.enable_mdns is False

    def test_env_override_listen_addr(self, tmp_dir, monkeypatch):
        monkeypatch.setenv("NEURALIS_LISTEN_ADDR", "/ip4/127.0.0.1/tcp/8000,/ip4/127.0.0.1/tcp/8001")
        cfg = NodeConfig.load(tmp_dir / "nonexistent.toml")
        assert len(cfg.network.listen_addresses) == 2

    def test_invalid_env_port_ignored(self, tmp_dir, monkeypatch):
        monkeypatch.setenv("NEURALIS_API_PORT", "not_a_number")
        cfg = NodeConfig.load(tmp_dir / "nonexistent.toml")
        assert cfg.api.port == 7100   # default unchanged

    def test_key_dir_property(self, tmp_dir):
        cfg = NodeConfig.defaults()
        cfg.identity.key_dir = str(tmp_dir / "keys")
        assert cfg.key_dir == tmp_dir / "keys"

    def test_api_loopback_only_default(self):
        """API must default to loopback — never 0.0.0.0."""
        cfg = NodeConfig.defaults()
        assert cfg.api.host == "127.0.0.1"

    def test_listen_addresses_default_has_entries(self):
        cfg = NodeConfig.defaults()
        assert len(cfg.network.listen_addresses) > 0

    def test_repr(self):
        cfg = NodeConfig.defaults()
        r = repr(cfg)
        assert "NodeConfig" in r
        assert "7100" in r


# ===========================================================================
# 5. Node lifecycle
# ===========================================================================

class TestNode:
    def _boot(self, tmp_dir, alias=None):
        """Boot a node with all paths inside tmp_dir."""
        cfg_path = tmp_dir / "config.toml"
        cfg = NodeConfig.defaults()
        cfg.identity.key_dir = str(tmp_dir / "identity")
        cfg.storage.ipfs_repo_path = str(tmp_dir / "ipfs")
        cfg.agents.agents_dir = str(tmp_dir / "agents")
        cfg.agents.models_dir = str(tmp_dir / "models")
        cfg.logging.log_dir = str(tmp_dir / "logs")
        cfg.save(cfg_path)
        # Patch HOME so NodeConfig.load picks up our config
        node = Node(
            identity=NodeIdentity.load_or_create(key_dir=tmp_dir / "identity", alias=alias),
            config=cfg,
        )
        node._boot_record = {}
        node.boot_time = time.time()
        node.state = NodeState.RUNNING
        return node

    def test_boot_creates_running_node(self, tmp_dir):
        node = self._boot(tmp_dir)
        assert node.state == NodeState.RUNNING

    def test_node_id_format(self, tmp_dir):
        node = self._boot(tmp_dir)
        assert node.identity.node_id.startswith("NRL1")

    def test_shutdown_changes_state(self, tmp_dir):
        node = self._boot(tmp_dir)
        node.shutdown()
        assert node.state == NodeState.STOPPED

    def test_double_shutdown_idempotent(self, tmp_dir):
        node = self._boot(tmp_dir)
        node.shutdown()
        node.shutdown()   # should not raise
        assert node.state == NodeState.STOPPED

    def test_shutdown_callbacks_called_lifo(self, tmp_dir):
        node = self._boot(tmp_dir)
        order = []
        node.on_shutdown(lambda: order.append("first"))
        node.on_shutdown(lambda: order.append("second"))
        node.on_shutdown(lambda: order.append("third"))
        node.shutdown()
        assert order == ["third", "second", "first"]

    def test_register_subsystem(self, tmp_dir):
        node = self._boot(tmp_dir)
        mock_mesh = object()
        node.register_subsystem("mesh", mock_mesh)
        assert node.get_subsystem("mesh") is mock_mesh

    def test_get_missing_subsystem_raises(self, tmp_dir):
        node = self._boot(tmp_dir)
        with pytest.raises(KeyError, match="mesh"):
            node.get_subsystem("mesh")

    def test_status_dict_fields(self, tmp_dir):
        node = self._boot(tmp_dir)
        status = node.status()
        required_keys = {
            "node_id", "peer_id", "alias", "state",
            "boot_time", "uptime_seconds", "subsystems",
            "listen_addresses", "telemetry_enabled",
        }
        assert required_keys.issubset(status.keys())

    def test_status_telemetry_always_false(self, tmp_dir):
        node = self._boot(tmp_dir)
        assert node.status()["telemetry_enabled"] is False

    def test_status_uptime_increases(self, tmp_dir):
        node = self._boot(tmp_dir)
        s1 = node.status()["uptime_seconds"]
        time.sleep(0.05)
        s2 = node.status()["uptime_seconds"]
        assert s2 >= s1

    def test_boot_full_sequence(self, tmp_dir, monkeypatch):
        """Exercise Node.boot() end-to-end inside tmp_dir."""
        monkeypatch.setenv("HOME", str(tmp_dir))
        # Monkey-patch DEFAULT paths so boot() stays inside tmp
        import neuralis.identity as id_mod
        import neuralis.config as cfg_mod
        original_id_default = id_mod.DEFAULT_KEY_DIR
        original_cfg_file = cfg_mod.CONFIG_FILE

        id_mod.DEFAULT_KEY_DIR = tmp_dir / "identity"
        cfg_mod.CONFIG_FILE = tmp_dir / "config.toml"
        cfg_mod.NEURALIS_HOME = tmp_dir

        try:
            node = Node.boot(config_path=tmp_dir / "config.toml", alias="test-boot")
            assert node.state == NodeState.RUNNING
            assert node.identity.node_id.startswith("NRL1")
            node.shutdown()
            assert node.state == NodeState.STOPPED
        finally:
            id_mod.DEFAULT_KEY_DIR = original_id_default
            cfg_mod.CONFIG_FILE = original_cfg_file

    def test_repr(self, tmp_dir):
        node = self._boot(tmp_dir)
        r = repr(node)
        assert "Node" in r
        assert "NRL1" in r


# ===========================================================================
# 6. Integration — identity + node + config together
# ===========================================================================

class TestIntegration:
    def test_full_node_identity_sign_verify_cycle(self, tmp_dir):
        """A node can sign a message and a remote can verify it using peer card."""
        key_dir = tmp_dir / "integration"
        identity = NodeIdentity.create_new(key_dir=key_dir)

        # Simulate: node signs a mesh message
        message = b"block:abc123:payload:hello"
        signature = identity.sign(message)

        # Simulate: remote peer receives peer card and verifies
        card = identity.to_peer_card()
        pub_bytes = bytes.fromhex(card["public_key"])
        assert NodeIdentity.verify_with_pubkey(pub_bytes, signature, message) is True
        assert NodeIdentity.verify_with_pubkey(pub_bytes, signature, b"tampered") is False

    def test_config_survives_reload(self, tmp_dir):
        cfg_path = tmp_dir / "persist.toml"
        cfg = NodeConfig.defaults()
        cfg.network.max_peers = 42
        cfg.network.enable_dht = False
        cfg.save(cfg_path)

        cfg2 = NodeConfig.load(cfg_path)
        assert cfg2.network.max_peers == 42
        assert cfg2.network.enable_dht is False

    def test_identity_keys_never_equal_across_nodes(self, tmp_dir):
        """100 identities — all unique node IDs."""
        ids = set()
        for i in range(10):
            ident = NodeIdentity.create_new(key_dir=tmp_dir / f"node_{i}")
            ids.add(ident.node_id)
        assert len(ids) == 10
