const API = '';

// --- State ---
let currentTab = 'signals';
let chartInstance = null;
let candleSeries = null;
let volumeSeries = null;
let selectedPair = null;
let selectedTimeframe = '5m';
let chartRefreshInterval = null;
let chartNeedsFit = true;

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    refreshAll();
    setInterval(refreshAll, 15000); // refresh toutes les 15s
});

async function refreshAll() {
    await Promise.all([
        fetchStatus(),
        fetchStats(),
        fetchSignals(),
        fetchTrades(),
        fetchBalance(),
    ]);
}

// --- API calls ---
async function fetchStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();
        const badge = document.getElementById('status-badge');
        if (data.status === 'running') {
            badge.textContent = 'en ligne';
            badge.className = 'badge badge-online';
        } else {
            badge.textContent = 'offline';
            badge.className = 'badge badge-offline';
        }
        document.getElementById('stat-active').textContent = data.scanner?.active_signals || 0;
    } catch {
        document.getElementById('status-badge').textContent = 'offline';
        document.getElementById('status-badge').className = 'badge badge-offline';
    }
}

async function fetchStats() {
    try {
        const res = await fetch(`${API}/api/stats`);
        const data = await res.json();
        document.getElementById('stat-trades').textContent = data.total_trades || 0;
        document.getElementById('stat-winrate').textContent = `${data.win_rate || 0}%`;
        const pnl = data.total_pnl_usd || 0;
        const pnlEl = document.getElementById('stat-pnl');
        pnlEl.textContent = `$${pnl.toFixed(2)}`;
        pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    } catch {}
}

async function fetchBalance() {
    try {
        const res = await fetch(`${API}/api/balance`);
        const data = await res.json();
        const el = document.getElementById('balance-display');
        if (data.total > 0) {
            el.textContent = `$${data.total.toFixed(2)}`;
        } else {
            el.textContent = 'Paper mode';
        }
    } catch {}
}

async function fetchSignals() {
    try {
        const res = await fetch(`${API}/api/signals?limit=30`);
        const data = await res.json();
        renderSignals(data.signals || []);
    } catch {
        document.getElementById('signals-list').innerHTML =
            '<div class="empty-state">Erreur de chargement</div>';
    }
}

async function fetchTrades() {
    try {
        const res = await fetch(`${API}/api/trades?limit=30`);
        const data = await res.json();
        renderTrades(data.trades || []);
    } catch {}
}

// --- Render ---
function renderSignals(signals) {
    const container = document.getElementById('signals-list');
    if (!signals.length) {
        container.innerHTML = '<div class="empty-state">Aucun signal pour le moment. Le scanner tourne...</div>';
        return;
    }

    container.innerHTML = signals.map(s => {
        const dirClass = s.direction === 'long' ? 'signal-long' :
                         s.direction === 'short' ? 'signal-short' : 'signal-notrade';
        const scoreClass = s.score >= 80 ? 'score-high' : s.score >= 65 ? 'score-mid' : 'score-low';
        const time = new Date(s.timestamp || s.created_at).toLocaleTimeString('fr-FR');
        const decimals = getDecimals(s.entry_price);

        let reasons = '';
        try {
            const arr = typeof s.reasons === 'string' ? JSON.parse(s.reasons) : (s.reasons || []);
            reasons = arr.map(r => `<div style="font-size:11px;color:var(--text-secondary)">• ${r}</div>`).join('');
        } catch {}

        return `
        <div class="signal-card">
            <div class="signal-header">
                <div style="display:flex;align-items:center;gap:8px">
                    <span class="signal-pair">${s.symbol}</span>
                    <span class="signal-mode">${s.mode}</span>
                </div>
                <div style="display:flex;align-items:center;gap:8px">
                    <span class="signal-time">${time}</span>
                    <span class="signal-direction ${dirClass}">${s.direction.toUpperCase()}</span>
                </div>
            </div>
            <div class="signal-body">
                <span class="label">Entree</span><span class="value">${s.entry_price?.toFixed(decimals) || '--'}</span>
                <span class="label">Stop</span><span class="value" style="color:var(--red)">${s.stop_loss?.toFixed(decimals) || '--'}</span>
                <span class="label">TP1</span><span class="value" style="color:var(--green)">${s.tp1?.toFixed(decimals) || '--'}</span>
                <span class="label">TP2</span><span class="value" style="color:var(--green)">${s.tp2?.toFixed(decimals) || '--'}</span>
                <span class="label">TP3</span><span class="value" style="color:var(--green)">${s.tp3?.toFixed(decimals) || '--'}</span>
                <span class="label">Levier</span><span class="value">${s.leverage || '--'}x</span>
            </div>
            ${reasons ? `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">${reasons}</div>` : ''}
            <div class="signal-score">
                <span style="font-size:12px;font-weight:600">${s.score}</span>
                <div class="score-bar"><div class="score-fill ${scoreClass}" style="width:${s.score}%"></div></div>
                <span style="font-size:12px">/100</span>
            </div>
        </div>`;
    }).join('');
}

function renderTrades(trades) {
    const container = document.getElementById('trades-list');
    if (!trades.length) {
        container.innerHTML = '<div class="empty-state">Aucun trade enregistre. Les trades apparaitront ici.</div>';
        return;
    }

    container.innerHTML = trades.map(t => {
        const pnlClass = (t.pnl_usd || 0) >= 0 ? 'trade-pnl-pos' : 'trade-pnl-neg';
        const time = new Date(t.entry_time || t.created_at).toLocaleString('fr-FR');
        return `
        <div class="trade-card">
            <div>
                <div style="font-weight:600">${t.symbol} ${t.direction?.toUpperCase()}</div>
                <div style="font-size:11px;color:var(--text-secondary)">${time} | ${t.mode}</div>
            </div>
            <div>
                <div class="${pnlClass}">$${(t.pnl_usd || 0).toFixed(2)}</div>
                <div style="font-size:11px;color:var(--text-secondary)">${t.result || 'pending'}</div>
            </div>
        </div>`;
    }).join('');
}

// --- Tabs ---
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[onclick="switchTab('${tab}')"]`).classList.add('active');
    document.getElementById(`tab-${tab}`).classList.add('active');

    if (tab === 'charts') {
        if (!chartInstance) initChart();
        fetchTickers();
        loadChart();
        if (!chartRefreshInterval) {
            chartRefreshInterval = setInterval(() => {
                if (currentTab === 'charts') { fetchTickers(); loadChart(); }
            }, 10000);
        }
    }
}

// --- Charts ---
function initChart() {
    const container = document.getElementById('chart-container');
    container.innerHTML = '';
    chartInstance = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 400,
        layout: {
            background: { color: '#161b22' },
            textColor: '#e6edf3',
        },
        grid: {
            vertLines: { color: '#30363d' },
            horzLines: { color: '#30363d' },
        },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: '#30363d' },
        timeScale: {
            borderColor: '#30363d',
            timeVisible: true,
            secondsVisible: false,
        },
    });

    candleSeries = chartInstance.addCandlestickSeries({
        upColor: '#3fb950',
        downColor: '#f85149',
        borderUpColor: '#3fb950',
        borderDownColor: '#f85149',
        wickUpColor: '#3fb950',
        wickDownColor: '#f85149',
    });

    volumeSeries = chartInstance.addHistogramSeries({
        color: '#58a6ff',
        priceFormat: { type: 'volume' },
        priceScaleId: '',
        scaleMargins: { top: 0.85, bottom: 0 },
    });

    window.addEventListener('resize', () => {
        if (chartInstance) chartInstance.applyOptions({ width: container.clientWidth });
    });
}

