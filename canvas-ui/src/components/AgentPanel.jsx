/**
 * components/AgentPanel.jsx
 * =========================
 * Collapsible bottom panel listing all local agents, their state/capabilities,
 * and a quick task submission form.
 */

import { useState } from 'react'
import { agentStateColor } from '../lib/utils'

const STATE_DOT = {
    RUNNING: '●',
    IDLE: '◉',
    LOADING: '◌',
    ERROR: '✕',
    STOPPED: '○',
}

export default function AgentPanel({ agents, onSubmitTask }) {
    const [expanded, setExpanded] = useState(true)
    const [selected, setSelected] = useState(null)
    const [taskInput, setTask] = useState('')
    const [result, setResult] = useState(null)
    const [running, setRunning] = useState(false)
    const [error, setError] = useState(null)

    const agentList = Object.values(agents || {})

    async function submitTask() {
        if (!selected || !taskInput.trim()) return
        setRunning(true)
        setResult(null)
        setError(null)
        try {
            const res = await onSubmitTask(selected, taskInput.trim(), {})
            setResult(res)
        } catch (err) {
            setError(err.message)
        } finally {
            setRunning(false)
        }
    }

    return (
        <div className="panel rounded-md fade-in-up" style={{ backdropFilter: 'blur(8px)' }}>
            {/* Header */}
            <button
                className="w-full flex items-center justify-between px-4 py-3 hover:bg-white hover:bg-opacity-5 transition-colors rounded-t-md"
                onClick={() => setExpanded(e => !e)}
            >
                <div className="flex items-center gap-2">
                    <span className="text-text-dim text-xs tracking-widest uppercase">Agents</span>
                    <span className="text-text-dim text-xs">{agentList.length}</span>
                </div>
                <span className="text-text-dim text-xs">{expanded ? '▲' : '▼'}</span>
            </button>

            {expanded && (
                <div className="border-t border-border">
                    {agentList.length === 0 ? (
                        <div className="px-4 py-4 text-center text-text-dim text-xs">
                            No agents loaded.<br />
                            <span className="opacity-60">Drop plugins into /agents/</span>
                        </div>
                    ) : (
                        <>
                            {/* Agent rows */}
                            <div className="divide-y divide-border">
                                {agentList.map(agent => (
                                    <AgentRow
                                        key={agent.name}
                                        agent={agent}
                                        selected={selected === agent.name}
                                        onSelect={() => setSelected(s => s === agent.name ? null : agent.name)}
                                    />
                                ))}
                            </div>

                            {/* Task input */}
                            {selected && (
                                <div className="px-4 py-3 border-t border-border fade-in-up">
                                    <div className="text-text-dim text-xs mb-1.5">
                                        TASK → <span className="text-accent">{selected}</span>
                                    </div>
                                    <div className="flex gap-2">
                                        <input
                                            value={taskInput}
                                            onChange={e => setTask(e.target.value)}
                                            onKeyDown={e => e.key === 'Enter' && submitTask()}
                                            placeholder="describe task…"
                                            className="flex-1 bg-grid border border-border text-text text-xs font-mono px-2 py-1.5 rounded outline-none focus:border-accent transition-colors"
                                        />
                                        <button
                                            onClick={submitTask}
                                            disabled={running || !taskInput.trim()}
                                            className="text-xs px-3 py-1.5 bg-accent-glow border border-accent border-opacity-40 text-accent rounded hover:bg-opacity-100 disabled:opacity-40 transition-colors whitespace-nowrap"
                                        >
                                            {running ? '…' : 'RUN'}
                                        </button>
                                    </div>
                                    {error && (
                                        <div className="mt-2 text-xs text-red-400">{error}</div>
                                    )}
                                    {result && (
                                        <div className="mt-2 p-2 bg-grid rounded border border-border text-xs font-mono text-text max-h-24 overflow-y-auto">
                                            <div className="text-text-dim text-xs mb-1">{result.status} · {result.duration_ms}ms</div>
                                            <pre className="whitespace-pre-wrap break-all">{JSON.stringify(result.data, null, 2)}</pre>
                                        </div>
                                    )}
                                </div>
                            )}
                        </>
                    )}
                </div>
            )}
        </div>
    )
}

function AgentRow({ agent, selected, onSelect }) {
    const dotClass = agentStateColor(agent.state)

    return (
        <div
            className={`px-4 py-2.5 cursor-pointer transition-colors ${selected ? 'bg-accent-glow' : 'hover:bg-white hover:bg-opacity-[0.03]'}`}
            onClick={onSelect}
        >
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                    <span className={`text-xs ${dotClass}`}>{STATE_DOT[agent.state?.toUpperCase()] ?? '○'}</span>
                    <span className="text-text font-mono text-xs">{agent.name}</span>
                    <span className="text-text-dim text-xs opacity-50">v{agent.version ?? '?'}</span>
                </div>
                <span className={`text-xs ${dotClass}`}>{agent.state?.toLowerCase()}</span>
            </div>
            {selected && agent.capabilities?.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1 fade-in-up">
                    {agent.capabilities.map(cap => (
                        <span key={cap} className="px-1.5 py-0.5 bg-grid border border-border text-text-dim text-xs rounded">
                            {cap}
                        </span>
                    ))}
                </div>
            )}
        </div>
    )
}
