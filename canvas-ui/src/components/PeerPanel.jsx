/**
 * components/PeerPanel.jsx
 * ========================
 * Right-side panel listing all known peers with status, ping, and connect/disconnect controls.
 */

import { useState } from 'react'
import { shortId, peerStatusColor, relativeTime } from '../lib/utils'

const STATUS_DOT = {
    VERIFIED: '●',
    CONNECTED: '●',
    CONNECTING: '◌',
    HANDSHAKING: '◌',
    DISCOVERED: '○',
    DEGRADED: '◈',
    DISCONNECTED: '○',
    BANNED: '✕',
}

export default function PeerPanel({ peers, onConnect, onDisconnect, onSelectPeer, selectedId }) {
    const [showAdd, setShowAdd] = useState(false)
    const [address, setAddress] = useState('')
    const [connecting, setConn] = useState(false)
    const [error, setError] = useState(null)

    const peerList = Object.values(peers || {})
        .sort((a, b) => {
            const rank = { VERIFIED: 0, CONNECTED: 0, HANDSHAKING: 1, CONNECTING: 2, DISCOVERED: 3, DEGRADED: 4, DISCONNECTED: 5, BANNED: 6 }
            return (rank[a.status] ?? 7) - (rank[b.status] ?? 7)
        })

    async function handleConnect() {
        if (!address.trim()) return
        setConn(true)
        setError(null)
        try {
            await onConnect(address.trim())
            setAddress('')
            setShowAdd(false)
        } catch (err) {
            setError(err.message)
        } finally {
            setConn(false)
        }
    }

    return (
        <div className="panel rounded-md flex flex-col min-w-[240px] max-w-[280px] max-h-[70vh] fade-in-up" style={{ backdropFilter: 'blur(8px)' }}>
            {/* Header */}
            <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-border shrink-0">
                <span className="text-text-dim text-xs tracking-widest uppercase">Peers</span>
                <div className="flex items-center gap-2">
                    <span className="text-text-dim text-xs">{peerList.length}</span>
                    <button
                        onClick={() => { setShowAdd(s => !s); setError(null) }}
                        className="text-accent text-xs px-1.5 py-0.5 border border-accent border-opacity-40 rounded hover:bg-accent-glow transition-colors"
                    >
                        {showAdd ? '✕' : '+ ADD'}
                    </button>
                </div>
            </div>

            {/* Add peer form */}
            {showAdd && (
                <div className="px-4 py-3 border-b border-border shrink-0 fade-in-up">
                    <div className="text-text-dim text-xs mb-1.5">MULTIADDR OR host:port</div>
                    <input
                        value={address}
                        onChange={e => setAddress(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && handleConnect()}
                        placeholder="/ip4/192.168.1.5/tcp/7101"
                        className="w-full bg-grid border border-border text-text text-xs font-mono px-2 py-1.5 rounded outline-none focus:border-accent transition-colors mb-2"
                    />
                    {error && <div className="text-red-400 text-xs mb-2">{error}</div>}
                    <button
                        onClick={handleConnect}
                        disabled={connecting || !address.trim()}
                        className="w-full text-xs py-1.5 bg-accent-glow border border-accent border-opacity-40 text-accent rounded hover:bg-opacity-100 disabled:opacity-40 transition-colors"
                    >
                        {connecting ? 'CONNECTING…' : 'CONNECT'}
                    </button>
                </div>
            )}

            {/* Peer list */}
            <div className="overflow-y-auto flex-1">
                {peerList.length === 0 ? (
                    <div className="px-4 py-6 text-center text-text-dim text-xs">
                        No peers discovered yet.<br />
                        <span className="text-text-dim opacity-60">mDNS scanning…</span>
                    </div>
                ) : (
                    <ul className="divide-y divide-border">
                        {peerList.map(peer => (
                            <PeerRow
                                key={peer.node_id}
                                peer={peer}
                                selected={selectedId === peer.node_id}
                                onSelect={() => onSelectPeer?.(peer)}
                                onDisconnect={() => onDisconnect?.(peer.node_id)}
                            />
                        ))}
                    </ul>
                )}
            </div>
        </div>
    )
}

function PeerRow({ peer, selected, onSelect, onDisconnect }) {
    const [hovered, setHovered] = useState(false)
    const dotColor = {
        VERIFIED: 'text-accent',
        CONNECTED: 'text-accent',
        CONNECTING: 'text-info',
        HANDSHAKING: 'text-info',
        DEGRADED: 'text-warn',
        DISCONNECTED: 'text-muted',
        BANNED: 'text-red-400',
        DISCOVERED: 'text-text-dim',
    }[peer.status] ?? 'text-text-dim'

    return (
        <li
            className={`px-4 py-2.5 cursor-pointer transition-colors ${selected ? 'bg-accent-glow' : hovered ? 'bg-white bg-opacity-[0.03]' : ''}`}
            onClick={onSelect}
            onMouseEnter={() => setHovered(true)}
            onMouseLeave={() => setHovered(false)}
        >
            <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-1.5 min-w-0">
                    <span className={`${dotColor} shrink-0 text-xs`}>{STATUS_DOT[peer.status] ?? '○'}</span>
                    <span className="text-text font-mono text-xs truncate">
                        {peer.alias || shortId(peer.node_id, 6, 4)}
                    </span>
                </div>
                {peer.last_ping_ms != null && (
                    <span className={`text-xs shrink-0 ${peer.last_ping_ms < 50 ? 'text-accent' : peer.last_ping_ms < 200 ? 'text-info' : 'text-warn'}`}>
                        {peer.last_ping_ms}ms
                    </span>
                )}
            </div>
            <div className="flex items-center justify-between mt-1">
                <span className={`text-xs ${peerStatusColor(peer.status)}`}>
                    {peer.status?.toLowerCase()}
                </span>
                {(peer.status === 'CONNECTED' || peer.status === 'VERIFIED') && hovered && (
                    <button
                        onClick={e => { e.stopPropagation(); onDisconnect() }}
                        className="text-xs text-red-400 hover:text-red-300 opacity-70 hover:opacity-100 transition-opacity"
                    >
                        disconnect
                    </button>
                )}
            </div>
        </li>
    )
}
