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
let lastCandles = [];
let vpCanvas = null;
let showFVG = true;
let showVolume = true;
let preloadedCandles = null;
let mexcWs = null;
let wsReconnectTimer = null;

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    refreshAll();
    // Pre-charger la 1ere paire + ses bougies pour affichage instantane
    fetch(`${API}/api/pairs`).then(r => r.json()).then(d => {
        if (d.pairs && d.pairs.length) {
            selectedPair = d.pairs[0];
            const sym = selectedPair.replace('/', '-');
            fetch(`${API}/api/ohlcv/${sym}?timeframe=${selectedTimeframe}&limit=300`)
                .then(r => r.json())
                .then(data => { preloadedCandles = data.candles || []; });
        }
    });
    fetchTickers();
    setInterval(refreshAll, 15000);
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
    if (currentTab === 'charts' && tab !== 'charts') disconnectMexcWs();
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[onclick="switchTab('${tab}')"]`).classList.add('active');
    document.getElementById(`tab-${tab}`).classList.add('active');

    if (tab === 'charts') {
        if (!chartInstance) initChart();
        // Charger chart immediatement sans attendre les tickers
        if (!selectedPair) {
            // Utiliser /api/pairs (rapide) pour avoir la 1ere paire
            fetch(`${API}/api/pairs`).then(r => r.json()).then(d => {
                if (d.pairs && d.pairs.length && !selectedPair) {
                    selectedPair = d.pairs[0];
                    loadChart();
                }
            });
        } else {
            loadChart();
        }
        fetchTickers(); // en parallele
        if (!chartRefreshInterval) {
            // Tickers refresh (prix dans les badges)
            chartRefreshInterval = setInterval(() => {
                if (currentTab === 'charts') fetchTickers();
            }, 15000);
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
        priceFormat: { type: 'volume' },
        priceScaleId: '',
        scaleMargins: { top: 0.8, bottom: 0 },
    });

    // Canvas overlay pour FVG (Fair Value Gaps)
    vpCanvas = document.createElement('canvas');
    vpCanvas.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:2;';
    container.style.position = 'relative';
    container.appendChild(vpCanvas);

    // Redessiner les FVG quand on scroll/zoom
    chartInstance.timeScale().subscribeVisibleLogicalRangeChange(() => drawFVG());

    // Empecher le zoom page quand on scroll/pinch sur le chart
    container.addEventListener('wheel', (e) => { e.preventDefault(); }, { passive: false });
    container.addEventListener('touchstart', (e) => { if (e.touches.length > 1) e.preventDefault(); }, { passive: false });

    window.addEventListener('resize', () => {
        if (chartInstance) {
            chartInstance.applyOptions({ width: container.clientWidth });
            drawFVG();
        }
    });
}

// --- Fair Value Gaps (FVG) ---
function detectFVG(candles) {
    const fvgs = [];
    for (let i = 2; i < candles.length; i++) {
        const c1 = candles[i - 2]; // bougie 1
        const c2 = candles[i - 1]; // bougie 2 (milieu)
        const c3 = candles[i];     // bougie 3

        // FVG Haussier: low de bougie 3 > high de bougie 1
        if (c3.low > c1.high) {
            fvgs.push({
                type: 'bull',
                top: c3.low,
                bottom: c1.high,
                timeStart: c2.time,
                timeEnd: candles[candles.length - 1].time,
                filled: false,
            });
        }

        // FVG Baissier: high de bougie 3 < low de bougie 1
        if (c3.high < c1.low) {
            fvgs.push({
                type: 'bear',
                top: c1.low,
                bottom: c3.high,
                timeStart: c2.time,
                timeEnd: candles[candles.length - 1].time,
                filled: false,
            });
        }
    }

    // Verifier si le FVG a ete comble (filled)
    fvgs.forEach(fvg => {
        for (let i = 0; i < candles.length; i++) {
            if (candles[i].time <= fvg.timeStart) continue;
            if (fvg.type === 'bull' && candles[i].low <= fvg.bottom) {
                fvg.filled = true;
                fvg.timeEnd = candles[i].time;
                break;
            }
            if (fvg.type === 'bear' && candles[i].high >= fvg.top) {
                fvg.filled = true;
                fvg.timeEnd = candles[i].time;
                break;
            }
        }
    });

    return fvgs;
}

function toggleFVG() {
    showFVG = !showFVG;
    const btn = document.getElementById('fvg-toggle');
    btn.textContent = showFVG ? 'FVG ON' : 'FVG OFF';
    btn.classList.toggle('active', showFVG);
    drawFVG();
}

function toggleVolume() {
    showVolume = !showVolume;
    const btn = document.getElementById('vol-toggle');
    btn.textContent = showVolume ? 'VOL ON' : 'VOL OFF';
    btn.classList.toggle('active', showVolume);
    if (volumeSeries) {
        volumeSeries.applyOptions({ visible: showVolume });
    }
}

function drawFVG() {
    if (!vpCanvas || !chartInstance || !candleSeries) return;
    const container = document.getElementById('chart-container');
    vpCanvas.width = container.clientWidth;
    vpCanvas.height = container.clientHeight;
    const ctx = vpCanvas.getContext('2d');
    ctx.clearRect(0, 0, vpCanvas.width, vpCanvas.height);
    if (!showFVG || !lastCandles.length) return;

    const fvgs = detectFVG(lastCandles);
    const timeScale = chartInstance.timeScale();

    fvgs.forEach(fvg => {
        const y1 = candleSeries.priceToCoordinate(fvg.top);
        const y2 = candleSeries.priceToCoordinate(fvg.bottom);
        const x1 = timeScale.timeToCoordinate(fvg.timeStart);
        const x2 = fvg.filled
            ? timeScale.timeToCoordinate(fvg.timeEnd)
            : vpCanvas.width;

        if (y1 === null || y2 === null || x1 === null) return;

        const y = Math.min(y1, y2);
        const h = Math.abs(y2 - y1);
        const x = x1;
        const w = (x2 !== null ? x2 : vpCanvas.width) - x1;

        if (w <= 0 || h <= 0) return;

        const alpha = fvg.filled ? 0.12 : 0.3;

        if (fvg.type === 'bull') {
            ctx.fillStyle = `rgba(38, 166, 154, ${alpha})`;
        } else {
            ctx.fillStyle = `rgba(239, 83, 80, ${alpha})`;
        }
        ctx.fillRect(x, y, w, h);

        // Bordure fine
        if (!fvg.filled) {
            ctx.strokeStyle = fvg.type === 'bull'
                ? 'rgba(38, 166, 154, 0.5)'
                : 'rgba(239, 83, 80, 0.5)';
            ctx.lineWidth = 1;
            ctx.strokeRect(x, y, w, h);
        }
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
        let candles;
        if (preloadedCandles && preloadedCandles.length) {
            candles = preloadedCandles;
            preloadedCandles = null; // utiliser une seule fois
        } else {
            const sym = selectedPair.replace('/', '-');
            const res = await fetch(`${API}/api/ohlcv/${sym}?timeframe=${selectedTimeframe}&limit=300`);
            const data = await res.json();
            candles = data.candles || [];
        }
        if (!candles.length) return;

        // Ajuster UTC -> heure locale
        const tzOffset = new Date().getTimezoneOffset() * -60;
        const adjusted = candles.map(c => ({
            time: c.time + tzOffset,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
            volume: c.volume,
        }));

        candleSeries.setData(adjusted.map(c => ({
            time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
        })));

        volumeSeries.setData(adjusted.map(c => ({
            time: c.time,
            value: c.volume,
            color: c.close >= c.open ? 'rgba(63,185,80,0.5)' : 'rgba(248,81,73,0.5)',
        })));

        // Stocker les candles ajustees pour les FVG
        lastCandles = adjusted;

        if (chartNeedsFit) {
            chartInstance.timeScale().fitContent();
            chartNeedsFit = false;
        }

        // Dessiner les Fair Value Gaps
        setTimeout(() => drawFVG(), 50);

        // Connecter WebSocket pour updates temps reel
        connectMexcWs();
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

// --- WebSocket temps reel via serveur ---
function connectMexcWs() {
    disconnectMexcWs();
    if (!selectedPair) return;

    const sym = selectedPair.replace('/', '-');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${location.host}/ws/kline/${sym}/${selectedTimeframe}`;

    mexcWs = new WebSocket(wsUrl);

    mexcWs.onopen = () => {
        console.log('WS connecte:', sym, selectedTimeframe);
    };

    mexcWs.onmessage = (evt) => {
        try {
            const data = JSON.parse(evt.data);
            updateCandleRealtime(data);
        } catch {}
    };

    mexcWs.onclose = () => {
        if (currentTab === 'charts') {
            wsReconnectTimer = setTimeout(connectMexcWs, 3000);
        }
    };

    mexcWs.onerror = () => { if (mexcWs) mexcWs.close(); };
}

