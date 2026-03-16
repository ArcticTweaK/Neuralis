/**
 * components/ContentPanel.jsx
 * ===========================
 * Collapsible panel listing all pinned CIDs in the local IPFS store.
 * Supports adding text content and unpinning.
 */

import { useState } from 'react'
import { formatBytes } from '../lib/utils'

export default function ContentPanel({ content, onUnpin }) {
    const [expanded, setExpanded] = useState(false)
    const [copied, setCopied] = useState(null)

    function copyToClipboard(cid) {
        navigator.clipboard.writeText(cid).then(() => {
            setCopied(cid)
            setTimeout(() => setCopied(null), 1500)
        })
    }

    return (
        <div className="panel rounded-md fade-in-up" style={{ backdropFilter: 'blur(8px)' }}>
            <button
                className="w-full flex items-center justify-between px-4 py-3 hover:bg-white hover:bg-opacity-5 transition-colors rounded-t-md"
                onClick={() => setExpanded(e => !e)}
            >
                <div className="flex items-center gap-2">
                    <span className="text-text-dim text-xs tracking-widest uppercase">Content</span>
                    <span className="text-text-dim text-xs">{content.length} pinned</span>
                </div>
                <span className="text-text-dim text-xs">{expanded ? '▲' : '▼'}</span>
            </button>

            {expanded && (
                <div className="border-t border-border max-h-48 overflow-y-auto">
                    {content.length === 0 ? (
                        <div className="px-4 py-4 text-center text-text-dim text-xs">
                            No pinned content.
                        </div>
                    ) : (
                        <ul className="divide-y divide-border">
                            {content.map(item => (
                                <li key={item.cid} className="px-4 py-2 group flex items-center justify-between gap-2 hover:bg-white hover:bg-opacity-[0.03] transition-colors">
                                    <div className="min-w-0">
                                        {item.name && (
                                            <div className="text-text text-xs truncate mb-0.5">{item.name}</div>
                                        )}
                                        <div
                                            className="text-text-dim font-mono text-xs truncate cursor-pointer hover:text-info transition-colors"
                                            title={item.cid}
                                            onClick={() => copyToClipboard(item.cid)}
                                        >
                                            {copied === item.cid ? '✓ copied' : item.cid.slice(0, 20) + '…' + item.cid.slice(-6)}
                                        </div>
                                        {item.size && (
                                            <div className="text-text-dim text-xs opacity-50">{formatBytes(item.size)}</div>
                                        )}
                                    </div>
                                    <button
                                        onClick={() => onUnpin?.(item.cid)}
                                        className="text-xs text-red-400 opacity-0 group-hover:opacity-60 hover:!opacity-100 transition-opacity shrink-0"
                                        title="Unpin"
                                    >
                                        ✕
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            )}
        </div>
    )
}
