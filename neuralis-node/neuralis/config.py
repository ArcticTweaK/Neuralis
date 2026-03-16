"""
neuralis.config
===============
Node configuration for Neuralis.

Configuration is layered (lowest → highest priority):
    1. Built-in defaults (this file)
    2. ~/.neuralis/config.toml  (user-level, persisted)
    3. Environment variables    (NEURALIS_* prefix)
    4. Programmatic overrides   (NodeConfig.override())

All config is local-first.  There are no remote config servers, no feature
flags fetched from the cloud, no telemetry endpoints.  If a key doesn't exist
in the user's config.toml, the default is used silently.

Usage
-----
    from neuralis.config import NodeConfig

    cfg = NodeConfig.load()
    print(cfg.network.listen_addresses)
    print(cfg.storage.ipfs_repo_path)

    cfg.identity.alias = "my-node"
    cfg.save()   # writes back to ~/.neuralis/config.toml
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

try:
    import tomllib          # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # pip install tomli  (3.9 / 3.10 backport)
    except ImportError:
        tomllib = None      # will fall back to defaults-only mode

try:
    import tomli_w          # pip install tomli-w
except ImportError:
    tomli_w = None

logger = logging.getLogger(__name__)

NEURALIS_HOME = Path.home() / ".neuralis"
CONFIG_FILE = NEURALIS_HOME / "config.toml"


# ---------------------------------------------------------------------------
# Sub-configs (one dataclass per domain)
# ---------------------------------------------------------------------------

@dataclass
class IdentityConfig:
    """Settings related to node identity."""
    key_dir: str = str(NEURALIS_HOME / "identity")
    alias: Optional[str] = None


@dataclass
class NetworkConfig:
    """
    libp2p transport and discovery settings.

    listen_addresses    : multiaddrs this node binds to
    announce_addresses  : multiaddrs advertised to peers (can differ behind NAT)
    bootstrap_peers     : static peers to connect on startup (multiaddr strings)
    enable_mdns         : LAN peer discovery (zero-config, no bootstrap needed)
    enable_dht          : Kademlia DHT for WAN peer discovery
    mdns_interval_secs  : how often to send mDNS probes
    max_peers           : soft cap on simultaneous peer connections
    connection_timeout  : seconds before an outbound dial is abandoned
    """
    listen_addresses: List[str] = field(default_factory=lambda: [
        "/ip4/0.0.0.0/tcp/7101",
        "/ip4/0.0.0.0/udp/7101/quic",
    ])
    announce_addresses: List[str] = field(default_factory=list)
    bootstrap_peers: List[str] = field(default_factory=list)
    enable_mdns: bool = True
    enable_dht: bool = True
    mdns_interval_secs: int = 5
    max_peers: int = 50
    connection_timeout: int = 30


@dataclass
class StorageConfig:
    """IPFS / local storage settings."""
    ipfs_repo_path: str = str(NEURALIS_HOME / "ipfs")
    max_storage_gb: float = 10.0
    auto_pin_local: bool = True          # pin all locally created content
    garbage_collect_interval_hrs: int = 24


@dataclass
class AgentConfig:
    """AI agent runtime settings."""
    agents_dir: str = str(NEURALIS_HOME / "agents")
    models_dir: str = str(NEURALIS_HOME / "models")
    enable_auto_discover: bool = True    # scan agents_dir on startup
    max_concurrent_agents: int = 4
    inference_threads: int = 2           # CPU threads per inference session
    # Default model to load when no agent specifies one
    default_model: Optional[str] = None  # e.g. "mistral-7b-instruct-q4.gguf"


@dataclass
class APIConfig:
    """Canvas API (FastAPI) settings."""
    host: str = "127.0.0.1"             # loopback only — UI talks to local node
    port: int = 7100
    cors_origins: List[str] = field(default_factory=lambda: [
        "http://localhost:3000",         # Vite dev server
        "http://localhost:7100",
    ])
    enable_docs: bool = False            # Swagger UI — disable in production


@dataclass
class LoggingConfig:
    """Logging settings."""
    level: str = "INFO"
    log_dir: str = str(NEURALIS_HOME / "logs")
    max_log_size_mb: int = 50
    backup_count: int = 3
    enable_console: bool = True


@dataclass
class TelemetryConfig:
    """
    Telemetry — always off.

    This section exists only to be explicit.  Neuralis never phones home.
    These flags cannot be set to True by any external config source; the
    setattr guard below enforces this at runtime.
    """
    enabled: bool = False
    crash_reports: bool = False
    usage_analytics: bool = False

    def __setattr__(self, name: str, value: object) -> None:
        # Enforce zero-telemetry: silently clamp any True back to False
        if isinstance(value, bool) and value is True:
            logger.warning(
                "TelemetryConfig.%s was set to True — "
                "Neuralis is zero-telemetry; resetting to False.", name
            )
            value = False
        super().__setattr__(name, value)


# ---------------------------------------------------------------------------
# NodeConfig — root config object
# ---------------------------------------------------------------------------

@dataclass
class NodeConfig:
    """
    Root configuration object for a Neuralis node.

    All sub-configs are nested dataclasses.  Call NodeConfig.load() to
    get an instance with user overrides applied; call .save() to persist.
    """
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    agents: AgentConfig = field(default_factory=AgentConfig)
    api: APIConfig = field(default_factory=APIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: Path = CONFIG_FILE) -> "NodeConfig":
        """
        Load config from disk, apply environment overrides, return instance.

        Layer order: defaults → toml file → env vars
        """
        cfg = cls()  # start with all defaults

        if tomllib is None:
            logger.warning(
                "tomllib / tomli not available — "
                "using default config only.  pip install tomli"
            )
        elif config_path.exists():
            try:
                with open(config_path, "rb") as fh:
                    data = tomllib.load(fh)
                cfg = cls._from_dict(data)
                logger.debug("Loaded config from %s", config_path)
            except Exception as exc:
                logger.error("Failed to parse config at %s: %s — using defaults", config_path, exc)
        else:
            logger.info("No config file at %s — using defaults", config_path)
            cfg._ensure_dirs()
            cfg.save(config_path)   # write defaults for user to edit

        cfg._apply_env_overrides()
        cfg._ensure_dirs()
        return cfg

    @classmethod
    def defaults(cls) -> "NodeConfig":
        """Return a pristine default config without reading disk."""
        return cls()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, config_path: Path = CONFIG_FILE) -> None:
        """Serialise this config to TOML and write to disk."""
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if tomli_w is None:
            # Fallback: write a basic TOML manually
            self._write_toml_fallback(config_path)
            return

        data = self._to_dict()
        with open(config_path, "wb") as fh:
            tomli_w.dump(data, fh)
        logger.debug("Config saved to %s", config_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        """Create all configured directories if they don't exist."""
        dirs = [
            self.identity.key_dir,
            self.storage.ipfs_repo_path,
            self.agents.agents_dir,
            self.agents.models_dir,
            self.logging.log_dir,
        ]
        for d in dirs:
            Path(d).mkdir(parents=True, exist_ok=True)

    def _apply_env_overrides(self) -> None:
        """
        Apply NEURALIS_* environment variable overrides.

        Supported env vars:
            NEURALIS_ALIAS            → identity.alias
            NEURALIS_LISTEN_ADDR      → network.listen_addresses (comma-sep)
            NEURALIS_BOOTSTRAP_PEERS  → network.bootstrap_peers (comma-sep)
            NEURALIS_MDNS             → network.enable_mdns (true/false)
            NEURALIS_DHT              → network.enable_dht (true/false)
            NEURALIS_API_PORT         → api.port
            NEURALIS_LOG_LEVEL        → logging.level
            NEURALIS_MAX_PEERS        → network.max_peers
        """
        env_map = {
            "NEURALIS_ALIAS": ("identity", "alias", str),
            "NEURALIS_API_PORT": ("api", "port", int),
            "NEURALIS_LOG_LEVEL": ("logging", "level", str),
            "NEURALIS_MAX_PEERS": ("network", "max_peers", int),
        }
        for env_key, (section, attr, cast) in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                try:
                    setattr(getattr(self, section), attr, cast(val))
                    logger.debug("Env override: %s.%s = %s", section, attr, val)
                except (ValueError, TypeError) as exc:
                    logger.warning("Invalid env var %s=%s: %s", env_key, val, exc)

        # Comma-separated list overrides
        if listen := os.environ.get("NEURALIS_LISTEN_ADDR"):
            self.network.listen_addresses = [a.strip() for a in listen.split(",") if a.strip()]

        if bootstrap := os.environ.get("NEURALIS_BOOTSTRAP_PEERS"):
            self.network.bootstrap_peers = [p.strip() for p in bootstrap.split(",") if p.strip()]

        for flag_key, attr in [("NEURALIS_MDNS", "enable_mdns"), ("NEURALIS_DHT", "enable_dht")]:
            val = os.environ.get(flag_key)
            if val is not None:
                setattr(self.network, attr, val.lower() in ("1", "true", "yes"))

    @classmethod
    def _from_dict(cls, data: dict) -> "NodeConfig":
        """Construct a NodeConfig from a parsed TOML dict."""
        cfg = cls()
        section_map = {
            "identity": (IdentityConfig, "identity"),
            "network": (NetworkConfig, "network"),
            "storage": (StorageConfig, "storage"),
            "agents": (AgentConfig, "agents"),
            "api": (APIConfig, "api"),
            "logging": (LoggingConfig, "logging"),
            "telemetry": (TelemetryConfig, "telemetry"),
        }
        for toml_key, (klass, attr) in section_map.items():
            if toml_key in data:
                section_data = data[toml_key]
                obj = getattr(cfg, attr)
                for k, v in section_data.items():
                    if hasattr(obj, k):
                        setattr(obj, k, v)
                    else:
                        logger.warning("Unknown config key: [%s].%s — ignoring", toml_key, k)
        return cfg

    def _to_dict(self) -> dict:
        """Convert to dict, replacing None values with empty strings for TOML compatibility."""
        def sanitize(obj):
            if isinstance(obj, dict):
                return {k: sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [sanitize(i) for i in obj]
            if obj is None:
                return ""
            return obj
        return sanitize(asdict(self))

    def _write_toml_fallback(self, path: Path) -> None:
        """Write a minimal TOML without tomli_w (string building)."""
        lines = ["# Neuralis Node Configuration\n# Auto-generated — edit as needed\n\n"]
        d = self._to_dict()
        for section, values in d.items():
            lines.append(f"[{section}]\n")
            if isinstance(values, dict):
                for k, v in values.items():
                    if v is None:
                        lines.append(f"# {k} = \"\"\n")
                    elif isinstance(v, bool):
                        lines.append(f"{k} = {'true' if v else 'false'}\n")
                    elif isinstance(v, list):
                        items = ", ".join(f'"{i}"' for i in v)
                        lines.append(f"{k} = [{items}]\n")
                    elif isinstance(v, str):
                        lines.append(f'{k} = "{v}"\n')
                    else:
                        lines.append(f"{k} = {v}\n")
            lines.append("\n")
        path.write_text("".join(lines))
        logger.debug("Config saved (fallback TOML) to %s", path)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def key_dir(self) -> Path:
        return Path(self.identity.key_dir)

    @property
    def ipfs_repo(self) -> Path:
        return Path(self.storage.ipfs_repo_path)

    @property
    def agents_dir(self) -> Path:
        return Path(self.agents.agents_dir)

    def __repr__(self) -> str:
        return (
            f"<NodeConfig api={self.api.host}:{self.api.port} "
            f"peers_max={self.network.max_peers} "
            f"mdns={self.network.enable_mdns} dht={self.network.enable_dht}>"
        )
