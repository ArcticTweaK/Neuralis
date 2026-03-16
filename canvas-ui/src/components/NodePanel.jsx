/**
 * components/NodePanel.jsx
 * ========================
 * Top-left HUD panel showing this node's identity and live status.
 * Alias is editable inline.
 */

import { useState, useRef } from 'react'
import { shortId, formatUptime, nodeStateColor } from '../lib/utils'

export default function NodePanel({ node, wsStatus, onUpdateAlias }) {
    const [editing, setEditing] = useState(false)
    const [aliasInput, setAliasInput] = useState('')
    const inputRef = useRef(null)

    function startEdit() {
        setAliasInput(node?.alias ?? '')
        setEditing(true)
        setTimeout(() => inputRef.current?.focus(), 50)
    }

    async function commitEdit() {
        const val = aliasInput.trim()
        if (val && val !== node?.alias) {
            try { await onUpdateAlias(val) } catch { /* ignore */ }
        }
        setEditing(false)
    }

    function handleKeyDown(e) {
        if (e.key === 'Enter') commitEdit()
        if (e.key === 'Escape') setEditing(false)
    }

    const wsColor = {
        connected: 'text-accent',
        connecting: 'text-warn',
        disconnected: 'text-red-400',
    }[wsStatus] ?? 'text-muted'

    const wsLabel = {
        connected: '● LIVE',
        connecting: '○ CONNECTING',
        disconnected: '○ OFFLINE',
    }[wsStatus] ?? '○ --'

    return (
        <div className="panel rounded-md p-4 min-w-[260px] fade-in-up" style={{ backdropFilter: 'blur(8px)' }}>
            {/* Header */}
            <div className="flex items-center justify-between mb-3">
                <span className="text-text-dim text-xs tracking-widest uppercase">This Node</span>
                <span className={`text-xs font-mono ${wsColor}`}>{wsLabel}</span>
            </div>

            {/* Alias */}
            <div className="mb-3">
                {editing ? (
                    <input
                        ref={inputRef}
                        value={aliasInput}
                        onChange={e => setAliasInput(e.target.value)}
                        onBlur={commitEdit}
                        onKeyDown={handleKeyDown}
                        className="bg-transparent border-b border-accent text-accent text-base font-display outline-none w-full"
                        maxLength={32}
                    />
                ) : (
                    <div
                        className="text-accent text-base font-display glow-accent cursor-pointer hover:text-accent-dim transition-colors"
                        onClick={startEdit}
                        title="Click to edit alias"
                    >
                        {node?.alias || 'unnamed-node'}
                        <span className="text-text-dim text-xs ml-2 opacity-50">[edit]</span>
                    </div>
                )}
            </div>

            {/* IDs */}
            <div className="space-y-1.5 text-xs">
                <Row label="NODE ID" value={shortId(node?.node_id, 8, 6)} mono dimVal />
                <Row label="PEER ID" value={shortId(node?.peer_id, 8, 6)} mono dimVal />
                <Row
                    label="STATE"
                    value={node?.state ?? '—'}
                    valueClass={nodeStateColor(node?.state)}
                />
                <Row label="UPTIME" value={formatUptime(node?.uptime_seconds)} />
            </div>

            {/* Subsystems */}
            {node?.subsystems?.length > 0 && (
                <div className="mt-3 pt-3 border-t border-border">
                    <div className="text-text-dim text-xs mb-1.5 tracking-wider">SUBSYSTEMS</div>
                    <div className="flex flex-wrap gap-1">
                        {node.subsystems.map(s => (
                            <span key={s} className="px-1.5 py-0.5 bg-accent-glow border border-accent border-opacity-30 text-accent text-xs rounded">
                                {s}
                            </span>
                        ))}
                    </div>
                </div>
            )}

            {/* Listen addresses */}
            {node?.listen_addresses?.length > 0 && (
                <div className="mt-3 pt-3 border-t border-border">
                    <div className="text-text-dim text-xs mb-1.5 tracking-wider">LISTENING ON</div>
                    <div className="space-y-0.5">
                        {node.listen_addresses.map(addr => (
                            <div key={addr} className="text-xs text-text-dim font-mono truncate" title={addr}>
                                {addr}
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    )
}

function Row({ label, value, valueClass = 'text-text', mono = false, dimVal = false }) {
    return (
        <div className="flex justify-between items-center gap-2">
            <span className="text-text-dim tracking-wider shrink-0">{label}</span>
            <span className={`${dimVal ? 'text-text-dim' : valueClass} ${mono ? 'font-mono' : ''} truncate text-right`}>
                {value ?? '—'}
            </span>
        </div>
    )
}
