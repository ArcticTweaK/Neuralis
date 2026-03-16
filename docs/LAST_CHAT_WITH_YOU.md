Running Neuralis for the First Time
IPFS subsystem registered correctly as 'ipfs', all other endpoints fully working. Last bug: routes.py calls ipfs.list_pins() but the method is named ls() in IPFSStore.

Files Edited
ipfs_store.py
run_server.py
Task
Background Steps
Running background command
Relocate
~/…/Neuralis $ cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &
import sys, asyncio
from neuralis.node import Node
from neuralis.api.app import create_app, serve
from neuralis.agents.runtime import AgentRuntime

alias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"

async def main():
node = Node.boot(alias=alias)
runtime = AgentRuntime(node)
await runtime.start()
app = create_app(node)
await serve(app, node.config.api)

asyncio.run(main())
PYEOF
echo "Server PID: $!"

arctic@nixos ~/Documents/Neuralis
❯

arctic@nixos ~/Documents/Neuralis
❯ cd /home/arctic/Documents/Neuralis && python --version && echo "---" && ls .venv/bin/python\* 2>/dev/null || echo "no venv python" && echo "---" && cat pyproject.toml 2>/dev/null || echo "no root pyproject.toml"
Python 3.13.11

---

󰡯 .venv/bin/python
󰡯 .venv/bin/python3
 .venv/bin/python3.13

---

no root pyproject.toml

…ctic@nixos ~/Documents/Neuralis
❯ cd /home/arctic/Documents/Neuralis && .venv/bin/pip list 2>/dev/null | head -60
Package Version

---

annotated-doc 0.0.4
annotated-types 0.7.0
anyio 4.12.1
certifi 2026.2.25
cffi 2.0.0
click 8.3.1
cryptography 46.0.5
fastapi 0.135.1
h11 0.16.0
httpcore 1.0.9
httptools 0.7.1
httpx 0.28.1
idna 3.11
iniconfig 2.3.0
packaging 26.0
pip 26.0.1
pluggy 1.6.0
pycparser 3.0
pydantic 2.12.5
pydantic_core 2.41.5
Pygments 2.19.2
pytest 9.0.2
pytest-asyncio 1.3.0
python-dotenv 1.2.2
PyYAML 6.0.3
starlette 0.52.1
tomli_w 1.2.0
typing_extensions 4.15.0
typing-inspection 0.4.2
uvicorn 0.41.0
uvloop 0.22.1
watchfiles 1.1.1
websockets 16.0

…ctic@nixos ~/Documents/Neuralis
❯ cat /home/arctic/Documents/Neuralis/neuralis-node/pyproject.toml && echo "===" && cat /home/arctic/Documents/Neuralis/crypto-layer/pyproject.toml && echo "===" && cat /home/arctic/Documents/Neuralis/canvas-api/pyproject.toml && echo "===" && cat /home/arctic/Documents/Neuralis/agent-runtime/pyproject.toml && echo "===" && cat /home/arctic/Documents/Neuralis/ipfs-store/pyproject.toml && echo "===" && cat /home/arctic/Documents/Neuralis/agent-protocol/pyproject.toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "neuralis-node"
version = "0.1.0"
description = "Neuralis — decentralized AI-native internet node (Module 1: core identity & config)"
readme = "README.md"
requires-python = ">=3.10"
license = {text = "MIT"}

dependencies = [ # Cryptography — Ed25519 identity, Fernet encryption at rest
"cryptography>=41.0.0",

    # TOML parsing (Python 3.11+ has tomllib built-in; 3.10 needs backport)
    "tomli>=2.0.1; python_version < '3.11'",

    # TOML writing
    "tomli-w>=1.0.0",

]

[project.optional-dependencies]
dev = [
"pytest>=7.4",
"pytest-asyncio>=0.23",
]

[project.scripts]
neuralis-node = "neuralis.cli:main"

