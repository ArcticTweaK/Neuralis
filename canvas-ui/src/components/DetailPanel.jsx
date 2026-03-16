/**
 * components/DetailPanel.jsx
 * ==========================
 * Slides in from the right when a peer node is selected on the canvas.
 * Shows full peer info, addresses, and remote agents if available.
 */

import { shortId, peerStatusColor, relativeTime } from '../lib/utils'

export default function DetailPanel({ node, remotes, onClose }) {
    if (!node) return null

    const remote = remotes?.[node.id]

    return (
        <div className="panel rounded-md w-[260px] fade-in-up" style={{ backdropFilter: 'blur(8px)' }}>
            {/* Header */}
            <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-border">
                <span className="text-text-dim text-xs tracking-widest uppercase">Peer Detail</span>
                <button
                    onClick={onClose}
                    className="text-text-dim hover:text-text transition-colors text-sm"
                >
                    ✕
                </button>
            </div>

            <div className="px-4 py-3 space-y-3">
                {/* Alias / ID */}
                <div>
                    <div className="text-peer font-display text-sm glow-peer">
                        {node.label || shortId(node.id, 8, 6)}
                    </div>
                    <div className="text-text-dim font-mono text-xs mt-1 break-all">
                        {node.id}
                    </div>
                </div>

                {/* Status / ping */}
                <div className="flex items-center justify-between">
                    <span className={`text-xs ${peerStatusColor(node.status)}`}>
                        {node.status?.toLowerCase() ?? 'unknown'}
                    </span>
                    {node.ping != null && (
                        <span className={`text-xs ${node.ping < 50 ? 'text-accent' : node.ping < 200 ? 'text-info' : 'text-warn'}`}>
                            {node.ping}ms RTT
                        </span>
                    )}
                </div>

                {/* Addresses */}
                {node.addresses?.length > 0 && (
                    <div>
                        <div className="text-text-dim text-xs mb-1 tracking-wider">ADDRESSES</div>
                        <div className="space-y-0.5">
                            {node.addresses.map(addr => (
                                <div key={addr} className="text-xs font-mono text-text-dim truncate" title={addr}>
                                    {addr}
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* Remote agents */}
                {remote?.agents?.length > 0 && (
                    <div>
                        <div className="text-text-dim text-xs mb-1.5 tracking-wider">REMOTE AGENTS</div>
                        <div className="space-y-1">
                            {remote.agents.map(agent => (
                                <div key={agent.name ?? agent} className="flex items-center gap-1.5">
                                    <span className="text-peer text-xs">◉</span>
                                    <span className="text-text text-xs font-mono">{agent.name ?? agent}</span>
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* Last seen */}
                {remote?.last_seen && (
                    <div className="text-text-dim text-xs">
                        Last seen: {relativeTime(remote.last_seen)}
                    </div>
                )}
            </div>
        </div>
    )
}
