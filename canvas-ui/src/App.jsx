/**
 * App.jsx
 * =======
 * Root component. Wires together:
 *   - useNodeState (central state + REST fetches)
 *   - useWebSocket (live event stream)
 *   - Canvas (D3 force graph)
 *   - All HUD panels (NodePanel, PeerPanel, AgentPanel, ContentPanel, EventLog)
 *   - StatusBar
 *   - LoadingScreen
 */

import { useState, useCallback } from 'react'
import { useNodeState } from './hooks/useNodeState'
import { useWebSocket } from './hooks/useWebSocket'
import Canvas from './components/Canvas'
import NodePanel from './components/NodePanel'
import PeerPanel from './components/PeerPanel'
import AgentPanel from './components/AgentPanel'
import ContentPanel from './components/ContentPanel'
import EventLog from './components/EventLog'
import DetailPanel from './components/DetailPanel'
import StatusBar from './components/StatusBar'
import LoadingScreen from './components/LoadingScreen'

export default function App() {
    const { state, actions, handleWsEvent, handleWsStatus } = useNodeState()
    const [selectedNode, setSelectedNode] = useState(null)

    // Wire WebSocket
    useWebSocket({
        onEvent: handleWsEvent,
        onStatus: handleWsStatus,
    })

    const handleSelectNode = useCallback((nodeData) => {
        setSelectedNode(nodeData)
    }, [])

    // Show loading / error splash
    if (state.loading) return <LoadingScreen error={null} />
    if (state.error) return <LoadingScreen error={state.error} />

    return (
        <div className="w-full h-full flex flex-col bg-void scanlines overflow-hidden">
            {/* Main area: canvas + overlay HUD panels */}
            <div className="flex-1 relative overflow-hidden">

                {/* ── Canvas (full bleed) ───────────────────────────────────────────── */}
                <Canvas
                    node={state.node}
                    peers={state.peers}
                    agents={state.agents}
                    selectedId={selectedNode?.id}
                    onSelectNode={handleSelectNode}
                />

                {/* ── Top-left: Node panel ─────────────────────────────────────────── */}
                <div className="absolute top-4 left-4 z-10 space-y-3">
                    <NodePanel
                        node={state.node}
                        wsStatus={state.wsStatus}
                        onUpdateAlias={actions.updateAlias}
                    />
                </div>

                {/* ── Top-right: Header / branding ─────────────────────────────────── */}
                <div className="absolute top-4 right-4 z-10 text-right pointer-events-none">
                    <div className="font-display text-accent text-lg glow-accent tracking-widest">NEURALIS</div>
                    <div className="text-text-dim text-xs tracking-widest opacity-50">AI MESH CANVAS</div>
                </div>

                {/* ── Right side: Peer list ─────────────────────────────────────────── */}
                <div className="absolute top-16 right-4 z-10">
                    <PeerPanel
                        peers={state.peers}
                        selectedId={selectedNode?.id}
                        onConnect={actions.connectPeer}
                        onDisconnect={actions.disconnectPeer}
                        onSelectPeer={(peer) => handleSelectNode({
                            id: peer.node_id,
                            label: peer.alias,
                            type: 'peer',
                            status: peer.status,
                            ping: peer.last_ping_ms,
                            addresses: peer.addresses,
                        })}
                    />
                </div>

                {/* ── Detail panel (slides in when peer selected) ───────────────────── */}
                {selectedNode && selectedNode.type === 'peer' && (
                    <div className="absolute top-1/2 -translate-y-1/2 right-[300px] z-10">
                        <DetailPanel
                            node={selectedNode}
                            remotes={state.remotes}
                            onClose={() => setSelectedNode(null)}
                        />
                    </div>
                )}

                {/* ── Bottom-left: Agents + Content + EventLog stack ───────────────── */}
                <div className="absolute bottom-4 left-4 z-10 space-y-2 w-[280px]">
                    <EventLog events={state.events} />
                    <ContentPanel
                        content={state.content}
                        onUnpin={actions.unpinContent}
                    />
                    <AgentPanel
                        agents={state.agents}
                        onSubmitTask={actions.submitTask}
                    />
                </div>

                {/* ── Peer count badge (canvas center top) ─────────────────────────── */}
                <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10 pointer-events-none">
                    <div className="flex items-center gap-3 text-xs text-text-dim font-mono">
                        <span>
                            <span className="text-accent">{Object.values(state.peers).filter(p => ['CONNECTED', 'VERIFIED'].includes(p.status)).length}</span>
                            {' '}peers connected
                        </span>
                        <span className="text-border">│</span>
                        <span>
                            <span className="text-info">{Object.values(state.agents).length}</span>
                            {' '}agents
                        </span>
                    </div>
                </div>

            </div>

            {/* ── Status bar (always visible at bottom) ─────────────────────────── */}
            <StatusBar
                node={state.node}
                peers={state.peers}
                agents={state.agents}
                content={state.content}
                wsStatus={state.wsStatus}
            />
        </div>
    )
}
