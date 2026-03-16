/**
 * components/Canvas.jsx
 * =====================
 * The spatial force-directed graph canvas. Renders this node at the center,
 * peers as satellite nodes, and animated edges between them.
 *
 * Built on D3 force simulation. Pan + zoom via d3-zoom. Drag nodes to reposition.
 * Clicking a node selects it and fires onSelectNode.
 */

import { useEffect, useRef, useCallback } from 'react'
import * as d3 from 'd3'
import { shortId, peerStatusColor } from '../lib/utils'

const STATUS_COLORS = {
    VERIFIED: '#00ff88',
    CONNECTED: '#00ff88',
    CONNECTING: '#4fc3f7',
    HANDSHAKING: '#4fc3f7',
    DISCOVERED: '#6b6d8a',
    DEGRADED: '#ff6b35',
    DISCONNECTED: '#3a3a5c',
    BANNED: '#ef4444',
}

const LOCAL_COLOR = '#00ff88'
const LOCAL_RADIUS = 28
const PEER_RADIUS = 18

export default function Canvas({ node, peers, agents, selectedId, onSelectNode }) {
    const svgRef = useRef(null)
    const simRef = useRef(null)
    const zoomRef = useRef(null)
    const nodesGRef = useRef(null)
    const linksGRef = useRef(null)
    const widthRef = useRef(0)
    const heightRef = useRef(0)

    // Build graph data from props
    const buildGraph = useCallback(() => {
        const peerList = Object.values(peers || {})
        const agentCount = Object.keys(agents || {}).length

        const nodes = [
            {
                id: node?.node_id ?? 'local',
                label: node?.alias || shortId(node?.node_id, 6, 4),
                type: 'local',
                state: node?.state,
                agentCount,
                fx: widthRef.current / 2,
                fy: heightRef.current / 2,
            },
            ...peerList.map(p => ({
                id: p.node_id,
                label: p.alias || shortId(p.node_id, 6, 4),
                type: 'peer',
                status: p.status,
                ping: p.last_ping_ms,
                addresses: p.addresses,
            })),
        ]

        const links = peerList
            .filter(p => ['CONNECTED', 'VERIFIED', 'HANDSHAKING', 'CONNECTING'].includes(p.status))
            .map(p => ({
                source: node?.node_id ?? 'local',
                target: p.node_id,
                status: p.status,
                ping: p.last_ping_ms,
            }))

        return { nodes, links }
    }, [node, peers, agents])

    // Initialize SVG + D3
    useEffect(() => {
        if (!svgRef.current) return

        const svg = d3.select(svgRef.current)
        const rect = svgRef.current.getBoundingClientRect()
        widthRef.current = rect.width
        heightRef.current = rect.height

        svg.selectAll('*').remove()

        // Defs — glow filters
        const defs = svg.append('defs')

        const glowAccent = defs.append('filter').attr('id', 'glow-accent').attr('x', '-50%').attr('y', '-50%').attr('width', '200%').attr('height', '200%')
        glowAccent.append('feGaussianBlur').attr('stdDeviation', '4').attr('result', 'blur')
        glowAccent.append('feMerge').selectAll('feMergeNode').data(['blur', 'SourceGraphic']).enter().append('feMergeNode').attr('in', d => d)

        const glowPeer = defs.append('filter').attr('id', 'glow-peer').attr('x', '-50%').attr('y', '-50%').attr('width', '200%').attr('height', '200%')
        glowPeer.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur')
        glowPeer.append('feMerge').selectAll('feMergeNode').data(['blur', 'SourceGraphic']).enter().append('feMergeNode').attr('in', d => d)

        // Arrow marker for directed edges
        defs.append('marker')
            .attr('id', 'arrow').attr('viewBox', '0 -4 8 8').attr('refX', 22).attr('refY', 0)
            .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
            .append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', '#3a3a5c')

        // Zoom container
        const g = svg.append('g').attr('class', 'zoom-container')
        zoomRef.current = g

        const zoom = d3.zoom()
            .scaleExtent([0.2, 4])
            .on('zoom', (event) => {
                g.attr('transform', event.transform)
            })

        svg.call(zoom)
            .call(zoom.transform, d3.zoomIdentity.translate(0, 0).scale(1))

        // Layer order: links behind nodes
        linksGRef.current = g.append('g').attr('class', 'links')
        nodesGRef.current = g.append('g').attr('class', 'nodes')

        return () => {
            simRef.current?.stop()
        }
    }, [])

    // Update simulation when data changes
    useEffect(() => {
        if (!nodesGRef.current || !linksGRef.current) return
        const W = widthRef.current
        const H = heightRef.current

        const { nodes, links } = buildGraph()

        // Keep existing positions for known nodes
        const existingPositions = {}
        if (simRef.current) {
            simRef.current.nodes().forEach(n => {
                existingPositions[n.id] = { x: n.x, y: n.y, vx: n.vx, vy: n.vy }
            })
            simRef.current.stop()
        }

        // Apply saved positions
        nodes.forEach(n => {
            if (existingPositions[n.id] && n.type !== 'local') {
                Object.assign(n, existingPositions[n.id])
            }
        })

        // D3 simulation
        const sim = d3.forceSimulation(nodes)
            .force('link', d3.forceLink(links).id(d => d.id).distance(130).strength(0.4))
            .force('charge', d3.forceManyBody().strength(-400))
            .force('collision', d3.forceCollide(PEER_RADIUS + 12))
            .force('center', d3.forceCenter(W / 2, H / 2).strength(0.05))
            .alphaDecay(0.03)

        simRef.current = sim

        // ── Links ────────────────────────────────────────────────────────────────
        const linkSel = linksGRef.current
            .selectAll('.link-group')
            .data(links, d => `${d.source.id ?? d.source}-${d.target.id ?? d.target}`)

        linkSel.exit().remove()

        const linkEnter = linkSel.enter().append('g').attr('class', 'link-group')
        linkEnter.append('line').attr('class', 'link-bg')
        linkEnter.append('line').attr('class', 'link-stream')

        const linkMerge = linkEnter.merge(linkSel)

        linkMerge.select('.link-bg')
            .attr('stroke', d => STATUS_COLORS[d.status] ?? '#3a3a5c')
            .attr('stroke-width', 1.5)
            .attr('stroke-opacity', 0.25)
            .attr('marker-end', 'url(#arrow)')

        linkMerge.select('.link-stream')
            .attr('stroke', d => STATUS_COLORS[d.status] ?? '#3a3a5c')
            .attr('stroke-width', 1)
            .attr('stroke-opacity', d => ['VERIFIED', 'CONNECTED'].includes(d.status) ? 0.7 : 0.2)
            .attr('stroke-dasharray', '6 4')
            .style('animation', d =>
                ['VERIFIED', 'CONNECTED'].includes(d.status)
                    ? 'stream 1.5s linear infinite'
                    : 'none'
            )

        // ── Nodes ────────────────────────────────────────────────────────────────
        const nodeSel = nodesGRef.current
            .selectAll('.node')
            .data(nodes, d => d.id)

        nodeSel.exit()
            .transition().duration(300).attr('opacity', 0).remove()

        const nodeEnter = nodeSel.enter().append('g')
            .attr('class', 'node')
            .attr('cursor', 'pointer')
            .attr('opacity', 0)
            .call(
                d3.drag()
                    .on('start', (event, d) => {
                        if (!event.active) sim.alphaTarget(0.3).restart()
                        if (d.type !== 'local') { d.fx = d.x; d.fy = d.y }
                    })
                    .on('drag', (event, d) => {
                        if (d.type !== 'local') { d.fx = event.x; d.fy = event.y }
                    })
                    .on('end', (event, d) => {
                        if (!event.active) sim.alphaTarget(0)
                        if (d.type !== 'local') { d.fx = null; d.fy = null }
                    })
            )
            .on('click', (event, d) => {
                event.stopPropagation()
                onSelectNode?.(d.id === (node?.node_id ?? 'local') ? null : d)
            })

        // Pulse ring (local node only)
        nodeEnter.filter(d => d.type === 'local').append('circle')
            .attr('class', 'pulse-ring')
            .attr('r', LOCAL_RADIUS + 8)
            .attr('fill', 'none')
            .attr('stroke', LOCAL_COLOR)
            .attr('stroke-width', 1)
            .attr('opacity', 0)

        // Outer hex ring
        nodeEnter.append('circle')
            .attr('class', 'node-ring')
            .attr('fill', 'none')
            .attr('stroke-width', 1.5)

        // Inner fill
        nodeEnter.append('circle')
            .attr('class', 'node-body')

        // Agent count badge (local node)
        nodeEnter.filter(d => d.type === 'local' && d.agentCount > 0)
            .append('text')
            .attr('class', 'agent-badge')
            .attr('text-anchor', 'middle')
            .attr('dominant-baseline', 'central')
            .attr('font-family', 'JetBrains Mono, monospace')
            .attr('font-size', 9)
            .attr('fill', '#050508')
            .attr('y', -LOCAL_RADIUS - 2)

        // Label
        nodeEnter.append('text')
            .attr('class', 'node-label')
            .attr('text-anchor', 'middle')
            .attr('font-family', 'JetBrains Mono, monospace')
            .attr('font-size', 10)
            .attr('y', d => d.type === 'local' ? LOCAL_RADIUS + 16 : PEER_RADIUS + 14)

        // Ping label
        nodeEnter.filter(d => d.type === 'peer').append('text')
            .attr('class', 'node-ping')
            .attr('text-anchor', 'middle')
            .attr('font-family', 'JetBrains Mono, monospace')
            .attr('font-size', 9)
            .attr('y', PEER_RADIUS + 26)

        nodeEnter.transition().duration(400).attr('opacity', 1)

        const nodeMerge = nodeEnter.merge(nodeSel)

        // Update ring
        nodeMerge.select('.node-ring')
            .attr('r', d => d.type === 'local' ? LOCAL_RADIUS + 4 : PEER_RADIUS + 3)
            .attr('stroke', d => d.type === 'local' ? LOCAL_COLOR : (STATUS_COLORS[d.status] ?? '#3a3a5c'))
            .attr('stroke-opacity', d => d.id === selectedId ? 1 : 0.5)
            .attr('filter', d => d.type === 'local' ? 'url(#glow-accent)' : (d.status === 'VERIFIED' || d.status === 'CONNECTED') ? 'url(#glow-peer)' : null)

        // Update body
        nodeMerge.select('.node-body')
            .attr('r', d => d.type === 'local' ? LOCAL_RADIUS : PEER_RADIUS)
            .attr('fill', d => d.type === 'local' ? 'rgba(0,255,136,0.12)' : `${STATUS_COLORS[d.status] ?? '#3a3a5c'}22`)
            .attr('stroke', d => d.type === 'local' ? LOCAL_COLOR : (STATUS_COLORS[d.status] ?? '#3a3a5c'))
            .attr('stroke-width', 1)

        // Update selection ring
        nodeMerge.select('.node-ring')
            .attr('stroke-width', d => d.id === selectedId ? 2.5 : 1.5)

        // Update labels
        nodeMerge.select('.node-label')
            .text(d => d.label)
            .attr('fill', d => d.type === 'local' ? LOCAL_COLOR : (STATUS_COLORS[d.status] ?? '#6b6d8a'))

        nodeMerge.select('.node-ping')
            .text(d => d.ping != null ? `${d.ping}ms` : '')
            .attr('fill', d => d.ping < 50 ? '#00ff88' : d.ping < 200 ? '#4fc3f7' : '#ff6b35')

        // Pulse ring animation (local node)
        const pulseRings = nodesGRef.current.selectAll('.pulse-ring')
        function animatePulse() {
            pulseRings
                .attr('r', LOCAL_RADIUS + 8)
                .attr('opacity', 0.5)
                .transition().duration(2000).ease(d3.easeExpOut)
                .attr('r', LOCAL_RADIUS + 32)
                .attr('opacity', 0)
                .on('end', animatePulse)
        }
        animatePulse()

        // Click on blank canvas to deselect
        d3.select(svgRef.current).on('click', () => onSelectNode?.(null))

        // Tick
        sim.on('tick', () => {
            linkMerge.select('.link-bg')
                .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
                .attr('x2', d => d.target.x).attr('y2', d => d.target.y)

            linkMerge.select('.link-stream')
                .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
                .attr('x2', d => d.target.x).attr('y2', d => d.target.y)

            nodeMerge.attr('transform', d => `translate(${d.x},${d.y})`)
        })

        return () => sim.stop()
    }, [buildGraph, selectedId, node, peers, agents, onSelectNode])

    return (
        <div className="relative w-full h-full grid-bg">
            <svg
                ref={svgRef}
                className="w-full h-full"
                style={{ background: 'transparent' }}
            />
        </div>
    )
}
