"""
neuralis.cli
============
Command-line interface for neuralis-node.

Commands
--------
    neuralis-node boot            Start the node (blocks until SIGINT/SIGTERM)
    neuralis-node identity        Print node identity (creates one if absent)
    neuralis-node status          Print current node status as JSON
    neuralis-node config          Print active config as JSON
    neuralis-node config --reset  Delete config file and regenerate defaults

Usage
-----
    pip install -e .
    neuralis-node boot --alias my-node
    neuralis-node identity
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def cmd_boot(args):
    """Boot the node and block until shutdown."""
    from neuralis.node import Node, NodeState

    node = Node.boot(alias=args.alias or None)
    print(f"\n  Node running: {node.identity.node_id}")
    print(f"  Press Ctrl+C to stop.\n")

    try:
        while node.state == NodeState.RUNNING:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()


def cmd_identity(args):
    """Print or create node identity."""
    from neuralis.config import NodeConfig
    from neuralis.identity import NodeIdentity

    cfg = NodeConfig.load()
    identity = NodeIdentity.load_or_create(key_dir=cfg.key_dir)
    card = identity.signed_peer_card()
    print(json.dumps(card, indent=2))


def cmd_status(args):
    """Print node status (boots briefly, prints, shuts down)."""
    from neuralis.node import Node

    node = Node.boot()
    status = node.status()
    node.shutdown()
    print(json.dumps(status, indent=2))


def cmd_config(args):
    """Print active config or reset to defaults."""
    from neuralis.config import NodeConfig, CONFIG_FILE
    import dataclasses

    if args.reset:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
            print(f"Deleted {CONFIG_FILE}")
        cfg = NodeConfig.defaults()
        cfg.save(CONFIG_FILE)
        print(f"Default config written to {CONFIG_FILE}")
        return

    cfg = NodeConfig.load()
    print(json.dumps(dataclasses.asdict(cfg), indent=2))


def main():
    parser = argparse.ArgumentParser(
        prog="neuralis-node",
        description="Neuralis decentralized AI-native internet node",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # boot
    boot_p = sub.add_parser("boot", help="Start the node")
    boot_p.add_argument("--alias", "-a", help="Human-readable node alias")
    boot_p.set_defaults(func=cmd_boot)

    # identity
    id_p = sub.add_parser("identity", help="Show node identity")
    id_p.set_defaults(func=cmd_identity)

    # status
    st_p = sub.add_parser("status", help="Show node status")
    st_p.set_defaults(func=cmd_status)

    # config
    cfg_p = sub.add_parser("config", help="Show or reset config")
    cfg_p.add_argument("--reset", action="store_true", help="Reset config to defaults")
    cfg_p.set_defaults(func=cmd_config)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
