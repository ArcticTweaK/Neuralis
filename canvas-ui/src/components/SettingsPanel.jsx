export default function SettingsPanel({ node }) {
    if (!node) return <div className="empty-state"><h3>Loading...</h3></div>

    const rows = [
        { label: "Node ID", sub: "Your unique identity on the mesh", val: node.node_id },
        { label: "Peer ID", sub: "libp2p peer identifier", val: node.peer_id },
        { label: "Alias", sub: "Human-readable node name", val: node.alias },
        { label: "Public Key", sub: "Ed25519 public key (hex)", val: node.public_key?.slice(0, 32) + "..." },
        { label: "State", sub: "Current node state", val: node.state },
        { label: "API", sub: "Canvas API endpoint", val: "http://127.0.0.1:7100" },
    ]

    const networkRows = node.listen_addresses?.map(addr => ({
        label: "Listen", sub: "TCP/UDP listener", val: addr
    })) ?? []

    const uptime = node.uptime_seconds
        ? `${Math.floor(node.uptime_seconds / 60)}m ${Math.floor(node.uptime_seconds % 60)}s`
        : "—"

    return (
        <div className="settings-panel">
            <div className="panel-header" style={{ padding: "20px 0 16px" }}>
                <div>
                    <div className="panel-title">Settings</div>
                    <div className="panel-subtitle">Node configuration · read-only</div>
                </div>
            </div>

            <div className="settings-section">
                <div className="settings-section-title">Identity</div>
                {rows.map(r => (
                    <div key={r.label} className="settings-row">
                        <div>
                            <div className="settings-row-label">{r.label}</div>
                            <div className="settings-row-sub">{r.sub}</div>
                        </div>
                        <div className="settings-val">{r.val}</div>
                    </div>
                ))}
            </div>

            <div className="settings-section">
                <div className="settings-section-title">Network</div>
                {networkRows.map((r, i) => (
                    <div key={i} className="settings-row">
                        <div>
                            <div className="settings-row-label">{r.label}</div>
                            <div className="settings-row-sub">{r.sub}</div>
                        </div>
                        <div className="settings-val">{r.val}</div>
                    </div>
                ))}
                <div className="settings-row">
                    <div>
                        <div className="settings-row-label">mDNS Discovery</div>
                        <div className="settings-row-sub">LAN peer discovery</div>
                    </div>
                    <div className={`toggle ${node.mdns_enabled ? "on" : ""}`}>
                        <div className="toggle-thumb" />
                    </div>
                </div>
                <div className="settings-row">
                    <div>
                        <div className="settings-row-label">DHT</div>
                        <div className="settings-row-sub">Global peer routing</div>
                    </div>
                    <div className={`toggle ${node.dht_enabled ? "on" : ""}`}>
                        <div className="toggle-thumb" />
                    </div>
                </div>
            </div>

            <div className="settings-section">
                <div className="settings-section-title">Runtime</div>
                <div className="settings-row">
                    <div>
                        <div className="settings-row-label">Uptime</div>
                        <div className="settings-row-sub">Time since last boot</div>
                    </div>
                    <div className="settings-val">{uptime}</div>
                </div>
                <div className="settings-row">
                    <div>
                        <div className="settings-row-label">Subsystems</div>
                        <div className="settings-row-sub">Active modules</div>
                    </div>
                    <div className="settings-val">{node.subsystems?.join(", ") || "—"}</div>
                </div>
                <div className="settings-row">
                    <div>
                        <div className="settings-row-label">Telemetry</div>
                        <div className="settings-row-sub">Zero telemetry — always off</div>
                    </div>
                    <div className="toggle">
                        <div className="toggle-thumb" />
                    </div>
                </div>
                <div className="settings-row">
                    <div>
                        <div className="settings-row-label">Max Peers</div>
                        <div className="settings-row-sub">Connection limit</div>
                    </div>
                    <div className="settings-val">{node.max_peers ?? 50}</div>
                </div>
            </div>
        </div>
    )
}
