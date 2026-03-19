export default function TopBar({ node, peers, agents }) {
    const peersOnline = peers?.filter(p => p.status === "VERIFIED" || p.status === "CONNECTED").length ?? 0
    const agentsActive = agents?.length ?? 0
    const nodeAlias = node?.alias || "dev-node"
    const nodeId = node?.node_id ? node.node_id.slice(0, 16) + "..." : "..."

    return (
        <header className="topbar">
            <a className="topbar-logo" href="#">
                <svg width="22" height="22" viewBox="0 0 48 48" fill="none">
                    <polygon points="24,2 44,13 44,35 24,46 4,35 4,13" fill="none" stroke="currentColor" strokeWidth="2" />
                    <polygon points="24,12 36,19 36,29 24,36 12,29 12,19" fill="none" stroke="currentColor" strokeWidth="1" opacity="0.5" />
                    <circle cx="24" cy="24" r="4" fill="currentColor" />
                </svg>
                NEURALIS
            </a>

            <div className="topbar-address">
                <span className="topbar-address-icon">
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                        <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M5.5 8h5M8 5.5v5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                    </svg>
                </span>
                <input
                    type="text"
                    defaultValue={`neuralis://${nodeAlias}`}
                    readOnly
                />
            </div>

            <div className="topbar-stats">
                <div className="topbar-stat">
                    <span className={`dot ${node?.state === "RUNNING" ? "online" : ""}`} />
                    <span className="val">{nodeAlias}</span>
                </div>
                <div className="topbar-stat">
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                        <circle cx="5" cy="8" r="2.5" />
                        <circle cx="11" cy="8" r="2.5" />
                        <path d="M7.5 8h1" />
                    </svg>
                    <span className="val">{peersOnline}</span>
                    <span>peers</span>
                </div>
                <div className="topbar-stat">
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                        <rect x="3" y="3" width="10" height="10" rx="2" />
                        <path d="M6 8h4M8 6v4" strokeLinecap="round" />
                    </svg>
                    <span className="val">{agentsActive}</span>
                    <span>agents</span>
                </div>
                <div className="topbar-privacy">
                    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                        <path d="M8 2L3 4v4c0 3 2.5 5.5 5 6 2.5-.5 5-3 5-6V4L8 2z" />
                    </svg>
                    PRIVATE
                </div>
            </div>
        </header>
    )
}
