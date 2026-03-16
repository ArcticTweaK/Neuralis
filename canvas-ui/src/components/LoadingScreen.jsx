/**
 * components/LoadingScreen.jsx
 * ============================
 * Boot splash displayed while the initial API fetch is in progress
 * or while the node is unreachable.
 */

export default function LoadingScreen({ error }) {
    return (
        <div className="w-full h-full flex items-center justify-center bg-void scanlines">
            <div className="text-center space-y-6 fade-in-up">
                {/* Hex logo */}
                <svg viewBox="0 0 80 80" className="w-20 h-20 mx-auto animate-spin-slow" style={{ filter: 'drop-shadow(0 0 12px rgba(0,255,136,0.6))' }}>
                    <polygon points="40,4 74,22 74,58 40,76 6,58 6,22" fill="none" stroke="#00ff88" strokeWidth="1.5" opacity="0.8" />
                    <polygon points="40,14 66,28 66,52 40,66 14,52 14,28" fill="none" stroke="#00ff88" strokeWidth="0.5" opacity="0.3" />
                    <circle cx="40" cy="40" r="6" fill="#00ff88" opacity="0.9" />
                    <circle cx="40" cy="40" r="10" fill="none" stroke="#00ff88" strokeWidth="0.5" opacity="0.4" />
                </svg>

                {/* Title */}
                <div>
                    <div className="text-accent font-display text-2xl glow-accent tracking-widest">NEURALIS</div>
                    <div className="text-text-dim text-xs mt-1 tracking-widest">DECENTRALIZED AI MESH</div>
                </div>

                {/* Status */}
                {error ? (
                    <div className="space-y-2">
                        <div className="text-warn text-xs">CANVAS API UNREACHABLE</div>
                        <div className="text-text-dim text-xs font-mono max-w-xs">{error}</div>
                        <div className="text-text-dim text-xs opacity-50">
                            Make sure the Neuralis node is running on port 7100.
                        </div>
                    </div>
                ) : (
                    <div className="text-text-dim text-xs cursor-blink tracking-wider">
                        CONNECTING TO LOCAL NODE
                    </div>
                )}

                {/* Boot lines */}
                <div className="text-text-dim text-xs opacity-30 space-y-0.5 font-mono text-left max-w-xs mx-auto">
                    <div>[ OK ] identity loaded</div>
                    <div>[ OK ] mesh transport init</div>
                    <div>[ OK ] ipfs store mounted</div>
                    <div>{error ? '[FAIL]' : '[ .. ]'} canvas api {error ? 'unreachable' : 'connecting'}</div>
                </div>
            </div>
        </div>
    )
}
