import { useState, useRef, useEffect } from "react"
import * as api from "../lib/api"

export default function ChatPanel({ agents, node }) {
    const [selectedAgent, setSelectedAgent] = useState(null)
    const [messages, setMessages] = useState([])
    const [input, setInput] = useState("")
    const [loading, setLoading] = useState(false)
    const messagesEndRef = useRef(null)

    useEffect(() => {
        if (agents?.length && !selectedAgent) {
            setSelectedAgent(agents[0])
        }
    }, [agents])

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
    }, [messages])

    const quickTasks = selectedAgent?.capabilities?.slice(0, 4) ?? []

    const send = async (text, task = null) => {
        if (!selectedAgent || (!text.trim() && !task)) return
        const userMsg = text.trim() || task

        setMessages(prev => [...prev, { role: "user", text: userMsg, ts: Date.now() }])
        setInput("")
        setLoading(true)

        try {
            let payload = {}
            let resolvedTask = task || "ask"

            if (!task) {
                // Auto-detect task from input
                const lower = userMsg.toLowerCase()
                if (lower.startsWith("math:") || lower.match(/^\d|^calculate|^compute|^eval/)) {
                    resolvedTask = "math"
                    payload = { expression: userMsg.replace(/^math:\s*/i, "") }
                } else if (lower.startsWith("summarize:") || lower.startsWith("summarise:")) {
                    resolvedTask = "summarize"
                    payload = { text: userMsg.replace(/^summari[sz]e:\s*/i, ""), sentences: 2 }
                } else if (userMsg === "ping") {
                    resolvedTask = "ping"
                } else if (userMsg === "help") {
                    resolvedTask = "help"
                } else {
                    resolvedTask = "ask"
                    payload = { question: userMsg }
                }
            } else {
                if (task === "ask") payload = { question: "Tell me what you can do" }
                else if (task === "math") payload = { expression: "2 ** 10 + sqrt(144)" }
                else if (task === "summarize") payload = { text: "Neuralis is a decentralized AI mesh. It connects nodes together. Each node runs local AI agents. There is no central server.", sentences: 2 }
            }

            const res = await api.submitTask(selectedAgent.name, resolvedTask, payload)

            setMessages(prev => [...prev, {
                role: "agent",
                agent: selectedAgent.name,
                status: res.status,
                text: formatResponse(res),
                data: res.data,
                duration: res.duration_ms,
                ts: Date.now(),
            }])
        } catch (err) {
            setMessages(prev => [...prev, {
                role: "agent",
                agent: selectedAgent?.name,
                status: "error",
                text: err.message,
                ts: Date.now(),
            }])
        } finally {
            setLoading(false)
        }
    }

    const formatResponse = (res) => {
        if (res.status === "error") return res.error || "Unknown error"
        const d = res.data
        if (!d) return "Done"
        if (d.answer) return d.answer
        if (d.summary) return d.summary
        if (d.result !== undefined) return `= ${d.result}`
        if (d.pong) return `Pong from ${d.node}`
        if (d.capabilities) return `Capabilities: ${d.capabilities.join(", ")}`
        return JSON.stringify(d, null, 2)
    }

    return (
        <div className="chat-panel">
            {/* Agent sidebar */}
            <div className="chat-sidebar">
                <div className="chat-sidebar-header">Agents</div>
                <div className="agent-list">
                    {agents?.length === 0 && (
                        <div className="empty-state" style={{ padding: "24px 12px" }}>
                            <p>No agents loaded</p>
                        </div>
                    )}
                    {agents?.map(a => (
                        <div
                            key={a.name}
                            className={`agent-item ${selectedAgent?.name === a.name ? "active" : ""}`}
                            onClick={() => { setSelectedAgent(a); setMessages([]) }}
                        >
                            <div className="agent-item-name">
                                <span className="agent-item-status" />
                                {a.name}
                            </div>
                            <div className="agent-item-caps">{a.capabilities?.slice(0, 3).join(" · ")}</div>
                        </div>
                    ))}
                </div>
            </div>

            {/* Chat main */}
            <div className="chat-main">
                {!selectedAgent ? (
                    <div className="chat-empty">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" opacity="0.4">
                            <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
                        </svg>
                        <h3>No agent selected</h3>
                        <p style={{ fontSize: 12, color: "var(--text-3)" }}>Select an agent from the sidebar</p>
                    </div>
                ) : (
                    <>
                        <div className="chat-messages">
                            {messages.length === 0 && (
                                <div style={{ textAlign: "center", paddingTop: 40, color: "var(--text-3)" }}>
                                    <div style={{ fontSize: 13, marginBottom: 4, color: "var(--text-2)" }}>
                                        {selectedAgent.name} agent
                                    </div>
                                    <div style={{ fontSize: 12 }}>
                                        {selectedAgent.description || "Ask anything or pick a task below"}
                                    </div>
                                </div>
                            )}
                            {messages.map((msg, i) => (
                                <div key={i} className={`chat-msg ${msg.role}`}>
                                    <div className="chat-msg-avatar">
                                        {msg.role === "user" ? "U" : msg.agent?.[0]?.toUpperCase() ?? "A"}
                                    </div>
                                    <div>
                                        <div className={`chat-msg-bubble ${msg.status === "error" ? "error" : ""}`}
                                            style={msg.status === "error" ? { borderColor: "rgba(240,80,80,0.3)", color: "var(--red)" } : {}}>
                                            {msg.text}
                                        </div>
                                        <div className="chat-msg-meta">
                                            {msg.role === "agent" && msg.duration && `${Math.round(msg.duration)}ms`}
                                        </div>
                                    </div>
                                </div>
                            ))}
                            {loading && (
                                <div className="chat-msg">
                                    <div className="chat-msg-avatar">{selectedAgent.name[0].toUpperCase()}</div>
                                    <div className="chat-msg-bubble" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                        <div className="spinner" />
                                        <span style={{ color: "var(--text-3)", fontSize: 13 }}>Thinking...</span>
                                    </div>
                                </div>
                            )}
                            <div ref={messagesEndRef} />
                        </div>

                        <div className="chat-input-area">
                            <div className="chat-quick-tasks">
                                {quickTasks.map(t => (
                                    <button key={t} className="quick-task" onClick={() => send("", t)}>
                                        {t}
                                    </button>
                                ))}
                            </div>
                            <div className="chat-input-row">
                                <textarea
                                    className="chat-input"
                                    placeholder={`Message ${selectedAgent.name}...`}
                                    value={input}
                                    onChange={e => setInput(e.target.value)}
                                    onKeyDown={e => {
                                        if (e.key === "Enter" && !e.shiftKey) {
                                            e.preventDefault()
                                            send(input)
                                        }
                                    }}
                                    rows={1}
                                />
                                <button
                                    className="chat-send-btn"
                                    onClick={() => send(input)}
                                    disabled={loading || !input.trim()}
                                >
                                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                        <line x1="22" y1="2" x2="11" y2="13" />
                                        <polygon points="22,2 15,22 11,13 2,9 22,2" />
                                    </svg>
                                </button>
                            </div>
                        </div>
                    </>
                )}
            </div>
        </div>
    )
}