async function fetchTickers() {
    try {
        const res = await fetch(`${API}/api/tickers`);
        const data = await res.json();
        const tickers = data.tickers || [];
        const bar = document.getElementById('ticker-bar');
        const selector = document.getElementById('pair-selector');

        if (!selectedPair && tickers.length) {
            selectedPair = tickers[0].symbol;
        }

        bar.innerHTML = tickers.map(t => {
            const changeClass = t.change_24h_pct >= 0 ? 'up' : 'down';
            const changeSign = t.change_24h_pct >= 0 ? '+' : '';
            const active = t.symbol === selectedPair ? 'active' : '';
            const dec = getDecimals(t.price);
            return `
            <div class="ticker-item ${active}" onclick="selectPair('${t.symbol}')">
                <div class="ticker-name">${t.name}</div>
                <div class="ticker-price">${t.price?.toFixed(dec) || '--'}</div>
                <div class="ticker-change ${changeClass}">${changeSign}${t.change_24h_pct?.toFixed(2) || '0'}%</div>
            </div>`;
        }).join('');

        selector.innerHTML = tickers.map(t => {
            const active = t.symbol === selectedPair ? 'active' : '';
            return `<button class="pair-btn ${active}" onclick="selectPair('${t.symbol}')">${t.name}</button>`;
        }).join('');
    } catch (e) {
        console.error('Erreur tickers:', e);
    }
}

function selectPair(symbol) {
    selectedPair = symbol;
    chartNeedsFit = true;
    fetchTickers();
    loadChart();
}

function changeTimeframe(tf) {
    selectedTimeframe = tf;
    chartNeedsFit = true;
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.tf-btn[onclick="changeTimeframe('${tf}')"]`).classList.add('active');
    loadChart();
}

async function loadChart() {
    if (!selectedPair || !chartInstance) return;
    try {
        const sym = selectedPair.replace('/', '-');
        const res = await fetch(`${API}/api/ohlcv/${sym}?timeframe=${selectedTimeframe}&limit=300`);
        const data = await res.json();
        const candles = data.candles || [];
        if (!candles.length) return;

        candleSeries.setData(candles.map(c => ({
            time: c.time,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
        })));

        volumeSeries.setData(candles.map(c => ({
            time: c.time,
            value: c.volume,
            color: c.close >= c.open ? 'rgba(63,185,80,0.3)' : 'rgba(248,81,73,0.3)',
        })));

        if (chartNeedsFit) {
            chartInstance.timeScale().fitContent();
            chartNeedsFit = false;
        }
    } catch (e) {
        console.error('Erreur chart:', e);
    }
}

// --- Utils ---
function getDecimals(price) {
    if (!price) return 4;
    if (price >= 100) return 2;
    if (price >= 1) return 4;
    if (price >= 0.01) return 6;
    return 8;
}

// --- PWA ---
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}
