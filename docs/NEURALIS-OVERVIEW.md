🌐 Neuralis — Complete Project Analysis
What Is Neuralis?
Neuralis is an ambitious Decentralized Physical Infrastructure Network (DePIN) — essentially a planetary-scale, self-governing AI operating system. The core idea: instead of paying a subscription to a centralized AI provider, users contribute their idle GPU/CPU compute to a global peer-to-peer mesh and earn back sovereign, uncensorable AI access in return.

The "Neuralis Flip": You become a homeowner of intelligence instead of a tenant of it.

Version: 0.9.0-BETA | Target: 100 Million Nodes | License: MIT

Architecture: 8 Modules, One Layered Stack
The entire system is organized as a monorepo of independent Python packages (plus one React frontend), all sharing a common neuralis.\* namespace. Each module is a separate pip-installable package. The
dev-start.sh
script stitches them all together at runtime via PYTHONPATH.

🖥️ canvas-ui\n(React / D3 Dashboard)
⚡ canvas-api\n(FastAPI REST + WebSocket)
📡 agent-protocol\n(Inter-Node Task Routing)
🤖 agent-runtime\n(Local LLM Inference Engine)
🔑 neuralis-node\n(Identity + Lifecycle Root)
🕸️ mesh-transport\n(P2P Layer-2 Transport)
📦 ipfs-store\n(Content-Addressable Storage)
🔐 crypto-layer\n(Application Cryptography)
Module-by-Module Breakdown
Module 1 — neuralis-node (Node Identity & Lifecycle)
Status: ✅ Complete

The root of everything. Every other module receives a
Node
reference and pulls config, identity, and shared state from it.

File Role
identity.py
Ed25519 keypair generation, NodeID = "NRL1" + hex(sha256(pubkey))[0:16], libp2p-compatible peer_id, Fernet-encrypted key persistence in ~/.neuralis/identity.key
config.py
Layered config system (hardcoded defaults → TOML file → env vars). Covers networking, storage, agents, API, logging, metrics
node.py
Node.boot() factory: loads config → sets up logging → creates identity → writes signed boot record to disk → sets state = RUNNING. Also owns subsystem registration and LIFO shutdown callbacks
cli.py
CLI entry point
Boot Sequence: Config → Identity → Boot Record (signed with Ed25519, gossiped to peers) → RUNNING → signal handlers (SIGINT/SIGTERM for graceful shutdown)

Module 2 — mesh-transport (P2P Layer-2 Transport)
Status: ✅ Complete (source dir empty — code lives inside neuralis-node)

Handles encrypted peer-to-peer communication:

Discovery: mDNS zero-conf for local network peer discovery
Handshake: HELLO frames where each peer signs their ephemeral X25519 public key with their Ed25519 identity key — prevents MITM
Session Encryption: X25519 ECDH → HKDF-SHA256 (info string: "neuralis-session-v1") → AES-256-GCM
Nonce strategy: Each side maintains its own monotonic nonce counter for replay prevention
Planned: GossipSub for cross-subnet propagation; NAT hole-punching (STUN/ICE) not yet implemented

Module 3 — ipfs-store (Content-Addressable Storage)
Status: ✅ Complete

An IPFS-inspired local blockstore:

File Role
cid.py
CID = b"b" + base32(sha256(chunk)) — multihash derivation
blockstore.py
Sharded flat-file store. Shard key = cid[1:3] → up to 1,024 subdirs. Atomic writes via .tmp + os.replace()
ipfs_store.py
High-level
add()
,
get()
,
pin()
,
unpin()
, gc(). Files > 256KB are chunked; a JSON manifest becomes the root CID
pins.py
PinManager — a JSON ledger of pinned CIDs that survive garbage collection
GC Strategy: Mark-and-sweep — mark all pinned CIDs, sweep blockstore, delete unpinned blocks.

Planned: Reed-Solomon erasure coding (currently uses whole-block replicas). At-rest block encryption (transport-layer encryption is already in place).

