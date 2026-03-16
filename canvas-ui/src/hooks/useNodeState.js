/**
 * hooks/useNodeState.js
 * =====================
 * Central state for the entire canvas. Combines:
 *   - Initial REST fetches (node status, peers, agents, content)
 *   - Live WebSocket event mutations
 *
 * Returns a single `state` object and `actions` for UI-triggered mutations.
 */

import { useReducer, useCallback, useEffect, useRef } from 'react'
import * as api from '../lib/api'

// ─── Initial State ───────────────────────────────────────────────────────────

const INITIAL = {
    node: null,    // NodeStatusResponse
    peers: {},      // { [node_id]: PeerResponse }
    agents: {},      // { [name]: AgentResponse }
    content: [],      // ContentAddResponse[]
    remotes: {},      // { [node_id]: RemoteNodeResponse }
    wsStatus: 'connecting', // 'connecting' | 'connected' | 'disconnected'
    loading: true,
    error: null,
    events: [],      // last 50 events for the event log
}

// ─── Reducer ─────────────────────────────────────────────────────────────────

function reducer(state, action) {
    switch (action.type) {

        case 'LOADED': return {
            ...state,
            loading: false,
            node: action.node,
            peers: Object.fromEntries((action.peers || []).map(p => [p.node_id, p])),
            agents: Object.fromEntries((action.agents || []).map(a => [a.name, a])),
            content: action.content || [],
        }

        case 'LOAD_ERROR': return {
            ...state,
            loading: false,
            error: action.error,
        }

        case 'WS_STATUS': return {
            ...state,
            wsStatus: action.status,
        }

        // Full node snapshot (sent on WS connect)
        case 'WS_NODE_STATUS': return {
            ...state,
            node: { ...state.node, ...action.data },
        }

        case 'WS_PEER_CONNECTED': {
            const p = action.data
            return {
                ...state,
                peers: { ...state.peers, [p.node_id]: p },
                events: _pushEvent(state.events, { type: 'peer_connected', data: p }),
            }
        }

        case 'WS_PEER_DISCONNECTED': {
            const { node_id } = action.data
            const existing = state.peers[node_id]
            if (!existing) return state
            return {
                ...state,
                peers: {
                    ...state.peers,
                    [node_id]: { ...existing, status: 'DISCONNECTED' },
                },
                events: _pushEvent(state.events, { type: 'peer_disconnected', data: action.data }),
            }
        }

        case 'WS_CONTENT_ANNOUNCED': {
            const cid = action.data
            const exists = state.content.find(c => c.cid === cid.cid)
            return {
                ...state,
                content: exists ? state.content : [cid, ...state.content],
                events: _pushEvent(state.events, { type: 'content_announced', data: cid }),
            }
        }

        case 'WS_REMOTE_AGENT': {
            const remote = action.data
            return {
                ...state,
                remotes: {
                    ...state.remotes,
                    [remote.node_id]: remote,
                },
            }
        }

        // Local optimistic updates from UI actions
        case 'NODE_ALIAS_UPDATED': return {
            ...state,
            node: state.node ? { ...state.node, alias: action.alias } : state.node,
        }

        case 'PEER_REMOVED': {
            const next = { ...state.peers }
            delete next[action.node_id]
            return { ...state, peers: next }
        }

        case 'CONTENT_REMOVED': return {
            ...state,
            content: state.content.filter(c => c.cid !== action.cid),
        }

        case 'CONTENT_ADDED': return {
            ...state,
            content: [action.item, ...state.content],
        }

        default: return state
    }
}

function _pushEvent(events, evt) {
    return [{ ...evt, ts: Date.now() }, ...events].slice(0, 50)
}

// ─── Hook ────────────────────────────────────────────────────────────────────

export function useNodeState() {
    const [state, dispatch] = useReducer(reducer, INITIAL)
    const mountedRef = useRef(true)

    // Initial data load
    useEffect(() => {
        mountedRef.current = true
        loadAll()
        // Poll node status every 10s (uptime counter etc)
        const interval = setInterval(() => refreshNode(), 10000)
        return () => {
            mountedRef.current = false
            clearInterval(interval)
        }
    }, [])

    async function loadAll() {
        try {
            const [node, peers, agents, content] = await Promise.all([
                api.getNodeStatus().catch(() => null),
                api.getPeers().catch(() => []),
                api.getAgents().catch(() => []),
                api.getContent().catch(() => []),
            ])
            if (!mountedRef.current) return
            dispatch({ type: 'LOADED', node, peers, agents, content })
        } catch (err) {
            if (!mountedRef.current) return
            dispatch({ type: 'LOAD_ERROR', error: err.message })
        }
    }

    async function refreshNode() {
        try {
            const node = await api.getNodeStatus()
            if (mountedRef.current) dispatch({ type: 'WS_NODE_STATUS', data: node })
        } catch { /* silent */ }
    }

    // WebSocket event handler — called by useWebSocket hook
    const handleWsEvent = useCallback((msg) => {
        const { event, data } = msg
        switch (event) {
            case 'node_status': dispatch({ type: 'WS_NODE_STATUS', data }); break
            case 'peer_connected': dispatch({ type: 'WS_PEER_CONNECTED', data }); break
            case 'peer_disconnected': dispatch({ type: 'WS_PEER_DISCONNECTED', data }); break
            case 'content_announced': dispatch({ type: 'WS_CONTENT_ANNOUNCED', data }); break
            case 'remote_agent_announce': dispatch({ type: 'WS_REMOTE_AGENT', data }); break
            default: break
        }
    }, [])

    const handleWsStatus = useCallback((status) => {
        dispatch({ type: 'WS_STATUS', status })
    }, [])

    // ─── Actions ───────────────────────────────────────────────────────────────

    const actions = {
        async connectPeer(address) {
            await api.connectPeer(address)
            const peers = await api.getPeers()
            dispatch({
                type: 'LOADED',
                node: state.node,
                peers,
                agents: Object.values(state.agents),
                content: state.content,
            })
        },

        async disconnectPeer(node_id) {
            await api.disconnectPeer(node_id)
            dispatch({ type: 'PEER_REMOVED', node_id })
        },

        async updateAlias(alias) {
            await api.patchNodeAlias(alias)
            dispatch({ type: 'NODE_ALIAS_UPDATED', alias })
        },

        async unpinContent(cid) {
            await api.unpinContent(cid)
            dispatch({ type: 'CONTENT_REMOVED', cid })
        },

        async submitTask(agent, task, params) {
            return api.submitTask(agent, task, params)
        },
    }

    return { state, actions, handleWsEvent, handleWsStatus }
}
