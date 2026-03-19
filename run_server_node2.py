import asyncio
import base64
import logging
import sys
from pathlib import Path

logger = logging.getLogger("neuralis.devserver")

async def main() -> None:
    from neuralis.node import Node
    from neuralis.mesh.host import MeshHost
    from neuralis.store.ipfs_store import IPFSStore
    from neuralis.agents.runtime import AgentRuntime
    from neuralis.protocol.router import ProtocolRouter
    from neuralis.api.app import create_app, serve
    from neuralis.mesh.peers import MessageType

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    alias = sys.argv[2] if len(sys.argv) > 2 else "node-2"

    node = Node.boot(config_path=config_path, alias=alias)

    mesh = MeshHost(node)
    await mesh.start()

    store = IPFSStore(node)
    await store.start()

    runtime = AgentRuntime(node)
    await runtime.start()

    proto = ProtocolRouter(node, mesh, runtime)
    await proto.start()

    async def _handle_content_request(envelope, peer):
        from neuralis.store.cid import CID
        cid_str = envelope.payload.get("cid")
        if not cid_str:
            return
        try:
            data = await store.get(CID.from_str(cid_str))
            await mesh.send_to(
                envelope.sender_id,
                MessageType.CONTENT_RESPONSE,
                {"cid": cid_str, "data": base64.b64encode(data).decode()},
            )
        except Exception:
            pass

    mesh.on_message(MessageType.CONTENT_REQUEST, _handle_content_request)

    app = create_app(node)
    await serve(app, node.config.api)

if __name__ == "__main__":
    asyncio.run(main())
