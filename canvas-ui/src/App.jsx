import { useState, useEffect, useRef, useCallback } from "react"
import Sidebar from "./components/Sidebar"
import MeshCanvas from "./components/MeshCanvas"
import TopBar from "./components/TopBar"
import ChatPanel from "./components/ChatPanel"
import FilesPanel from "./components/FilesPanel"
import SettingsPanel from "./components/SettingsPanel"
import PeersPanel from "./components/PeersPanel"
import { useNeuralis } from "./hooks/useNeuralis"
import "./styles.css"

export default function App() {
    const [activePanel, setActivePanel] = useState("mesh")
    const [selectedNode, setSelectedNode] = useState(null)
    const { node, peers, agents, content, loading, error, refresh } = useNeuralis()

    const panels = {
        mesh: <MeshCanvas node={node} peers={peers} onSelectNode={setSelectedNode} selectedNode={selectedNode} />,
        chat: <ChatPanel agents={agents} node={node} />,
        files: <FilesPanel content={content} node={node} refresh={refresh} />,
        peers: <PeersPanel peers={peers} node={node} />,
        settings: <SettingsPanel node={node} />,
    }

    return (
        <div className="app">
            <TopBar node={node} peers={peers} agents={agents} />
            <div className="app-body">
                <Sidebar active={activePanel} onChange={setActivePanel} peers={peers} agents={agents} />
                <main className="main-content">
                    {loading && !node ? (
                        <div className="boot-screen">
                            <div className="boot-logo">
                                <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
                                    <polygon points="24,2 44,13 44,35 24,46 4,35 4,13" fill="none" stroke="currentColor" strokeWidth="1.5" className="hex-outer" />
                                    <polygon points="24,10 37,17 37,31 24,38 11,31 11,17" fill="none" stroke="currentColor" strokeWidth="0.75" opacity="0.4" />
                                    <circle cx="24" cy="24" r="4" fill="currentColor" />
                                </svg>
                            </div>
                            <p className="boot-text">Connecting to mesh...</p>
                        </div>
                    ) : error ? (
                        <div className="boot-screen">
                            <div className="boot-logo error">
                                <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
                                    <polygon points="24,2 44,13 44,35 24,46 4,35 4,13" fill="none" stroke="currentColor" strokeWidth="1.5" />
                                    <line x1="24" y1="16" x2="24" y2="28" stroke="currentColor" strokeWidth="2" />
                                    <circle cx="24" cy="33" r="1.5" fill="currentColor" />
                                </svg>
                            </div>
                            <p className="boot-text">Node unreachable</p>
                            <p className="boot-sub">Make sure Neuralis is running on port 7100</p>
                        </div>
                    ) : (
                        panels[activePanel]
                    )}
                </main>
            </div>
        </div>
    )
}