Module 4 — agent-runtime (Local AI Inference Engine)
Status: ✅ Complete

Runs local AI models on GGUF format using llama-cpp-python:

File Role
inference.py
InferenceEngine — loads/unloads GGUF models, runs inference via llama-cpp-python
loader.py
Plugin-based agent discovery from ~/.neuralis/agents/. Hot-reload support
bus.py
AgentBus — in-process pub/sub message bus. Agents subscribe to topics (their name + capabilities)
runtime.py
AgentRuntime
orchestrator — owns all three above, registers with
Node
, dispatches tasks
base.py
AgentMessage, AgentResponse, AgentState base types
Plugin Model: Any Python file in ~/.neuralis/agents/ exposing a class with NAME, CAPABILITIES, and a
handle(message)
async method gets auto-discovered and wired onto the bus.

Module 5 — agent-protocol (Inter-Node Task Routing)
Status: ✅ Complete

Handles routing tasks across the mesh to remote nodes:

File Role
messages.py
ProtocolMessage wire format — typed enums for HELLO, TASK_REQUEST, TASK_RESPONSE, CAPABILITY_QUERY, etc.
router.py
ProtocolRouter — maintains a registry of remote nodes + their capabilities; routes tasks using capability tokens for authorization
codec.py
Message serialization/deserialization
Module 6 — canvas-api (FastAPI Control Plane)
Status: ✅ Complete

A FastAPI REST + WebSocket server that acts as the control plane for the local node and bridges the UI to the backend:

Router Prefix Key Endpoints
node_router /api/node GET /status, PATCH /alias, POST /shutdown
peer_router /api/peers GET /, POST /connect, DELETE /{id}, GET /{id}
content_router /api/content POST / (add), GET /{cid}, POST /{cid}/pin, DELETE /{cid}/pin, GET /stats/storage
agent_router /api/agents GET /, POST /task, POST /reload, GET /{name}
protocol_router /api/protocol GET /nodes, POST /task, POST /query
All routers inject the
Node
object as a FastAPI dependency — no global state.

Module 7 — canvas-ui (React Spatial Dashboard)
Status: 🔲 In Progress

A React + Vite + TailwindCSS dashboard with a D3 force-directed graph as the main canvas:

Component Role
Canvas.jsx
Full-bleed D3 force graph showing local node + peers as interconnected nodes
NodePanel.jsx
Top-left HUD: node ID, alias, state, uptime
PeerPanel.jsx
Right side: peer list with status, ping, connect/disconnect
AgentPanel.jsx
Bottom-left: agent list, task submission form
ContentPanel.jsx
Bottom-left stack: pinned CIDs, upload
EventLog.jsx
Bottom-left stack: real-time WebSocket event stream
StatusBar.jsx
Always-visible bottom bar: aggregate counts
DetailPanel.jsx
Slides in on peer selection with deep info
State Management: useNodeState hook drives all REST polling; useWebSocket drives live updates.

Module 8 — crypto-layer (Application Cryptography)
Status: ✅ Stable core | 🔲 ZK Proofs not implemented

The security layer — audit outcome: CONDITIONAL PASS:

File Primitive Role
signing.py
Ed25519 Node identity signing. Canonical digest:
sha256(VERSION_byte + timestamp + sender_id + payload)
before signing — prevents length-extension attacks
exchange.py
X25519 ECDH Session + envelope key derivation. Ephemeral keys signed by Ed25519 identity key (MITM prevention)
envelope.py
AES-256-GCM SealedEnvelope — ephemeral ECDH + HKDF + AES-GCM + Ed25519 sig. Achieves: confidentiality, authenticity, PFS, replay resistance, header binding
keystore.py
PBKDF2 + Fernet Encrypted key persistence + rotation. Keys stored in ~/.neuralis/crypto/keys.json
tokens.py
HMAC-SHA256
CapabilityToken
— JWT-like offline-verifiable auth tokens. Wire format:
b64url(header).b64url(payload).b64url(hmac)
. Capability matching supports hierarchical wildcards (agent:invoke:\*)
Known Audit Findings:

