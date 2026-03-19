const BASE = ""

export class ApiError extends Error {
    constructor(status, message) {
        super(message)
        this.status = status
        this.name = "ApiError"
    }
}

async function request(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } }
    if (body !== undefined) opts.body = JSON.stringify(body)
    let res
    try {
        res = await fetch(`${BASE}${path}`, opts)
    } catch (err) {
        throw new ApiError(0, `Network error: ${err.message}`)
    }
    let data
    try { data = await res.json() } catch { data = null }
    if (!res.ok) throw new ApiError(res.status, data?.detail ?? `HTTP ${res.status}`)
    return data
}

export const getNodeStatus = () => request("GET", "/api/node/status")
export const getPeers = () => request("GET", "/api/peers")
export const connectPeer = (multiaddr) => request("POST", "/api/peers/connect", { multiaddr })
export const getAgents = () => request("GET", "/api/agents")
export const getContent = () => request("GET", "/api/content")
export const addContent = (data, name) => request("POST", "/api/content", { data, pin: true, name })
export const getContentByCid = (cid) => request("GET", `/api/content/${cid}`)
export const submitTask = (agent, task, payload) =>
    request("POST", "/api/agents/task", { task, payload, target: agent })