[tool.setuptools.packages.find]
where = ["."]
include = ["neuralis*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
===
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "neuralis-crypto-layer"
version = "0.1.0"
description = "Neuralis — Application-layer cryptography (Module 8)"
readme = "README.md"
requires-python = ">=3.10"
license = {text = "MIT"}

dependencies = [
"neuralis-node>=0.1.0",
"cryptography>=41.0.0",
]

[project.optional-dependencies]
dev = [
"pytest>=7.4",
"pytest-asyncio>=0.23",
]

[tool.setuptools.packages.find]
where = ["."]
include = ["neuralis*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = [
"../neuralis-node",
".",
]
===
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "neuralis-canvas-api"
version = "0.1.0"
description = "Neuralis Module 6 — Canvas API (FastAPI bridge)"
requires-python = ">=3.11"
dependencies = [
"fastapi>=0.110.0",
"pydantic>=2.0",
"uvicorn>=0.29.0",
"httpx>=0.27.0", # for testing with AsyncClient
]

[tool.setuptools.packages.find]
where = ["."]
include = ["neuralis*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.package-dir]
"" = "."
===
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "neuralis-agent-runtime"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
inference = ["llama-cpp-python>=0.2.0"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
===
[bat error]: '/home/arctic/Documents/Neuralis/ipfs-store/pyproject.toml': No such file or directory (os error 2)

…ctic@nixos ~/Documents/Neuralis
❯ cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python -c "
import traceback

mods = [
'neuralis.node',
'neuralis.config',
'neuralis.identity',
'neuralis.crypto.signing',
'neuralis.crypto.exchange',

      'neuralis.crypto.envelope',

      'neuralis.crypto.keystore',

      'neuralis.crypto.tokens',
      'neuralis.store.blockstore',
      'neuralis.store.ipfs_store',
      'neuralis.store.pins',
      'neuralis.store.cid',
      'neuralis.agents.runtime',
      'neuralis.agents.bus',
      'neuralis.agents.inference',
      'neuralis.agents.loader',
      'neuralis.protocol.messages',
      'neuralis.protocol.router',

      'neuralis.api.app',
      'neuralis.api.routes',

]

for m in mods:
try:
**import**(m)
print(f' OK {m}')
except Exception as e:
print(f' FAIL {m}: {e}')
"
OK neuralis.node
OK neuralis.config
OK neuralis.identity
OK neuralis.crypto.signing
OK neuralis.crypto.exchange
OK neuralis.crypto.envelope
OK neuralis.crypto.keystore
OK neuralis.crypto.tokens
OK neuralis.store.blockstore
OK neuralis.store.ipfs_store
OK neuralis.store.pins
OK neuralis.store.cid
OK neuralis.agents.runtime
OK neuralis.agents.bus
OK neuralis.agents.inference
OK neuralis.agents.loader
OK neuralis.protocol.messages
OK neuralis.protocol.router
OK neuralis.api.app
OK neuralis.api.routes

…ctic@nixos ~/Documents/Neuralis
❯ cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python -c "
import sys, asyncio, traceback

async def main():
from neuralis.node import Node
from neuralis.agents.runtime import AgentRuntime
from neuralis.api.app import create_app

      print('[1] Booting node...')
      node = Node.boot(alias='test-node')
      print(f'[2] Node booted: {node.identity.node_id}')

      print('[3] Starting AgentRuntime...')
      runtime = AgentRuntime(node)
      await runtime.start()
      print('[4] AgentRuntime started')

      print('[5] Creating FastAPI app...')
      app = create_app(node)
      print('[6] App created successfully!')
      print()
      print('=== All systems nominal! ===')
      print(f'Node ID: {node.identity.node_id}')
      print(f'State:   {node.state}')
      print(f'Agents:  {runtime.loader.count()}')

      await runtime.stop()
      node.shutdown()
      print('[7] Clean shutdown complete.')

asyncio.run(main())
" 2>&1
[1] Booting node...
18:13:27 INFO neuralis.node ============================================================
18:13:27 INFO neuralis.node Neuralis Node — booting
18:13:27 INFO neuralis.node ============================================================
18:13:27 INFO neuralis.identity Loaded node identity: NRL15VR9VkWWk8ny9qm8XCVJkurJ3zJk1x9CnX2Lu7zSNepg
18:13:27 INFO neuralis.node Node alias set to: test-node
18:13:27 INFO neuralis.node Node ID : NRL15VR9VkWWk8ny9qm8XCVJkurJ3zJk1x9CnX2Lu7zSNepg
18:13:27 INFO neuralis.node Peer ID : 12D3KooW5VR9VkWWk8ny9qm8XCVJkurJ3zJk1x9C
18:13:27 INFO neuralis.node Alias : test-node
18:13:27 INFO neuralis.node API : http://127.0.0.1:7100
18:13:27 INFO neuralis.node Listen : ['/ip4/0.0.0.0/tcp/7101', '/ip4/0.0.0.0/udp/7101/quic']
18:13:27 INFO neuralis.node mDNS : True | DHT: True
18:13:27 INFO neuralis.node State : RUNNING
18:13:27 INFO neuralis.node ============================================================
[2] Node booted: NRL15VR9VkWWk8ny9qm8XCVJkurJ3zJk1x9CnX2Lu7zSNepg
[3] Starting AgentRuntime...
18:13:27 INFO neuralis.agents.runtime AgentRuntime: starting …
18:13:27 INFO neuralis.agents.loader AgentLoader: discovery complete — 0 agent(s) active
18:13:27 INFO neuralis.agents.runtime AgentRuntime: started — 0 agent(s) loaded, model=none
[4] AgentRuntime started
[5] Creating FastAPI app...
[6] App created successfully!

=== All systems nominal! ===
Node ID: NRL15VR9VkWWk8ny9qm8XCVJkurJ3zJk1x9CnX2Lu7zSNepg
State: RUNNING
Agents: 0
18:13:27 INFO neuralis.agents.runtime AgentRuntime: stopping …
18:13:27 INFO neuralis.agents.inference InferenceEngine: model unloaded
18:13:27 INFO neuralis.agents.runtime AgentRuntime: stopped
18:13:27 INFO neuralis.node Node shutting down…
18:13:27 INFO neuralis.node Node stopped. Uptime: 0.0 seconds.
[7] Clean shutdown complete.
lis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &
^
agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys,

fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asyncio
^
as-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import

fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Node
^
t:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app,
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, serve
^
"dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntime
^
"dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntime
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntime
^
asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"
^
asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"
^
Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main()
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main():
^
create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias)
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias)
^
ents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node)
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node)
^
ents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start()
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start()
^
lias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node)
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node)
^
v) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)
^
v) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)
^
ain(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)asyncio.run(main())
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)asyncio.run(main())
^
ain(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)asyncio.run(main())
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)asyncio.run(main())PYEOF
^
alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)asyncio.run(main())PYEOFecho "Server PID:
fish: Expected a string, but found a redirection
cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python - "dev-node" <<'PYEOF' > /tmp/neuralis-server.log 2>&1 &import sys, asynciofrom neuralis.node import Nodefrom neuralis.api.app import create_app, servefrom neuralis.agents.runtime import AgentRuntimealias = sys.argv[1] if len(sys.argv) > 1 else "dev-node"async def main(): node = Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)asyncio.run(main())PYEOFecho "Server PID: $!"
^
= Node.boot(alias=alias) runtime = AgentRuntime(node) await runtime.start() app = create_app(node) await serve(app, node.config.api)asyncio.run(main())PYEOFecho "Server PID: $!"

