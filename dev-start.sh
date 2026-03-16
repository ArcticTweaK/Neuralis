#!/usr/bin/env bash
# ============================================================
# Neuralis — Local Development Startup Script
# ============================================================
# Usage:
#   ./dev-start.sh           — boots with alias "dev-node"
#   ./dev-start.sh my-node   — boots with a custom alias
# ============================================================

set -euo pipefail

ALIAS="${1:-dev-node}"
ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Neuralis local dev stack"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---- 1. Resolve Python — prefer .venv to avoid shell activation issues -----
if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
    PIP="$ROOT/.venv/bin/pip"
else
    PYTHON="python"
    PIP="pip"
fi

# ---- 2. Build PYTHONPATH from all sub-package source roots ------------------
export PYTHONPATH="\
$ROOT/neuralis-node:\
$ROOT/agent-runtime:\
$ROOT/agent-protocol:\
$ROOT/canvas-api:\
$ROOT/crypto-layer:\
$ROOT/mesh-transport:\
$ROOT/ipfs-store"

echo "  Python    → $PYTHON"
echo "  PYTHONPATH set to all sub-package roots"

# ---- 3. Ensure runtime deps ------------------------------------------------
"$PYTHON" -c "import uvicorn, fastapi, websockets" 2>/dev/null || {
    echo "▶ Installing runtime deps (uvicorn[standard], fastapi) …"
    "$PIP" install -q "uvicorn[standard]" fastapi
}

echo ""
echo "▶ Booting node (alias: $ALIAS) + Canvas API on http://127.0.0.1:7100 …"
echo "  Agents dir → ~/.neuralis/agents/"
echo "  (Press Ctrl+C to stop)"
echo ""

# ---- 4. Run node + API (blocks until Ctrl+C) ---------------
"$PYTHON" - "$ALIAS" <<'PYEOF'
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
