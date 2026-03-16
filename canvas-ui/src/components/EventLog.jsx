/**
 * components/EventLog.jsx
 * =======================
 * Live event log driven by WebSocket events. Shows the last 50 mesh events
 * in a scrollable terminal-style feed. Auto-scrolls to bottom on new events.
 */

import { useEffect, useRef, useState } from 'react'
import { shortId } from '../lib/utils'

const EVENT_CONFIG = {
    peer_connected: { label: 'PEER+', color: 'text-accent' },
    peer_disconnected: { label: 'PEER-', color: 'text-warn' },
    content_announced: { label: 'CID+', color: 'text-info' },
    remote_agent_announce: { label: 'AGENT', color: 'text-peer' },
    node_status: { label: 'STATUS', color: 'text-text-dim' },
}

function formatEventData(type, data) {
    if (!data) return ''
    switch (type) {
        case 'peer_connected':
        case 'peer_disconnected':
            return `${data.alias || shortId(data.node_id, 6, 4)} [${data.status ?? 'disconnected'}]`
        case 'content_announced':
            return `${data.cid?.slice(0, 16)}… (${data.size ?? '?'}B)`
        case 'remote_agent_announce':
            return `node ${shortId(data.node_id, 6, 4)} → ${data.agents?.length ?? 0} agents`
        default:
            return JSON.stringify(data).slice(0, 60)
    }
}

export default function EventLog({ events }) {
    const [expanded, setExpanded] = useState(false)
    const bottomRef = useRef(null)

    useEffect(() => {
        if (expanded) {
            bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
        }
    }, [events, expanded])

    return (
        <div className="panel rounded-md fade-in-up" style={{ backdropFilter: 'blur(8px)' }}>
            <button
                className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-white hover:bg-opacity-5 transition-colors rounded-t-md"
                onClick={() => setExpanded(e => !e)}
            >
                <div className="flex items-center gap-2">
                    <span className="text-text-dim text-xs tracking-widest uppercase">Event Log</span>
                    {events.length > 0 && (
                        <span className="px-1 py-0.5 bg-accent-glow text-accent text-xs rounded">
                            {events.length}
                        </span>
                    )}
                </div>
                <span className="text-text-dim text-xs">{expanded ? '▲' : '▼'}</span>
            </button>

            {expanded && (
                <div className="border-t border-border max-h-40 overflow-y-auto">
                    {events.length === 0 ? (
                        <div className="px-4 py-3 text-text-dim text-xs text-center">
                            Waiting for events…
                            <span className="cursor-blink" />
                        </div>
                    ) : (
                        <div className="py-1">
                            {[...events].reverse().map((evt, i) => {
                                const cfg = EVENT_CONFIG[evt.type] ?? { label: evt.type?.toUpperCase().slice(0, 6) ?? '???', color: 'text-text-dim' }
                                return (
                                    <div key={i} className="px-4 py-1 flex items-baseline gap-2 hover:bg-white hover:bg-opacity-[0.02] transition-colors">
                                        <span className="text-text-dim text-xs shrink-0 opacity-50">
                                            {new Date(evt.ts).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                                        </span>
                                        <span className={`text-xs font-mono shrink-0 w-12 ${cfg.color}`}>{cfg.label}</span>
                                        <span className="text-text-dim text-xs truncate">
                                            {formatEventData(evt.type, evt.data)}
                                        </span>
                                    </div>
                                )
                            })}
                            <div ref={bottomRef} />
                        </div>
                    )}
                </div>
            )}
        </div>
    )
}
