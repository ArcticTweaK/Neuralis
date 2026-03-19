import { useEffect, useRef, useState } from "react"

function lerp(a, b, t) { return a + (b - a) * t }

export default function MeshCanvas({ node, peers, onSelectNode, selectedNode }) {
    const canvasRef = useRef(null)
    const animRef = useRef(null)
    const nodesRef = useRef([])
    const [hoveredNode, setHoveredNode] = useState(null)
    const [hoverPos, setHoverPos] = useState({ x: 0, y: 0 })
    const offsetRef = useRef({ x: 0, y: 0 })
    const dragRef = useRef({ dragging: false, startX: 0, startY: 0 })

    useEffect(() => {
        const canvas = canvasRef.current
        if (!canvas) return
        const ctx = canvas.getContext("2d")

        const resize = () => {
            canvas.width = canvas.offsetWidth * window.devicePixelRatio
            canvas.height = canvas.offsetHeight * window.devicePixelRatio
            ctx.scale(window.devicePixelRatio, window.devicePixelRatio)
        }
        resize()
        window.addEventListener("resize", resize)

        // Build node list
        const allNodes = []
        const cx = canvas.offsetWidth / 2
        const cy = canvas.offsetHeight / 2

        // Self node always center
        if (node) {
            allNodes.push({
                id: node.node_id,
                alias: node.alias || "this node",
                isSelf: true,
                x: cx,
                y: cy,
                tx: cx,
                ty: cy,
                vx: 0,
                vy: 0,
                radius: 28,
                status: "self",
                ping: null,
            })
        }

        // Peer nodes arranged in orbit
        const verifiedPeers = (peers || []).filter(p => p.status === "VERIFIED" || p.status === "CONNECTED")
        verifiedPeers.forEach((p, i) => {
            const angle = (i / Math.max(verifiedPeers.length, 1)) * Math.PI * 2 - Math.PI / 2
            const dist = 180 + Math.random() * 60
            allNodes.push({
                id: p.node_id,
                alias: p.alias || p.node_id.slice(0, 8),
                isSelf: false,
                x: cx + Math.cos(angle) * dist,
                y: cy + Math.sin(angle) * dist,
                tx: cx + Math.cos(angle) * dist,
                ty: cy + Math.sin(angle) * dist,
                vx: (Math.random() - 0.5) * 0.3,
                vy: (Math.random() - 0.5) * 0.3,
                radius: 18,
                status: p.status,
                ping: p.last_ping_ms,
            })
        })

        nodesRef.current = allNodes

        // Particle system for background
        const particles = Array.from({ length: 60 }, () => ({
            x: Math.random() * canvas.offsetWidth,
            y: Math.random() * canvas.offsetHeight,
            r: Math.random() * 1.5 + 0.3,
            vx: (Math.random() - 0.5) * 0.2,
            vy: (Math.random() - 0.5) * 0.2,
            alpha: Math.random() * 0.3 + 0.05,
        }))

        let frame = 0

        function draw() {
            const W = canvas.offsetWidth
            const H = canvas.offsetHeight
            ctx.clearRect(0, 0, W, H)

            const ox = offsetRef.current.x
            const oy = offsetRef.current.y
            frame++

            // Draw grid dots
            ctx.fillStyle = "rgba(255,255,255,0.04)"
            const gridSize = 40
            for (let gx = (ox % gridSize); gx < W; gx += gridSize) {
                for (let gy = (oy % gridSize); gy < H; gy += gridSize) {
                    ctx.beginPath()
                    ctx.arc(gx, gy, 0.8, 0, Math.PI * 2)
                    ctx.fill()
                }
            }

            // Update & draw particles
            particles.forEach(p => {
                p.x += p.vx; p.y += p.vy
                if (p.x < 0) p.x = W; if (p.x > W) p.x = 0
                if (p.y < 0) p.y = H; if (p.y > H) p.y = 0
                ctx.beginPath()
                ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
                ctx.fillStyle = `rgba(79,142,247,${p.alpha})`
                ctx.fill()
            })

            const nodes = nodesRef.current
            if (nodes.length === 0) {
                animRef.current = requestAnimationFrame(draw)
                return
            }

            // Draw connections
            const self = nodes.find(n => n.isSelf)
            nodes.forEach(n => {
                if (n.isSelf) return
                if (!self) return
                const sx = self.x + ox, sy = self.y + oy
                const nx = n.x + ox, ny = n.y + oy
                const grad = ctx.createLinearGradient(sx, sy, nx, ny)
                grad.addColorStop(0, "rgba(79,142,247,0.4)")
                grad.addColorStop(1, "rgba(61,214,140,0.15)")
                ctx.beginPath()
                ctx.moveTo(sx, sy)
                ctx.lineTo(nx, ny)
                ctx.strokeStyle = grad
                ctx.lineWidth = 1
                ctx.setLineDash([4, 8])
                ctx.stroke()
                ctx.setLineDash([])

                // Animate packet along connection
                const t = ((frame * 0.008) % 1)
                const px = lerp(sx, nx, t)
                const py = lerp(sy, ny, t)
                ctx.beginPath()
                ctx.arc(px, py, 2, 0, Math.PI * 2)
                ctx.fillStyle = "rgba(79,142,247,0.8)"
                ctx.fill()
            })

            // Draw nodes
            nodes.forEach(n => {
                const nx = n.x + ox
                const ny = n.y + oy
                const isHovered = hoveredNode?.id === n.id
                const isSelected = selectedNode?.id === n.id

                // Float animation for non-self nodes
                if (!n.isSelf) {
                    n.x += n.vx + Math.sin(frame * 0.01 + n.x) * 0.1
                    n.y += n.vy + Math.cos(frame * 0.01 + n.y) * 0.1
                    // Soft boundary
                    const dx = n.x - (self?.x ?? cx)
                    const dy = n.y - (self?.y ?? cy)
                    const dist = Math.sqrt(dx * dx + dy * dy)
                    if (dist > 280) {
                        n.vx -= dx * 0.0002
                        n.vy -= dy * 0.0002
                    }
                    if (dist < 100) {
                        n.vx += dx * 0.001
                        n.vy += dy * 0.001
                    }
                    n.vx *= 0.98; n.vy *= 0.98
                }

                // Outer ring pulse for self node
                if (n.isSelf) {
                    const pulse = Math.sin(frame * 0.04) * 6 + n.radius + 12
                    ctx.beginPath()
                    ctx.arc(nx, ny, pulse, 0, Math.PI * 2)
                    ctx.strokeStyle = "rgba(79,142,247,0.08)"
                    ctx.lineWidth = 2
                    ctx.stroke()

                    const pulse2 = Math.sin(frame * 0.04 + 1) * 4 + n.radius + 22
                    ctx.beginPath()
                    ctx.arc(nx, ny, pulse2, 0, Math.PI * 2)
                    ctx.strokeStyle = "rgba(79,142,247,0.04)"
                    ctx.lineWidth = 1.5
                    ctx.stroke()
                }

                // Node circle
                const glowAlpha = isHovered || isSelected ? 0.25 : 0.1
                const glowR = isHovered || isSelected ? n.radius + 16 : n.radius + 8
                const grd = ctx.createRadialGradient(nx, ny, 0, nx, ny, glowR)
                grd.addColorStop(0, n.isSelf ? `rgba(79,142,247,${glowAlpha})` : `rgba(61,214,140,${glowAlpha})`)
                grd.addColorStop(1, "rgba(0,0,0,0)")
                ctx.beginPath()
                ctx.arc(nx, ny, glowR, 0, Math.PI * 2)
                ctx.fillStyle = grd
                ctx.fill()

                // Node body
                ctx.beginPath()
                ctx.arc(nx, ny, n.radius, 0, Math.PI * 2)
                ctx.fillStyle = n.isSelf ? "#1a2035" : "#161e20"
                ctx.fill()
                ctx.strokeStyle = n.isSelf
                    ? (isSelected ? "#4f8ef7" : "rgba(79,142,247,0.6)")
                    : (isSelected ? "#3dd68c" : "rgba(61,214,140,0.4)")
                ctx.lineWidth = isHovered || isSelected ? 1.5 : 1
                ctx.stroke()

                // Inner hex for self
                if (n.isSelf) {
                    const hexR = 10
                    ctx.beginPath()
                    for (let i = 0; i < 6; i++) {
                        const a = (i / 6) * Math.PI * 2 - Math.PI / 6
                        if (i === 0) ctx.moveTo(nx + Math.cos(a) * hexR, ny + Math.sin(a) * hexR)
                        else ctx.lineTo(nx + Math.cos(a) * hexR, ny + Math.sin(a) * hexR)
                    }
                    ctx.closePath()
                    ctx.strokeStyle = "rgba(79,142,247,0.5)"
                    ctx.lineWidth = 1
                    ctx.stroke()
                } else {
                    // Dot for peers
                    ctx.beginPath()
                    ctx.arc(nx, ny, 4, 0, Math.PI * 2)
                    ctx.fillStyle = n.status === "VERIFIED" ? "#3dd68c" : "#f0a040"
                    ctx.fill()
                }

                // Label
                ctx.fillStyle = isHovered || isSelected ? "#f0f0f5" : "rgba(240,240,245,0.6)"
                ctx.font = `${n.isSelf ? "500" : "400"} 11px 'DM Sans', sans-serif`
                ctx.textAlign = "center"
                ctx.fillText(n.alias, nx, ny + n.radius + 14)
                if (n.ping !== null && n.ping !== undefined) {
                    ctx.fillStyle = "rgba(140,140,160,0.5)"
                    ctx.font = "10px 'DM Mono', monospace"
                    ctx.fillText(`${Math.round(n.ping)}ms`, nx, ny + n.radius + 26)
                }
            })

            animRef.current = requestAnimationFrame(draw)
        }

        draw()
        return () => {
            cancelAnimationFrame(animRef.current)
            window.removeEventListener("resize", resize)
        }
    }, [node, peers])

    const getNodeAt = (x, y) => {
        const nodes = nodesRef.current
        const ox = offsetRef.current.x
        const oy = offsetRef.current.y
        for (const n of nodes) {
            const dx = (n.x + ox) - x
            const dy = (n.y + oy) - y
            if (Math.sqrt(dx * dx + dy * dy) <= n.radius + 8) return n
        }
        return null
    }

    const handleMouseMove = (e) => {
        const rect = canvasRef.current.getBoundingClientRect()
        const x = e.clientX - rect.left
        const y = e.clientY - rect.top

        if (dragRef.current.dragging) {
            offsetRef.current.x += e.clientX - dragRef.current.startX
            offsetRef.current.y += e.clientY - dragRef.current.startY
            dragRef.current.startX = e.clientX
            dragRef.current.startY = e.clientY
            return
        }

        const n = getNodeAt(x, y)
        setHoveredNode(n)
        if (n) setHoverPos({ x: e.clientX - rect.left, y: e.clientY - rect.top })
        canvasRef.current.style.cursor = n ? "pointer" : "grab"
    }

    const handleMouseDown = (e) => {
        dragRef.current = { dragging: true, startX: e.clientX, startY: e.clientY }
        canvasRef.current.style.cursor = "grabbing"
    }

    const handleMouseUp = (e) => {
        const wasDragging = dragRef.current.dragging
        dragRef.current.dragging = false
        canvasRef.current.style.cursor = "grab"
        if (!wasDragging) return
        const rect = canvasRef.current.getBoundingClientRect()
        const n = getNodeAt(e.clientX - rect.left, e.clientY - rect.top)
        if (n) onSelectNode(n)
    }

    const handleClick = (e) => {
        const rect = canvasRef.current.getBoundingClientRect()
        const n = getNodeAt(e.clientX - rect.left, e.clientY - rect.top)
        if (n) onSelectNode(n === selectedNode ? null : n)
        else onSelectNode(null)
    }

    const allNodes = nodesRef.current
    const peerCount = (peers || []).filter(p => p.status === "VERIFIED" || p.status === "CONNECTED").length

    return (
        <div className="mesh-canvas">
            <canvas
                ref={canvasRef}
                style={{ width: "100%", height: "100%", cursor: "grab" }}
                onMouseMove={handleMouseMove}
                onMouseDown={handleMouseDown}
                onMouseUp={handleMouseUp}
                onClick={handleClick}
                onMouseLeave={() => setHoveredNode(null)}
            />

            {hoveredNode && !hoveredNode.isSelf && (
                <div className="mesh-node-card" style={{ left: hoverPos.x, top: hoverPos.y }}>
                    <div className="node-name">{hoveredNode.alias}</div>
                    <div className="node-id">{hoveredNode.id?.slice(0, 24)}...</div>
                    <div className="node-stats">
                        <div className="node-stat">Status: <span>{hoveredNode.status}</span></div>
                        {hoveredNode.ping && <div className="node-stat">Ping: <span>{Math.round(hoveredNode.ping)}ms</span></div>}
                    </div>
                </div>
            )}

            <div className="mesh-legend">
                <div className="mesh-legend-item">
                    <div className="mesh-legend-dot" style={{ background: "#4f8ef7" }} />
                    This node
                </div>
                <div className="mesh-legend-item">
                    <div className="mesh-legend-dot" style={{ background: "#3dd68c" }} />
                    Verified peer
                </div>
                <div className="mesh-legend-item">
                    <div className="mesh-legend-dot" style={{ background: "#f0a040" }} />
                    Connecting
                </div>
            </div>

            <div className="mesh-controls">
                <button className="mesh-ctrl-btn" title="Reset view" onClick={() => { offsetRef.current = { x: 0, y: 0 } }}>⊹</button>
            </div>

            {peerCount === 0 && node && (
                <div className="mesh-empty" style={{ pointerEvents: "none" }}>
                    <h3>No peers connected</h3>
                    <p style={{ fontSize: 12, color: "var(--text-3)", marginTop: 4 }}>Your node is running. Go to Peers to connect.</p>
                </div>
            )}
        </div>
    )
}
