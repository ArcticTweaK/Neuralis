/** @type {import('tailwindcss').Config} */
export default {
    content: ['./index.html', './src/**/*.{js,jsx}'],
    theme: {
        extend: {
            fontFamily: {
                mono: ['"JetBrains Mono"', '"Fira Code"', 'monospace'],
                display: ['"Share Tech Mono"', 'monospace'],
            },
            colors: {
                void: '#050508',
                grid: '#0d0d14',
                panel: '#0a0a12',
                border: '#1a1a2e',
                accent: '#00ff88',
                'accent-dim': '#00cc6a',
                'accent-glow': 'rgba(0,255,136,0.15)',
                warn: '#ff6b35',
                'warn-dim': '#cc5520',
                info: '#4fc3f7',
                'info-dim': '#2196f3',
                peer: '#a78bfa',
                'peer-dim': '#7c3aed',
                muted: '#3a3a5c',
                text: '#c8cad8',
                'text-dim': '#6b6d8a',
                'text-bright': '#eef0ff',
            },
            boxShadow: {
                'accent-glow': '0 0 20px rgba(0,255,136,0.3)',
                'peer-glow': '0 0 20px rgba(167,139,250,0.3)',
                'warn-glow': '0 0 20px rgba(255,107,53,0.3)',
                'panel': '0 4px 32px rgba(0,0,0,0.6)',
            },
            animation: {
                'pulse-slow': 'pulse 3s cubic-bezier(0.4,0,0.6,1) infinite',
                'spin-slow': 'spin 8s linear infinite',
                'fade-in': 'fadeIn 0.3s ease-out',
                'slide-up': 'slideUp 0.25s ease-out',
            },
            keyframes: {
                fadeIn: { from: { opacity: '0' }, to: { opacity: '1' } },
                slideUp: { from: { opacity: '0', transform: 'translateY(8px)' }, to: { opacity: '1', transform: 'translateY(0)' } },
            },
        },
    },
    plugins: [],
}
