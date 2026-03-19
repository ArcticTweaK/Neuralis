const TABS = [
    {
        id: "mesh",
        label: "Mesh",
        icon: (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="12" cy="12" r="2" />
                <circle cx="5" cy="5" r="2" />
                <circle cx="19" cy="5" r="2" />
                <circle cx="5" cy="19" r="2" />
                <circle cx="19" cy="19" r="2" />
                <path d="M7 5h10M5 7v10M19 7v10M7 19h10M7 7l4 4M13 13l4 4M17 7l-4 4M7 17l4-4" strokeLinecap="round" />
            </svg>
        ),
    },
    {
        id: "chat",
        label: "Chat",
        icon: (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
        ),
    },
    {
        id: "files",
        label: "Files",
        icon: (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z" strokeLinecap="round" strokeLinejoin="round" />
                <polyline points="13,2 13,9 20,9" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
        ),
    },
    {
        id: "peers",
        label: "Peers",
        icon: (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" strokeLinecap="round" />
                <circle cx="9" cy="7" r="4" />
                <path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" strokeLinecap="round" />
            </svg>
        ),
    },
    {
        id: "settings",
        label: "Settings",
        icon: (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" strokeLinecap="round" />
            </svg>
        ),
    },
]

export default function Sidebar({ active, onChange, peers, agents }) {
    const peersOnline = peers?.filter(p => p.status === "VERIFIED" || p.status === "CONNECTED").length ?? 0
    const agentsActive = agents?.length ?? 0

    return (
        <nav className="sidebar">
            {TABS.map(tab => (
                <button
                    key={tab.id}
                    className={`sidebar-btn ${active === tab.id ? "active" : ""}`}
                    onClick={() => onChange(tab.id)}
                    title={tab.label}
                >
                    {tab.icon}
                    {tab.id === "peers" && peersOnline > 0 && <span className="badge" />}
                    {tab.id === "chat" && agentsActive > 0 && <span className="badge" />}
                </button>
            ))}
            <div className="sidebar-spacer" />
        </nav>
    )
}
