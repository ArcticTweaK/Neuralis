/**
 * hooks/useWebSocket.js
 * =====================
 * Connects to ws://localhost:7100/ws and dispatches incoming Canvas events
 * into the shared application state.
 *
 * Events handled:
 *   node_status          → full node snapshot on connect
 *   peer_connected       → add/update peer
 *   peer_disconnected    → mark peer disconnected
 *   content_announced    → add CID to content list
 *   remote_agent_announce → update remote node's agent list
 *
 * Reconnects with exponential backoff (max 30s) on drop.
 */

import { useEffect, useRef, useCallback } from 'react'

const WS_URL = `ws://${location.host}/ws` // proxied by Vite to ws://localhost:7100
const BACKOFF_BASE_MS = 1000
const BACKOFF_MAX_MS = 30000

export function useWebSocket({ onEvent, onStatus }) {
    const wsRef = useRef(null)
    const attemptsRef = useRef(0)
    const timersRef = useRef([])
    const mountedRef = useRef(true)

    const clearTimers = useCallback(() => {
        timersRef.current.forEach(clearTimeout)
        timersRef.current = []
    }, [])

    const connect = useCallback(() => {
        if (!mountedRef.current) return
        onStatus?.('connecting')

        const ws = new WebSocket(WS_URL)
        wsRef.current = ws

        ws.onopen = () => {
            if (!mountedRef.current) { ws.close(); return }
            attemptsRef.current = 0
            onStatus?.('connected')
        }

        ws.onmessage = (evt) => {
            if (!mountedRef.current) return
            try {
                const msg = JSON.parse(evt.data)
                onEvent?.(msg)
            } catch (err) {
                console.warn('[WS] failed to parse message', err)
            }
        }

        ws.onerror = () => {
            // onclose fires right after, so no need to handle here
        }

        ws.onclose = () => {
            if (!mountedRef.current) return
            onStatus?.('disconnected')

            // Exponential backoff
            const delay = Math.min(
                BACKOFF_BASE_MS * Math.pow(1.8, attemptsRef.current),
                BACKOFF_MAX_MS
            )
            attemptsRef.current++

            const t = setTimeout(connect, delay)
            timersRef.current.push(t)
        }
    }, [onEvent, onStatus])

    useEffect(() => {
        mountedRef.current = true
        connect()
        return () => {
            mountedRef.current = false
            clearTimers()
            wsRef.current?.close()
        }
    }, [connect, clearTimers])

    const send = useCallback((data) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify(data))
        }
    }, [])

    return { send }
}
