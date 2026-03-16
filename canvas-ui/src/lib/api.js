/**
 * lib/api.js
 * ==========
 * All HTTP calls to the Neuralis Canvas API (http://localhost:7100).
 * Every function returns the parsed JSON response or throws an ApiError.
 */

const BASE = '' // relative — proxied by Vite to http://localhost:7100

export class ApiError extends Error {
    constructor(status, message) {
        super(message)
        this.status = status
        this.name = 'ApiError'
    }
}

async function request(method, path, body = undefined) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    }
    if (body !== undefined) opts.body = JSON.stringify(body)

    let res
    try {
        res = await fetch(`${BASE}${path}`, opts)
    } catch (err) {
        throw new ApiError(0, `Network error: ${err.message}`)
    }

    let data
    try {
        data = await res.json()
    } catch {
        data = null
    }

    if (!res.ok) {
        throw new ApiError(res.status, data?.detail ?? `HTTP ${res.status}`)
    }
    return data
}

// ─── Health ──────────────────────────────────────────────────────────────────

export const getHealth = () => request('GET', '/health')

// ─── Node ────────────────────────────────────────────────────────────────────

export const getNodeStatus = () => request('GET', '/api/node/status')
export const getNodeConfig = () => request('GET', '/api/node/config')
export const patchNodeAlias = (alias) => request('PATCH', '/api/node/alias', { alias })

// ─── Peers ───────────────────────────────────────────────────────────────────

export const getPeers = () => request('GET', '/api/peers')
export const connectPeer = (address) => request('POST', '/api/peers/connect', { address })
export const disconnectPeer = (node_id) => request('DELETE', `/api/peers/${node_id}`)
export const pingPeer = (node_id) => request('POST', `/api/peers/${node_id}/ping`)

// ─── Content ─────────────────────────────────────────────────────────────────

export const getContent = () => request('GET', '/api/content')
export const addContent = (data, name, tags) => request('POST', '/api/content', { data, name, tags })
export const getContentByCid = (cid) => request('GET', `/api/content/${cid}`)
export const unpinContent = (cid) => request('DELETE', `/api/content/${cid}`)

// ─── Agents ──────────────────────────────────────────────────────────────────

export const getAgents = () => request('GET', '/api/agents')
export const getAgentByName = (name) => request('GET', `/api/agents/${name}`)
export const submitTask = (agent, task, params) =>
    request('POST', '/api/agents/task', { agent, task, params })

// ─── Protocol / Remote ───────────────────────────────────────────────────────

export const getRemoteNodes = () => request('GET', '/api/protocol/nodes')
export const routeTask = (node_id, agent, task, params) =>
    request('POST', '/api/protocol/route', { node_id, agent, task, params })
