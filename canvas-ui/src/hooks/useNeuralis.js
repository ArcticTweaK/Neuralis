import { useState, useEffect, useCallback, useRef } from "react"
import * as api from "../lib/api"

export function useNeuralis() {
    const [node, setNode] = useState(null)
    const [peers, setPeers] = useState([])
    const [agents, setAgents] = useState([])
    const [content, setContent] = useState([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const mountedRef = useRef(true)

    const fetchAll = useCallback(async () => {
        try {
            const [nodeRes, peersRes, agentsRes, contentRes] = await Promise.all([
                api.getNodeStatus().catch(() => null),
                api.getPeers().catch(() => ({ peers: [] })),
                api.getAgents().catch(() => ({ agents: [] })),
                api.getContent().catch(() => ({ pins: [] })),
            ])

            if (!mountedRef.current) return

            if (nodeRes) {
                setNode(nodeRes)
                setError(null)
            } else {
                setError("Node unreachable")
            }

            setPeers(peersRes?.peers ?? peersRes ?? [])
            setAgents(agentsRes?.agents ?? agentsRes ?? [])
            setContent(contentRes?.pins ?? contentRes ?? [])
        } catch (err) {
            if (mountedRef.current) setError(err.message)
        } finally {
            if (mountedRef.current) setLoading(false)
        }
    }, [])

    useEffect(() => {
        mountedRef.current = true
        fetchAll()
        const interval = setInterval(fetchAll, 3000)
        return () => {
            mountedRef.current = false
            clearInterval(interval)
        }
    }, [fetchAll])

    return { node, peers, agents, content, loading, error, refresh: fetchAll }
}
