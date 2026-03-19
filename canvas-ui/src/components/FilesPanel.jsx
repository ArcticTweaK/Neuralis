import { useState } from "react"
import * as api from "../lib/api"

function formatBytes(bytes) {
    if (!bytes) return "0 B"
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatDate(ts) {
    if (!ts) return ""
    return new Date(ts * 1000).toLocaleDateString()
}

export default function FilesPanel({ content, node, refresh }) {
    const [adding, setAdding] = useState(false)
    const [newText, setNewText] = useState("")
    const [newName, setNewName] = useState("")
    const [loading, setLoading] = useState(false)
    const [fetchCid, setFetchCid] = useState("")
    const [toast, setToast] = useState(null)

    const showToast = (msg, type = "success") => {
        setToast({ msg, type })
        setTimeout(() => setToast(null), 3000)
    }

    const handleAdd = async () => {
        if (!newText.trim()) return
        setLoading(true)
        try {
            const res = await api.addContent(newText, newName || undefined)
            showToast(`Stored: ${res.cid.slice(0, 20)}...`)
            setNewText(""); setNewName(""); setAdding(false)
            refresh?.()
        } catch (err) {
            showToast(err.message, "error")
        } finally {
            setLoading(false)
        }
    }

    const handleFetch = async () => {
        if (!fetchCid.trim()) return
        setLoading(true)
        try {
            const res = await api.getContentByCid(fetchCid.trim())
            showToast(`Retrieved ${formatBytes(res.size)} from mesh`)
            setFetchCid("")
            refresh?.()
        } catch (err) {
            showToast(err.message, "error")
        } finally {
            setLoading(false)
        }
    }

    const pins = content ?? []

    return (
        <div className="files-panel">
            <div className="panel-header">
                <div>
                    <div className="panel-title">Files</div>
                    <div className="panel-subtitle">{pins.length} pinned · content-addressed storage</div>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                    <button className="btn btn-ghost" onClick={() => setAdding(!adding)}>
                        {adding ? "Cancel" : "+ Store"}
                    </button>
                </div>
            </div>

            {adding && (
                <div style={{ padding: "12px 24px", borderBottom: "1px solid var(--border)", background: "var(--bg-2)" }}>
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                        <input
                            placeholder="Name (optional)"
                            value={newName}
                            onChange={e => setNewName(e.target.value)}
                            style={{
                                background: "var(--bg-3)", border: "1px solid var(--border)",
                                borderRadius: "var(--radius-sm)", padding: "8px 12px",
                                color: "var(--text)", fontSize: 13, outline: "none"
                            }}
                        />
                        <textarea
                            placeholder="Content to store on the mesh..."
                            value={newText}
                            onChange={e => setNewText(e.target.value)}
                            rows={3}
                            style={{
                                background: "var(--bg-3)", border: "1px solid var(--border)",
                                borderRadius: "var(--radius-sm)", padding: "8px 12px",
                                color: "var(--text)", fontSize: 13, outline: "none",
                                resize: "none", fontFamily: "var(--font-body)"
                            }}
                        />
                        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                            <button className="btn btn-primary" onClick={handleAdd} disabled={loading}>
                                {loading ? "Storing..." : "Store on mesh"}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Fetch by CID */}
            <div style={{ padding: "10px 24px", borderBottom: "1px solid var(--border)", display: "flex", gap: 8 }}>
                <input
                    className="connect-input"
                    placeholder="Fetch by CID from mesh..."
                    value={fetchCid}
                    onChange={e => setFetchCid(e.target.value)}
                    onKeyDown={e => e.key === "Enter" && handleFetch()}
                />
                <button className="btn btn-ghost" onClick={handleFetch} disabled={loading || !fetchCid.trim()}>
                    Fetch
                </button>
            </div>

            <div className="files-list">
                {pins.length === 0 && (
                    <div className="empty-state">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" opacity="0.4">
                            <path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z" />
                            <polyline points="13,2 13,9 20,9" />
                        </svg>
                        <h3>No files stored</h3>
                        <p>Store content on the mesh and it will appear here, pinned to your node.</p>
                    </div>
                )}
                {pins.map((pin, i) => (
                    <div key={pin.cid || i} className="file-item">
                        <div className="file-icon">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                                <path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z" />
                                <polyline points="13,2 13,9 20,9" />
                            </svg>
                        </div>
                        <div className="file-info">
                            <div className="file-name">{pin.name || "Unnamed"}</div>
                            <div className="file-cid">{pin.cid}</div>
                        </div>
                        <div className="file-meta">
                            <div className="file-size">{formatBytes(pin.size)}</div>
                            <div className="file-date">{formatDate(pin.pinned_at)}</div>
                            <div style={{ marginTop: 4 }}>
                                <span className="file-pin-badge">
                                    <svg width="8" height="8" viewBox="0 0 16 16" fill="currentColor">
                                        <circle cx="8" cy="8" r="4" />
                                    </svg>
                                    pinned
                                </span>
                            </div>
                        </div>
                    </div>
                ))}
            </div>

            {toast && (
                <div className={`toast ${toast.type}`}>{toast.msg}</div>
            )}
        </div>
    )
}