function updateCandleRealtime(data) {
    if (!candleSeries || !volumeSeries || !lastCandles.length) return;

    const tzOffset = new Date().getTimezoneOffset() * -60;
    const time = data.t + tzOffset;
    const candle = {
        time: time,
        open: data.o,
        high: data.h,
        low: data.l,
        close: data.c,
        volume: data.q || data.a || 0,
    };

    // Mettre a jour la derniere bougie ou en ajouter une nouvelle
    const last = lastCandles[lastCandles.length - 1];
    if (last && last.time === candle.time) {
        // Meme bougie -> mise a jour
        last.open = candle.open;
        last.high = candle.high;
        last.low = candle.low;
        last.close = candle.close;
        last.volume = candle.volume;
    } else if (!last || candle.time > last.time) {
        // Nouvelle bougie
        lastCandles.push(candle);
    }

    candleSeries.update({
        time: candle.time, open: candle.open, high: candle.high,
        low: candle.low, close: candle.close,
    });

    volumeSeries.update({
        time: candle.time,
        value: candle.volume,
        color: candle.close >= candle.open ? 'rgba(63,185,80,0.5)' : 'rgba(248,81,73,0.5)',
    });
}

function disconnectMexcWs() {
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    if (mexcWs) {
        if (mexcWs._pingInterval) clearInterval(mexcWs._pingInterval);
        mexcWs.onclose = null;
        mexcWs.close();
        mexcWs = null;
    }
}

// --- PWA ---
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}
