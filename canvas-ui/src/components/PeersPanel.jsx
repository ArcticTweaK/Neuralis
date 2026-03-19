import { useState } from "react"
import * as api from "../lib/api"

export default function PeersPanel({ peers, node }) {
    const [multiaddr, setMultiaddr] = useState("")
    const [connecting, setConnecting] = useState(false)
    const [toast, setToast] = useState(null)

    const showToast = (msg, type = "success") => {
        setToast({ msg, type })
        setTimeout(() => setToast(null), 3000)
    }

    const handleConnect = async () => {
        if (!multiaddr.trim()) return
        setConnecting(true)
        try {
            const res = await api.connectPeer(multiaddr.trim())
            if (res.success) showToast("Dial scheduled — check back in a moment")
            else showToast(res.message || "Failed to connect", "error")
            setMultiaddr("")
        } catch (err) {
            showToast(err.message, "error")
        } finally {
            setConnecting(false)
        }
    }

    const allPeers = peers ?? []
    const connected = allPeers.filter(p => p.status === "VERIFIED" || p.status === "CONNECTED")
    const known = allPeers.filter(p => p.status !== "VERIFIED" && p.status !== "CONNECTED")

    return (
        <div className="peers-panel">
            <div className="panel-header">
                <div>
                    <div className="panel-title">Peers</div>
                    <div className="panel-subtitle">{connected.length} connected · {allPeers.length} known</div>
                </div>
            </div>

            <div className="connect-input-row">
                <input
                    className="connect-input"
                    placeholder="/ip4/x.x.x.x/tcp/7101/p2p/NRL1..."
                    value={multiaddr}
                    onChange={e => setMultiaddr(e.target.value)}
                    onKeyDown={e => e.key === "Enter" && handleConnect()}
                />
                <button
                    className="btn btn-primary"
                    onClick={handleConnect}
                    disabled={connecting || !multiaddr.trim()}
                    style={{ whiteSpace: "nowrap" }}
                >
                    {connecting ? "..." : "Connect"}
                </button>
            </div>

            <div className="peer-grid">
                {allPeers.length === 0 && (
                    <div className="empty-state" style={{ gridColumn: "1/-1" }}>
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" opacity="0.4">
                            <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" />
                            <circle cx="9" cy="7" r="4" />
                            <path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" />
                        </svg>
                        <h3>No peers yet</h3>
                        <p>Enter a multiaddr above to connect to another Neuralis node.</p>
                    </div>
                )}
                {allPeers.map(p => (
                    <div key={p.node_id} className="peer-card">
                        <div className="peer-card-top">
                            <div className="peer-avatar">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--text-3)" strokeWidth="1.5">
                                    <circle cx="12" cy="12" r="3" />
                                    <path d="M12 2v2M12 20v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M2 12h2M20 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
                                </svg>
                                <span className={`peer-status-dot ${p.status === "VERIFIED" ? "verified" : p.status === "CONNECTED" ? "connected" : ""}`} />
                            </div>
                            <div>
                                <div className="peer-name">{p.alias || "Unknown"}</div>
                                <div className={`peer-status-text ${p.status === "VERIFIED" ? "verified" : ""}`}>
                                    {p.status?.toLowerCase()}
                                </div>
                            </div>
                        </div>
                        <div className="peer-id">{p.node_id}</div>
                        <div className="peer-stats">
                            <div className="peer-stat">
                                <div className="peer-stat-label">Ping</div>
                                <div className="peer-stat-val">
                                    {p.last_ping_ms ? `${Math.round(p.last_ping_ms)}ms` : "—"}
                                </div>
                            </div>
                            <div className="peer-stat">
                                <div className="peer-stat-label">Failures</div>
                                <div className="peer-stat-val">{p.failed_attempts ?? 0}</div>
                            </div>
                            <div className="peer-stat" style={{ gridColumn: "1/-1" }}>
                                <div className="peer-stat-label">Address</div>
                                <div className="peer-stat-val" style={{ fontSize: 11, fontFamily: "var(--font-mono)" }}>
                                    {p.addresses?.[0] || "—"}
                                </div>
                            </div>
                        </div>
                    </div>
                ))}
            </div>

            {toast && <div className={`toast ${toast.type}`}>{toast.msg}</div>}
        </div>
    )
}
