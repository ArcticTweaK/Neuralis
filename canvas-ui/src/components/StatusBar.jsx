/**
 * components/StatusBar.jsx
 * ========================
 * Thin bottom status bar showing mesh-wide stats and zero-telemetry notice.
 */

import { formatUptime } from '../lib/utils'

export default function StatusBar({ node, peers, agents, content, wsStatus }) {
    const peerList = Object.values(peers ?? {})
    const connectedN = peerList.filter(p => ['CONNECTED', 'VERIFIED'].includes(p.status)).length
    const agentList = Object.values(agents ?? {})
    const runningN = agentList.filter(a => a.state === 'RUNNING' || a.state === 'IDLE').length

    const wsColor = {
        connected: 'text-accent',
        connecting: 'text-warn',
        disconnected: 'text-red-400',
    }[wsStatus] ?? 'text-muted'

    return (
        <div className="h-7 border-t border-border bg-panel flex items-center px-4 gap-6 text-xs font-mono shrink-0">
            {/* Left: node identity shorthand */}
            <span className="text-text-dim">
                NEURALIS <span className="text-accent">{node?.alias ?? '—'}</span>
            </span>

            <span className="text-border">│</span>

            {/* Peers */}
            <span className="text-text-dim">
                PEERS <span className="text-text">{connectedN}</span>
                <span className="text-border">/{peerList.length}</span>
            </span>

            <span className="text-border">│</span>

            {/* Agents */}
            <span className="text-text-dim">
                AGENTS <span className="text-text">{runningN}</span>
                <span className="text-border">/{agentList.length}</span>
            </span>

            <span className="text-border">│</span>

            {/* Content */}
            <span className="text-text-dim">
                PINS <span className="text-text">{content?.length ?? 0}</span>
            </span>

            <span className="text-border">│</span>

            {/* Uptime */}
            <span className="text-text-dim">
                UP <span className="text-text">{formatUptime(node?.uptime_seconds)}</span>
            </span>

            {/* Right: WS + telemetry notice */}
            <div className="ml-auto flex items-center gap-4">
                <span className="text-text-dim opacity-40">TELEMETRY: OFF</span>
                <span className={wsColor}>
                    {wsStatus === 'connected' ? '● WS' : wsStatus === 'connecting' ? '◌ WS' : '○ WS'}
                </span>
            </div>
        </div>
    )
}
