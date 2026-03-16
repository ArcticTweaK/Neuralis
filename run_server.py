"""
Neuralis — Full Stack Dev Server (run_server.py)
================================================
Boots ALL subsystems in the correct order:
  1. Node.boot()           — identity, config, lifecycle root
  2. MeshHost              — P2P TCP listener + mDNS discovery
  3. IPFSStore             — content-addressable local blockstore
  4. AgentRuntime          — local LLM inference engine + agent bus
  5. ProtocolRouter        — inter-node task routing (requires mesh + runtime)
  6. FastAPI + uvicorn     — Canvas API on http://127.0.0.1:7100

Usage:
    python run_server.py [alias]
"""

import asyncio
import logging
import sys

logger = logging.getLogger("neuralis.devserver")


async def main() -> None:
    alias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"

    # ------------------------------------------------------------------
    # 1. Boot the Node (identity + config + lifecycle root)
    # ------------------------------------------------------------------
    from neuralis.node import Node

    node = Node.boot(alias=alias)

    # ------------------------------------------------------------------
    # 2. Mesh transport — P2P listener + mDNS peer discovery
    # ------------------------------------------------------------------
    from neuralis.mesh.host import MeshHost

    mesh = MeshHost(node)
    await mesh.start()

    # ------------------------------------------------------------------
    # 3. IPFS blockstore — content-addressable local storage
    # ------------------------------------------------------------------
    from neuralis.store.ipfs_store import IPFSStore

    store = IPFSStore(node)
    await store.start()

    # ------------------------------------------------------------------
    # 4. Agent runtime — local LLM inference + plugin agents
    # ------------------------------------------------------------------
    from neuralis.agents.runtime import AgentRuntime

    runtime = AgentRuntime(node)
    await runtime.start()

    # ------------------------------------------------------------------
    # 5. Protocol router — inter-node task routing over the mesh
    # ------------------------------------------------------------------
    from neuralis.protocol.router import ProtocolRouter

    proto = ProtocolRouter(node, mesh, runtime)
    await proto.start()

    # ------------------------------------------------------------------
    # 6. Canvas API — FastAPI REST + WebSocket on http://127.0.0.1:7100
    # ------------------------------------------------------------------
    from neuralis.api.app import create_app, serve

    app = create_app(node)

    logger.info(
        "Neuralis fully started — %d subsystems active: %s",
        len(node.subsystems),
        list(node.subsystems.keys()),
    )

    await serve(app, node.config.api)


if __name__ == "__main__":
    asyncio.run(main())