F-01 (Low): Default envelope TTL 300s. Recommend nonce bloomfilter for sub-TTL replay detection.
F-02 (Medium): Signer.from_node() accesses identity.\_private_key directly — should use a controlled
sign()
method.
F-03 (Low): HMAC key rotation immediately invalidates all existing tokens — no grace period.
F-04 (High): ZK proofs are completely absent despite being a key roadmap feature.
Cryptographic Stack Summary
Identity: Ed25519 (node ID, message signing, handshake auth)
Key Exchange: X25519 (ephemeral ECDH for sessions/envelopes)
KDF: HKDF-SHA256 (derives AES keys from ECDH output, domain-separated)
Encryption: AES-256-GCM (AEAD, all payload encryption)
Auth Tokens: HMAC-SHA256 (CapabilityTokens — offline JWT-like)
Key Storage: PBKDF2 + Fernet (at-rest key protection)
Planned: Groth16 SNARK / Merkle-path ZKP / Bulletproofs
Tokenomics: Dual-Token DePIN Economy
$NEUR — Protocol Token (liquid, tradable)
Stake to register as a node, burn to get credits, vote on governance
Fixed epoch emissions (decreasing schedule)
Supply contracts as demand-driven burns increase
$NRU — Neural Resource Unit (non-tradable internal credit)
Minted by burning $NEUR (initial rate: 1 NEUR → 100 NRU)
Consumed per inference token, per MB stored, per GB transferred
Rate auto-adjusts via DAO-governed utilization ratio
Burn-and-Mint Equilibrium (BME): As AI service demand rises, more $NEUR burns → supply contracts → price pressure rewards long-term node operators.

Current State: Credit deduction and reward distribution logic is not yet written into the runtime. The hooks (tokens, task routing, resource monitoring) all exist — but the economic settlement layer is planned for Phase 3+.

Current Build Status & Roadmap
Phase Status Details
Modules 1–4 (Core P2P + Storage + Agent Runtime) ✅ Complete Production-grade code
Module 5–6 (Protocol + Canvas API) ✅ Complete Full REST surface
Module 7 (Canvas UI) 🎨 In Progress React dashboard, D3 canvas
Module 8 (Crypto Layer) 🐛 Integration/Bug-fixing Core crypto stable; ZK proofs absent; integration with runtime pending
Phase 2 (100k Node Mesh Testing) 🔲 Planned Needs GossipSub + NAT traversal
Phase 3 (NRU Credit Ledger) 🔲 Planned Needs Module 8 ZK proofs
Phase 4 (On-chain Settlement) 🔲 Planned Solana/EVM bridge
Phase 5 (DAO Governance) 🔲 Planned
100M Node Target 🔲 Long-term Erasure coding, Reed-Solomon
How to Run (Dev)
bash

# From project root — boots node + API on http://127.0.0.1:7100

./dev-start.sh [alias]

# Then start the UI separately

cd canvas-ui && npm run dev
The
dev-start.sh
script builds a PYTHONPATH from all 7 Python sub-packages and runs a single inline Python script that wires: Node.boot() →
AgentRuntime
→ FastAPI app → uvicorn.

Key Architectural Patterns
Dependency Injection Root: The
Node
object is the single source of truth — every subsystem receives it and accesses config/identity through it.
LIFO Shutdown: Subsystems register shutdown callbacks via node.on_shutdown(), called in reverse order.
Plugin Architecture: Agents are auto-discovered Python plugins dropped in ~/.neuralis/agents/.
Domain-Separated Crypto: HKDF uses different info strings per protocol context to prevent key material reuse across layers.
Defense in Depth: Dual protection on envelopes (AES-GCM tag + Ed25519 sig); X25519 keys signed by Ed25519 identity keys to prevent MITM.
