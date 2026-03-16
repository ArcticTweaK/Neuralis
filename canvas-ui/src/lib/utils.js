/**
 * lib/utils.js
 * ============
 * Shared utility helpers used across canvas components.
 */

/**
 * Truncate a node/peer ID for display.
 * "NRL1abc123def456..." → "NRL1abc1…def456"
 */
export function shortId(id = '', head = 8, tail = 6) {
    if (!id || id.length <= head + tail + 1) return id
    return `${id.slice(0, head)}…${id.slice(-tail)}`
}

/**
 * Format uptime seconds into human-readable string.
 * 3661 → "1h 01m 01s"
 */
export function formatUptime(seconds) {
    if (!seconds || seconds < 0) return '0s'
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = Math.floor(seconds % 60)
    if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`
    if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`
    return `${s}s`
}

/**
 * Format bytes into human-readable size.
 */
export function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`
}

/**
 * Map peer status to color class.
 */
export function peerStatusColor(status) {
    switch (status?.toUpperCase()) {
        case 'VERIFIED': return 'text-accent'
        case 'CONNECTED': return 'text-accent'
        case 'CONNECTING': return 'text-info'
        case 'HANDSHAKING': return 'text-info'
        case 'DISCOVERED': return 'text-text-dim'
        case 'DEGRADED': return 'text-warn'
        case 'DISCONNECTED': return 'text-muted'
        case 'BANNED': return 'text-red-500'
        default: return 'text-text-dim'
    }
}

/**
 * Map agent state to color class.
 */
export function agentStateColor(state) {
    switch (state?.toUpperCase()) {
        case 'RUNNING': return 'text-accent'
        case 'IDLE': return 'text-info'
        case 'LOADING': return 'text-warn'
        case 'ERROR': return 'text-red-400'
        case 'STOPPED': return 'text-muted'
        default: return 'text-text-dim'
    }
}

/**
 * Map node state to color class.
 */
export function nodeStateColor(state) {
    switch (state?.toUpperCase()) {
        case 'RUNNING': return 'text-accent'
        case 'BOOTING': return 'text-warn'
        case 'SHUTTING_DOWN': return 'text-warn'
        case 'ERROR': return 'text-red-400'
        case 'STOPPED': return 'text-muted'
        default: return 'text-text-dim'
    }
}

/**
 * Generate a stable pseudo-random position for a node on the canvas
 * based on its ID string. Returns { x, y } in [0..1] space.
 */
export function stablePosition(id = '') {
    let h1 = 0, h2 = 0
    for (let i = 0; i < id.length; i++) {
        const c = id.charCodeAt(i)
        h1 = ((h1 << 5) - h1 + c) | 0
        h2 = ((h2 << 7) - h2 + c * 31) | 0
    }
    const x = (Math.abs(h1) % 10000) / 10000
    const y = (Math.abs(h2) % 10000) / 10000
    return { x, y }
}

/**
 * Clamp value between min and max.
 */
export function clamp(val, min, max) {
    return Math.min(Math.max(val, min), max)
}

/**
 * Format a unix timestamp as relative time.
 * e.g. "3s ago", "2m ago", "1h ago"
 */
export function relativeTime(ts) {
    if (!ts) return 'never'
    const diff = (Date.now() / 1000) - ts
    if (diff < 60) return `${Math.floor(diff)}s ago`
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
    return `${Math.floor(diff / 3600)}h ago`
}