Ask every time
Cancel
Running background command
Relocate
~/…/Neuralis $ pkill -f "run_server.py" 2>/dev/null; sleep 1 && cd /home/arctic/Documents/Neuralis && PYTHONPATH="neuralis-node:agent-runtime:agent-protocol:canvas-api:crypto-layer:mesh-transport:ipfs-store" .venv/bin/python run_server.py dev-node

Ask every time
Cancel
Progress Updates
1
Checking Python environment, venv, and installed dependencies

2
Doing a dry-run import test to discover exact errors before fixing them

3
Attempting a full Node.boot() + API startup and capturing any runtime errors

4
Starting the full dev server (node + API) as a background process, then verifying all endpoints respond

5
Starting the Neuralis API server in background and verifying it responds on port 7100

6
Checking remaining API endpoints and then starting the canvas-ui frontend

7
Reading mesh, IPFS, and protocol subsystem startup patterns to wire them all up properly

8
Killing partial server, writing full-stack run_server.py that starts all 6 subsystems, then relaunching

9
Waiting for server to come up, then verifying all 5 API endpoint groups respond correctly

10
Diagnosing the IPFS subsystem key name mismatch between routes.py and ipfs_store.py

11
Verifying all API endpoints respond correctly and then starting the canvas-ui frontend

12
Fixing missing list_pins method on IPFSStore (routes.py expects it but the method is named ls())
