const API = '';

// --- State ---
let currentTab = 'v1';
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
let preloadedPair = null;
let mexcWs = null;
let wsReconnectTimer = null;
let compareChart = null;
let compareV1Series = null;
let compareV2Series = null;
let compareFtSeries = null;
let comparePeriod = 0;

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    refreshAll();
    fetch(`${API}/api/pairs`).then(r => r.json()).then(d => {
        if (d.pairs && d.pairs.length) {
            selectedPair = d.pairs[0];
            preloadedPair = d.pairs[0];
            const sym = selectedPair.replace('/', '-');
            fetch(`${API}/api/ohlcv/${sym}?timeframe=${selectedTimeframe}&limit=300`)
                .then(r => r.json())
                .then(data => { preloadedCandles = data.candles || []; });
        }
    });
    fetchTickers();
    setInterval(refreshAll, 15000);
    setInterval(updateCountdowns, 1000);
});

async function refreshAll() {
    await Promise.all([
        fetchStatus(),
        fetchBotData('V1'),
        fetchBotData('V2'),
        fetchTrades(),
        fetchLivePositions(),
        fetchFreqtradeData(),
    ]);
}

// --- API calls ---
async function fetchStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();
        ['V1', 'V2'].forEach(ver => {
            const v = ver.toLowerCase();
            const botStatus = data.scanners?.[ver];
            const badge = document.getElementById(`${v}-status`);
            if (botStatus?.running) {
                badge.textContent = 'en ligne';
                badge.className = 'badge badge-online';
            } else {
                badge.textContent = 'offline';
                badge.className = 'badge badge-offline';
            }
            document.getElementById(`${v}-active`).textContent = botStatus?.active_signals || 0;
        });
    } catch {
        ['v1', 'v2'].forEach(v => {
            const badge = document.getElementById(`${v}-status`);
            badge.textContent = 'offline';
            badge.className = 'badge badge-offline';
        });
    }
}

async function fetchBotData(version) {
    const v = version.toLowerCase();
    try {
        const [portfolioRes, signalsRes] = await Promise.all([
            fetch(`${API}/api/paper/portfolio?bot_version=${version}`),
            fetch(`${API}/api/signals?limit=30&bot_version=${version}`),
        ]);
        const portfolio = await portfolioRes.json();
        const signals = await signalsRes.json();
        updateBotSidebar(v, portfolio);
        renderSignals(signals.signals || [], `${v}-signals-list`, version);
    } catch (e) {
        console.error(`fetchBotData(${version}):`, e);
    }
}

function updateBotSidebar(v, data) {
    const trades = data.total_trades || 0;
    const wins = data.wins || 0;
    const losses = data.losses || 0;
    const winRate = trades > 0 ? Math.round((wins / trades) * 100) : 0;
    const pnl = data.total_pnl || 0;
    const balance = data.current_balance || 0;

    document.getElementById(`${v}-trades`).textContent = trades;
    document.getElementById(`${v}-wins`).textContent = wins;
    document.getElementById(`${v}-losses`).textContent = losses;
    document.getElementById(`${v}-winrate`).textContent = `${winRate}%`;

    const pnlEl = document.getElementById(`${v}-pnl`);
    pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
    pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';

    const balEl = document.getElementById(`${v}-balance`);
    balEl.textContent = `$${balance.toFixed(2)}`;
    balEl.style.color = balance >= (data.initial_balance || 100) ? 'var(--green)' : 'var(--red)';
}

async function fetchTrades() {
    try {
        const [v1Res, v2Res] = await Promise.all([
            fetch(`${API}/api/trades?limit=30&bot_version=V1`),
            fetch(`${API}/api/trades?limit=30&bot_version=V2`),
        ]);
        const v1 = await v1Res.json();
        const v2 = await v2Res.json();
        const allTrades = [
            ...(v1.trades || []).map(t => ({...t, bot_version: t.bot_version || 'V1'})),
            ...(v2.trades || []).map(t => ({...t, bot_version: t.bot_version || 'V2'})),
        ];
        allTrades.sort((a, b) => new Date(b.entry_time || b.created_at) - new Date(a.entry_time || a.created_at));
        renderTrades(allTrades);
    } catch {}
}

// --- Render ---
function renderSignals(signals, containerId, botVersion) {
    containerId = containerId || 'v2-signals-list';
    botVersion = botVersion || 'V2';
    const container = document.getElementById(containerId);
    if (!signals.length) {
        container.innerHTML = '<div class="empty-state">Aucun signal pour le moment. Le scanner tourne...</div>';
        return;
    }

    // Filtrer : ne garder que les signaux encore jouables (< 20s et pas deja executes)
    signals = signals.filter(s => {
        const status = (s.status || '').toLowerCase();
        if (['executed', 'skipped', 'error'].includes(status)) return false;
        const ts = s.timestamp || s.created_at;
        const signalTime = new Date(ts.endsWith('Z') ? ts : ts + 'Z').getTime();
        const age = (Date.now() - signalTime) / 1000;
        return age <= 20;
    });

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

        // Boutons d'execution (seulement si pas deja execute et < 20s)
        const status = (s.status || '').toLowerCase();
        const ts = s.timestamp || s.created_at;
        const signalTime = new Date(ts.endsWith('Z') ? ts : ts + 'Z').getTime();
        const ageSeconds = Math.floor((Date.now() - signalTime) / 1000);
        const EXPIRE_SEC = 20;
        const isExpired = ageSeconds > EXPIRE_SEC;
        const canExec = !['executed', 'skipped', 'error', 'expired'].includes(status) && s.direction !== 'none' && !isExpired;
        const remaining = Math.max(0, EXPIRE_SEC - ageSeconds);
        let actionsHtml = '';
        if (canExec && s.id) {
            const sid = s.id;
            actionsHtml = `
            <div class="signal-actions">
                <button class="btn-amount" onclick="openExecModal(${sid},'market',5)">5$</button>
                <button class="btn-amount" onclick="openExecModal(${sid},'market',10)">10$</button>
                <button class="btn-amount" onclick="openExecModal(${sid},'market',25)">25$</button>
                <button class="btn-custom" onclick="openExecModal(${sid},'market',0)">...$</button>
                <span class="signal-countdown" id="countdown-${sid}">${remaining}s</span>
            </div>`;
        } else if (isExpired && !['executed', 'skipped', 'error'].includes(status)) {
            actionsHtml = '<div class="signal-actions"><span class="signal-status status-expired">Expire</span></div>';
        } else if (status === 'executed') {
            actionsHtml = '<div class="signal-actions"><span class="signal-status status-executed">&#x2705; Execute</span></div>';
        } else if (status === 'skipped') {
            actionsHtml = '<div class="signal-actions"><span class="signal-status status-skipped">Ignore</span></div>';
        } else if (status === 'error') {
            actionsHtml = '<div class="signal-actions"><span class="signal-status status-error">Erreur</span></div>';
        }

        const isTest = status === 'test';
        const testBadge = isTest ? '<span class="signal-test">SIMULATION</span>' : '';
        const expiredClass = (isExpired && !['executed'].includes(status)) ? 'signal-card-expired' : '';

        const vBadge = botVersion === 'V1'
            ? '<span class="v1-badge" style="font-size:9px;padding:1px 5px">V1</span>'
            : '<span class="v2-badge" style="font-size:9px;padding:1px 5px">V2</span>';

        return `
        <div class="signal-card ${isTest ? 'signal-card-test' : ''} ${expiredClass}" id="signal-card-${s.id || 0}" data-status="${status}" data-bot-version="${botVersion}">
            <div class="signal-header">
                <div style="display:flex;align-items:center;gap:8px">
                    ${vBadge}
                    <span class="signal-pair">${s.symbol}</span>
                    <span class="signal-mode">${s.mode}</span>
                    ${testBadge}
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
            ${actionsHtml}
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
        const bv = t.bot_version || 'V2';
        const badgeClass = bv === 'V1' ? 'v1-badge' : 'v2-badge';
        return `
        <div class="trade-card">
            <div>
                <div style="font-weight:600;display:flex;align-items:center;gap:6px">
                    <span class="${badgeClass}" style="font-size:9px;padding:1px 5px">${bv}</span>
                    ${t.symbol} ${t.direction?.toUpperCase()}
                </div>
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
        if (!selectedPair) {
            fetch(`${API}/api/pairs`).then(r => r.json()).then(d => {
                if (d.pairs && d.pairs.length && !selectedPair) {
                    selectedPair = d.pairs[0];
                    loadChart();
                }
            });
        } else {
            loadChart();
        }
        fetchTickers();
        if (!chartRefreshInterval) {
            chartRefreshInterval = setInterval(() => {
                if (currentTab === 'charts') fetchTickers();
            }, 15000);
        }
    }
    if (tab === 'compare') loadCompareData();
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
        priceScaleId: 'right',
    });
    candleSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.05, bottom: 0.25 },
    });

    volumeSeries = chartInstance.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
        scaleMargins: { top: 0.8, bottom: 0 },
    });
    chartInstance.priceScale('volume').applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
        drawTicks: false,
        borderVisible: false,
        visible: false,
    });

    // Canvas overlay pour FVG (Fair Value Gaps)
    vpCanvas = document.createElement('canvas');
    vpCanvas.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:2;';
    container.style.position = 'relative';
    container.appendChild(vpCanvas);

    // Boucle rAF continue pour redessiner FVG en temps reel (suit zoom/drag prix)
    function fvgLoop() {
        if (chartInstance && showFVG && currentTab === 'charts') drawFVG();
        requestAnimationFrame(fvgLoop);
    }
    requestAnimationFrame(fvgLoop);

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
    lastMainPeriod = 0;
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.tf-btn[onclick="changeTimeframe('${tf}')"]`).classList.add('active');
    loadChart();
}

async function loadChart() {
    if (!selectedPair || !chartInstance) return;
    try {
        let candles;
        if (preloadedCandles && preloadedCandles.length && preloadedPair === selectedPair && selectedTimeframe === '5m') {
            candles = preloadedCandles;
            preloadedCandles = null;
            preloadedPair = null;
        } else {
            const sym = selectedPair.replace('/', '-');
            const res = await fetch(`${API}/api/ohlcv/${sym}?timeframe=${selectedTimeframe}&limit=300`);
            const data = await res.json();
            candles = data.candles || [];
        }
        if (!candles.length) return;

        // Adapter la precision selon le prix (+2 decimales)
        const price = candles[candles.length - 1].close;
        let precision, minMove;
        if (price >= 100)       { precision = 2; minMove = 0.01; }
        else if (price >= 1)    { precision = 4; minMove = 0.0001; }
        else if (price >= 0.01) { precision = 4; minMove = 0.0001; }
        else                    { precision = 8; minMove = 0.00000001; }

        candleSeries.applyOptions({
            priceFormat: { type: 'price', precision, minMove },
        });

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

// --- Refresh chart sans reset zoom (pour countdown new candle) ---
async function refreshChartCandles() {
    if (!selectedPair || !chartInstance || !candleSeries) return;
    try {
        const savedRange = chartInstance.timeScale().getVisibleLogicalRange();

        const sym = selectedPair.replace('/', '-');
        const res = await fetch(`${API}/api/ohlcv/${sym}?timeframe=${selectedTimeframe}&limit=10`);
        const data = await res.json();
        const candles = data.candles || [];
        if (!candles.length) return;

        const tzOffset = new Date().getTimezoneOffset() * -60;
        const newCandles = candles.map(c => ({
            time: c.time + tzOffset,
            open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume,
        }));

        newCandles.forEach(c => {
            candleSeries.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
            volumeSeries.update({ time: c.time, value: c.volume, color: c.close >= c.open ? 'rgba(63,185,80,0.5)' : 'rgba(248,81,73,0.5)' });
        });

        newCandles.forEach(nc => {
            const idx = lastCandles.findIndex(lc => lc.time === nc.time);
            if (idx >= 0) {
                lastCandles[idx] = nc;
            } else {
                lastCandles.push(nc);
            }
        });

        // Restaurer le zoom exact
        if (savedRange) {
            chartInstance.timeScale().setVisibleLogicalRange(savedRange);
        }
    } catch (e) {
        console.error('refreshChartCandles error:', e);
    }
}

async function refreshPopupChartCandles() {
    if (!popupPair || !popupChart || !popupCandleSeries) return;
    try {
        const savedRange = popupChart.timeScale().getVisibleLogicalRange();

        const sym = popupPair.replace('/', '-');
        const res = await fetch(`${API}/api/ohlcv/${sym}?timeframe=${popupTimeframe}&limit=10`);
        const data = await res.json();
        const candles = data.candles || [];
        if (!candles.length) return;

        const tzOffset = new Date().getTimezoneOffset() * -60;
        const newCandles = candles.map(c => ({
            time: c.time + tzOffset,
            open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume,
        }));

        newCandles.forEach(c => {
            popupCandleSeries.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
            popupVolumeSeries.update({ time: c.time, value: c.volume, color: c.close >= c.open ? 'rgba(63,185,80,0.5)' : 'rgba(248,81,73,0.5)' });
        });

        newCandles.forEach(nc => {
            const idx = popupLastCandles.findIndex(lc => lc.time === nc.time);
            if (idx >= 0) {
                popupLastCandles[idx] = nc;
            } else {
                popupLastCandles.push(nc);
            }
        });

        if (savedRange) {
            popupChart.timeScale().setVisibleLogicalRange(savedRange);
        }
    } catch (e) {
        console.error('refreshPopupChartCandles error:', e);
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
    const isNewCandle = !last || candle.time > last.time;

    // Sauvegarder le zoom avant ajout nouvelle bougie (evite le dezoom auto)
    let savedRange = null;
    if (isNewCandle && chartInstance) {
        savedRange = chartInstance.timeScale().getVisibleLogicalRange();
    }

    if (last && last.time === candle.time) {
        last.open = candle.open;
        last.high = candle.high;
        last.low = candle.low;
        last.close = candle.close;
        last.volume = candle.volume;
    } else if (isNewCandle) {
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

    // Restaurer le zoom (decale de 1 bar pour voir la nouvelle bougie)
    if (savedRange) {
        chartInstance.timeScale().setVisibleLogicalRange({
            from: savedRange.from + 1,
            to: savedRange.to + 1,
        });
    }
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

// --- Execution depuis le dashboard ---
let pendingExec = null;

async function openExecModal(signalId, orderType, margin) {
    const card = document.getElementById(`signal-card-${signalId}`);
    if (!card) return;

    const botVersion = card.dataset.botVersion || 'V2';
    const symbol = card.querySelector('.signal-pair')?.textContent || '?';
    const direction = card.querySelector('.signal-direction')?.textContent || '?';
    const leverage = card.querySelector('.signal-body .value:last-child')?.textContent || '10x';
    const lev = parseInt(leverage) || 10;
    const needsInput = margin === 0;

    let paperBalance = 0;
    try {
        const pRes = await fetch(`${API}/api/paper/portfolio?bot_version=${botVersion}`);
        const pData = await pRes.json();
        paperBalance = (pData.current_balance || 0) - (pData.reserved_margin || 0);
    } catch {}

    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');
    const confirmBtn = document.getElementById('modal-confirm');

    title.textContent = `PAPER ${direction} ${symbol}`;

    let inputHtml = '';
    if (needsInput) {
        inputHtml = `<input type="number" id="modal-margin" placeholder="Montant en $ (ex: 15)" min="1" step="1" autofocus>`;
    }

    const displayMargin = needsInput ? '...' : `${margin}$`;
    const displayPosition = needsInput ? '...' : `${margin * lev}$`;

    body.innerHTML = `
        <div style="background:rgba(88,166,255,0.12);border:1px solid rgba(88,166,255,0.3);border-radius:6px;padding:8px;margin-bottom:12px;font-size:12px;color:var(--blue);text-align:center;font-weight:600">Solde dispo: ${paperBalance.toFixed(2)}$</div>
        <div class="row"><span class="lbl">Direction</span><span class="val" style="color:${direction==='LONG'?'var(--green)':'var(--red)'}">${direction}</span></div>
        <div class="row"><span class="lbl">Levier</span><span class="val">${lev}x</span></div>
        <div class="row"><span class="lbl">Marge</span><span class="val" id="modal-margin-display">${displayMargin}</span></div>
        <div class="row"><span class="lbl">Position</span><span class="val" id="modal-position-display">${displayPosition}</span></div>
        ${inputHtml}
    `;

    confirmBtn.textContent = 'Confirmer';
    confirmBtn.className = 'btn-exec';
    confirmBtn.disabled = false;
    confirmBtn.style.background = '';

    pendingExec = { signal_id: signalId, order_type: 'market', margin: margin, lev: lev };
    document.getElementById('exec-modal').style.display = 'flex';

    if (needsInput) {
        const input = document.getElementById('modal-margin');
        input.focus();
        input.addEventListener('input', () => {
            const val = parseFloat(input.value) || 0;
            document.getElementById('modal-margin-display').textContent = val > 0 ? `${val}$` : '...';
            document.getElementById('modal-position-display').textContent = val > 0 ? `${val * lev}$` : '...';
        });
    }
}

function closeModal() {
    document.getElementById('exec-modal').style.display = 'none';
    pendingExec = null;
}

async function confirmExec() {
    if (!pendingExec) return;

    let margin = pendingExec.margin;
    if (margin === 0) {
        const input = document.getElementById('modal-margin');
        if (input) margin = parseFloat(input.value) || 0;
        if (margin <= 0) { input.style.borderColor = 'var(--red)'; return; }
    }

    const confirmBtn = document.getElementById('modal-confirm');
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Execution...';

    try {
        const res = await fetch(`${API}/api/execute/${pendingExec.signal_id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ margin: margin, order_type: pendingExec.order_type }),
        });
        const result = await res.json();

        if (result.success) {
            closeModal();
            refreshAll();
        } else {
            confirmBtn.textContent = result.error || 'Erreur';
            confirmBtn.style.background = 'var(--red)';
            confirmBtn.style.color = '#fff';
            setTimeout(() => {
                confirmBtn.disabled = false;
                confirmBtn.textContent = 'Confirmer';
                confirmBtn.style.background = '';
                confirmBtn.style.color = '';
            }, 2000);
        }
    } catch (e) {
        confirmBtn.textContent = 'Erreur reseau';
        confirmBtn.style.background = 'var(--red)';
        setTimeout(() => {
            confirmBtn.disabled = false;
            confirmBtn.textContent = 'Confirmer';
            confirmBtn.style.background = '';
        }, 2000);
    }
}

// Fermer modal avec Escape
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
});

// --- Signal countdown (expire apres 20s) ---
function updateCountdowns() {
    document.querySelectorAll('.signal-countdown').forEach(el => {
        const sec = parseInt(el.textContent) - 1;
        if (sec <= 0) {
            fetchBotData('V1');
            fetchBotData('V2');
        } else {
            el.textContent = sec + 's';
            if (sec <= 5) el.style.color = 'var(--red)';
        }
    });
}

// --- Candle countdown timer ---
let lastMainPeriod = 0;
let lastPopupPeriod = 0;

function timeframeToMs(tf) {
    const map = { '1m': 60, '3m': 180, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800 };
    return (map[tf] || 300) * 1000;
}

function formatCountdown(ms) {
    if (ms <= 0) return '0:00';
    const totalSec = Math.ceil(ms / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${m}:${String(s).padStart(2,'0')}`;
}

function updateCandleCountdown() {
    const now = Date.now();

    // Chart principal
    const mainEl = document.getElementById('candle-countdown');
    if (mainEl && currentTab === 'charts') {
        const tfMs = timeframeToMs(selectedTimeframe);
        const currentPeriod = Math.floor(now / tfMs);
        const remaining = tfMs - (now % tfMs);
        mainEl.textContent = formatCountdown(remaining);
        mainEl.style.color = remaining < 10000 ? 'var(--red)' : remaining < 30000 ? 'var(--orange)' : 'var(--text-secondary)';

        // Nouvelle bougie detectee -> attendre 2s puis rafraichir sans reset zoom
        if (lastMainPeriod > 0 && currentPeriod !== lastMainPeriod) {
            setTimeout(() => refreshChartCandles(), 2000);
        }
        lastMainPeriod = currentPeriod;
    }

    // Popup chart
    const popupEl = document.getElementById('popup-candle-countdown');
    const modal = document.getElementById('chart-modal');
    if (popupEl && modal && modal.style.display !== 'none') {
        const tfMs = timeframeToMs(popupTimeframe);
        const currentPeriod = Math.floor(now / tfMs);
        const remaining = tfMs - (now % tfMs);
        popupEl.textContent = formatCountdown(remaining);
        popupEl.style.color = remaining < 10000 ? 'var(--red)' : remaining < 30000 ? 'var(--orange)' : 'var(--text-secondary)';

        // Nouvelle bougie detectee -> attendre 2s puis rafraichir sans reset zoom
        if (lastPopupPeriod > 0 && currentPeriod !== lastPopupPeriod) {
            setTimeout(() => refreshPopupChartCandles(), 2000);
        }
        lastPopupPeriod = currentPeriod;
    }
}
setInterval(updateCandleCountdown, 1000);
updateCandleCountdown();

// --- Positions live (WebSocket MEXC direct) ---
let positionsDataV1 = [];
let positionsDataV2 = [];
let livePrices = {};      // symbol -> prix live
let posMexcWs = null;
let posMexcReconnect = null;
let posSubscribedSymbols = new Set();

async function fetchLivePositions() {
    try {
        const [v1Res, v2Res] = await Promise.all([
            fetch(`${API}/api/positions?bot_version=V1`),
            fetch(`${API}/api/positions?bot_version=V2`),
        ]);
        const v1 = await v1Res.json();
        const v2 = await v2Res.json();
        positionsDataV1 = (v1.positions || []).filter(p => p.state !== 'closed');
        positionsDataV2 = (v2.positions || []).filter(p => p.state !== 'closed');

        const all = [...positionsDataV1, ...positionsDataV2];
        if (all.length > 0) {
            connectPosMexcWs();
            updatePositionsUI();
        } else {
            disconnectPosMexcWs();
            document.getElementById('v1-positions-live').innerHTML = '';
            document.getElementById('v2-positions-live').innerHTML = '';
        }
    } catch {
        document.getElementById('v1-positions-live').innerHTML = '';
        document.getElementById('v2-positions-live').innerHTML = '';
    }
}

function connectPosMexcWs() {
    const allPositions = [...positionsDataV1, ...positionsDataV2];
    const symbols = new Set(allPositions.map(p => p.symbol));
    const mexcSymbols = new Map();
    symbols.forEach(s => {
        mexcSymbols.set(s, s.split(':')[0].replace('-', '_').replace('/', '_'));
    });

    // Si deja connecte aux memes symbols, ne rien faire
    if (posMexcWs && posMexcWs.readyState === 1) {
        const newSet = [...symbols].sort().join(',');
        const oldSet = [...posSubscribedSymbols].sort().join(',');
        if (newSet === oldSet) return;
        // Symbols differents -> reconnecter
        posMexcWs.close();
    }

    if (posMexcWs && posMexcWs.readyState <= 1) {
        posMexcWs.onclose = null;
        posMexcWs.close();
    }

    posMexcWs = new WebSocket('wss://contract.mexc.com/edge');
    posSubscribedSymbols = symbols;

    posMexcWs.onopen = () => {
        console.log('MEXC WS connecte pour positions:', [...mexcSymbols.values()]);
        // S'abonner au deal de chaque symbol (chaque trade = prix instantane)
        mexcSymbols.forEach((ms, s) => {
            posMexcWs.send(JSON.stringify({
                method: 'sub.deal',
                param: { symbol: ms }
            }));
        });
        // Keepalive
        posMexcWs._ping = setInterval(() => {
            if (posMexcWs.readyState === 1) posMexcWs.send('{"method":"ping"}');
        }, 20000);
    };

    posMexcWs.onmessage = (evt) => {
        try {
            const msg = JSON.parse(evt.data);
            if (msg.channel === 'push.deal' && msg.data) {
                // msg.data est un tableau de trades
                const deals = Array.isArray(msg.data) ? msg.data : [msg.data];
                const lastDeal = deals[deals.length - 1];
                const price = parseFloat(lastDeal.p);
                const mexcSym = msg.symbol || '';
                if (price > 0) {
                    mexcSymbols.forEach((ms, s) => {
                        if (ms === mexcSym) livePrices[s] = price;
                    });
                    updatePositionsUI();
                }
            }
        } catch {}
    };

    posMexcWs.onclose = () => {
        if (posMexcWs?._ping) clearInterval(posMexcWs._ping);
        posMexcWs = null;
        // Reconnecter si on a encore des positions
        if (positionsDataV1.length > 0 || positionsDataV2.length > 0) {
            posMexcReconnect = setTimeout(connectPosMexcWs, 2000);
        }
    };

    posMexcWs.onerror = () => { if (posMexcWs) posMexcWs.close(); };
}

function disconnectPosMexcWs() {
    if (posMexcReconnect) { clearTimeout(posMexcReconnect); posMexcReconnect = null; }
    if (posMexcWs) {
        if (posMexcWs._ping) clearInterval(posMexcWs._ping);
        posMexcWs.onclose = null;
        posMexcWs.close();
        posMexcWs = null;
    }
    posSubscribedSymbols = new Set();
}

function updatePositionsUI() {
    const v1Result = positionsDataV1.map(p => {
        const cur = livePrices[p.symbol] || p.entry_price;
        return calcLivePnl(p, cur);
    });
    v1Result.sort((a, b) => (b.total_pnl || 0) - (a.total_pnl || 0));
    renderLivePositions(v1Result, 'v1-positions-live');

    const v2Result = positionsDataV2.map(p => {
        const cur = livePrices[p.symbol] || p.entry_price;
        return calcLivePnl(p, cur);
    });
    v2Result.sort((a, b) => (b.total_pnl || 0) - (a.total_pnl || 0));
    renderLivePositions(v2Result, 'v2-positions-live');
}

function calcLivePnl(pos, currentPrice) {
    const entry = pos.entry_price;
    const dir = pos.direction;
    const origQty = pos.original_quantity;
    const remQty = pos.remaining_quantity;
    const margin = pos.margin_required || 1;

    let realized = 0;
    if (pos.tp1_hit) {
        const q = origQty * ((pos.tp1_close_pct || 40) / 100);
        realized += (dir === 'long' ? pos.tp1 - entry : entry - pos.tp1) * q;
    }
    if (pos.tp2_hit) {
        const q = origQty * ((pos.tp2_close_pct || 30) / 100);
        realized += (dir === 'long' ? pos.tp2 - entry : entry - pos.tp2) * q;
    }

    const diff = dir === 'long' ? currentPrice - entry : entry - currentPrice;
    const unrealized = diff * remQty;
    const total = realized + unrealized;
    const pnlPct = (total / margin) * 100;

    const sl = pos.stop_loss, tp3 = pos.tp3;
    let progress = 50;
    if (tp3 !== sl) {
        progress = dir === 'long'
            ? Math.max(0, Math.min(100, ((currentPrice - sl) / (tp3 - sl)) * 100))
            : Math.max(0, Math.min(100, ((sl - currentPrice) / (sl - tp3)) * 100));
    }

    return { ...pos, current_price: currentPrice, total_pnl: total, pnl_pct: pnlPct, progress };
}

function renderLivePositions(positions, containerId) {
    const container = document.getElementById(containerId || 'v2-positions-live');
    if (!positions.length) {
        container.innerHTML = '';
        return;
    }

    container.innerHTML = `
        <div class="positions-live-title"><span class="dot"></span> Positions ouvertes</div>
        ${positions.map(p => {
            const dec = getDecimals(p.entry_price);
            const pnl = p.total_pnl || 0;
            const pnlPct = p.pnl_pct || 0;
            const pnlClass = pnl >= 0 ? 'up' : 'down';
            const cardClass = pnl > 0 ? 'pos-profit' : pnl < 0 ? 'pos-loss' : '';
            const sign = pnl >= 0 ? '+' : '';
            const dir = p.direction === 'long' ? 'LONG' : 'SHORT';
            const dirColor = p.direction === 'long' ? 'var(--green)' : 'var(--red)';

            const stateClass = p.state === 'breakeven' ? 'pos-state-breakeven' :
                               p.state === 'trailing' ? 'pos-state-trailing' : 'pos-state-active';
            const stateLabel = p.state === 'breakeven' ? 'BE' :
                               p.state === 'trailing' ? 'TRAIL' : 'ACTIF';

            // Barre de progression (precalculee par le serveur ou calculee ici)
            let progress = p.progress || 0;
            if (!p.progress) {
                const sl = p.stop_loss;
                const tp3 = p.tp3;
                const cur = p.current_price || p.entry_price;
                if (p.direction === 'long') {
                    progress = Math.max(0, Math.min(100, ((cur - sl) / (tp3 - sl)) * 100));
                } else {
                    progress = Math.max(0, Math.min(100, ((sl - cur) / (sl - tp3)) * 100));
                }
            }
            const barColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';

            return `
            <div class="pos-card ${cardClass}">
                <div class="pos-header">
                    <div style="display:flex;align-items:center;gap:8px">
                        <span class="pos-symbol">${p.symbol.split('/')[0]}</span>
                        <span style="color:${dirColor};font-weight:700;font-size:13px">${dir}</span>
                        <span class="pos-state ${stateClass}">${stateLabel}</span>
                    </div>
                    <div class="pos-pnl ${pnlClass}">${sign}${pnl.toFixed(2)}$ <span style="font-size:13px">(${sign}${pnlPct.toFixed(1)}%)</span></div>
                </div>
                <div class="pos-prices">
                    <div class="pos-price-item">
                        <div class="pos-price-label">Entree</div>
                        <div class="pos-price-val">${p.entry_price.toFixed(dec)}</div>
                    </div>
                    <div class="pos-price-item">
                        <div class="pos-price-label">Prix actuel</div>
                        <div class="pos-price-val" style="color:${pnl >= 0 ? 'var(--green)' : 'var(--red)'}">${(p.current_price || 0).toFixed(dec)}</div>
                    </div>
                    <div class="pos-price-item">
                        <div class="pos-price-label">Marge x${p.leverage || '?'}</div>
                        <div class="pos-price-val">${(p.margin_required || 0).toFixed(0)}$ → ${(p.position_size_usd || 0).toFixed(0)}$</div>
                    </div>
                </div>
                <div class="pos-bar"><div class="pos-bar-fill" style="width:${progress}%;background:${barColor}"></div></div>
                <div class="pos-levels">
                    <span class="pos-level pos-level-sl">SL ${p.stop_loss.toFixed(dec)}</span>
                    <span class="pos-level pos-level-tp ${p.tp1_hit ? 'hit' : ''}">TP1 ${p.tp1.toFixed(dec)}</span>
                    <span class="pos-level pos-level-tp ${p.tp2_hit ? 'hit' : ''}">TP2 ${p.tp2.toFixed(dec)}</span>
                    <span class="pos-level pos-level-tp ${p.tp3_hit ? 'hit' : ''}">TP3 ${p.tp3.toFixed(dec)}</span>
                    <button class="pos-chart-btn" onclick="openChartModal('${p.symbol}', ${p.entry_price}, '${p.direction}', {sl:${p.stop_loss},tp1:${p.tp1},tp2:${p.tp2},tp3:${p.tp3}})">CHART</button>
                    <button class="pos-close-btn" onclick="closePosition(${p.id}, this)">FERMER</button>
                </div>
            </div>`;
        }).join('')}
    `;
}

// --- Fermer position manuellement ---
async function closePosition(posId, btn) {
    // Feedback instantane
    if (btn) { btn.disabled = true; btn.textContent = '...'; }
    // Envoyer le prix live du WS pour eviter un appel MEXC lent
    const allPos = [...positionsDataV1, ...positionsDataV2];
    const pos = allPos.find(p => p.id === posId);
    const livePrice = pos ? (livePrices[pos.symbol] || 0) : 0;
    try {
        const res = await fetch(`${API}/api/positions/${posId}/close`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ price: livePrice }),
        });
        await res.json();
    } catch (e) {
        console.error('Erreur close position:', e);
    }
    refreshAll();
}

// --- Paper Trading Reset ---
async function resetPaper(botVersion) {
    botVersion = botVersion || 'V2';
    if (!confirm(`Remettre le portefeuille ${botVersion} a 100$ ?\nTous les trades et signaux ${botVersion} seront effaces.`)) return;
    try {
        await fetch(`${API}/api/paper/reset?bot_version=${botVersion}`, { method: 'POST' });
        refreshAll();
    } catch (e) {
        console.error('Reset erreur:', e);
    }
}

// ============================================
// POPUP CHART (pour positions)
// ============================================
let popupChart = null;
let popupCandleSeries = null;
let popupVolumeSeries = null;
let popupFvgCanvas = null;
let popupLastCandles = [];
let popupPair = null;
let popupTimeframe = '5m';
let popupShowVol = true;
let popupShowFVG = true;
let popupWs = null;
let popupEntryPrice = null;
let popupDirection = null;
let popupEntryLine = null;
let popupLevelLines = [];
let popupLevels = null; // {sl, tp1, tp2, tp3}

// --- Drag modal chart ---
function initDragModal() {
    const modal = document.querySelector('#chart-modal .chart-modal-lg');
    const header = modal.querySelector('.modal-header');
    let isDragging = false, startX, startY, origX, origY;

    header.style.cursor = 'grab';
    modal.style.position = 'relative';

    header.addEventListener('mousedown', (e) => {
        if (e.target.closest('.btn-icon')) return;
        isDragging = true;
        header.style.cursor = 'grabbing';
        startX = e.clientX;
        startY = e.clientY;
        const style = window.getComputedStyle(modal);
        origX = parseInt(style.left) || 0;
        origY = parseInt(style.top) || 0;
        e.preventDefault();
    });

    document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        modal.style.left = (origX + e.clientX - startX) + 'px';
        modal.style.top = (origY + e.clientY - startY) + 'px';
    });

    document.addEventListener('mouseup', () => {
        if (isDragging) {
            isDragging = false;
            header.style.cursor = 'grab';
        }
    });
}
document.addEventListener('DOMContentLoaded', initDragModal);

function openChartModal(symbol, entryPrice, direction, levels) {
    popupPair = symbol;
    popupTimeframe = '5m';
    popupShowVol = false;
    popupShowFVG = false;
    popupEntryPrice = entryPrice || null;
    popupDirection = direction || null;
    popupLevels = levels || null; // {sl, tp1, tp2, tp3}

    document.getElementById('chart-modal-title').textContent = symbol.split(':')[0];
    document.getElementById('chart-modal').style.display = 'flex';

    // Reset position du modal (au centre)
    const modal = document.querySelector('#chart-modal .chart-modal-lg');
    modal.style.left = '0px';
    modal.style.top = '0px';

    // Reset TF buttons
    document.querySelectorAll('.popup-tf-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.popup-tf-btn[onclick="popupChangeTimeframe(\'5m\')"]').classList.add('active');
    document.getElementById('popup-vol-toggle').classList.remove('active');
    document.getElementById('popup-fvg-toggle').classList.remove('active');

    // Init chart apres un frame (pour que le container ait sa taille)
    requestAnimationFrame(() => {
        initPopupChart();
        loadPopupChart();
    });
}

function closeChartModal() {
    document.getElementById('chart-modal').style.display = 'none';
    disconnectPopupWs();
    if (popupChart) {
        popupChart.remove();
        popupChart = null;
        popupCandleSeries = null;
        popupVolumeSeries = null;
        popupLevelLines = [];
    }
    if (popupFvgCanvas) {
        popupFvgCanvas.remove();
        popupFvgCanvas = null;
    }
    popupLastCandles = [];
}

function initPopupChart() {
    const container = document.getElementById('popup-chart-container');
    container.innerHTML = '';
    if (popupChart) { popupChart.remove(); popupChart = null; }

    popupChart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight || 480,
        layout: { background: { color: '#161b22' }, textColor: '#e6edf3' },
        grid: { vertLines: { color: '#30363d' }, horzLines: { color: '#30363d' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: '#30363d' },
        timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
    });

    popupCandleSeries = popupChart.addCandlestickSeries({
        upColor: '#3fb950', downColor: '#f85149',
        borderUpColor: '#3fb950', borderDownColor: '#f85149',
        wickUpColor: '#3fb950', wickDownColor: '#f85149',
        priceScaleId: 'right',
    });
    popupCandleSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.05, bottom: 0.25 },
    });

    popupVolumeSeries = popupChart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'popup-volume',
        visible: popupShowVol,
    });
    popupChart.priceScale('popup-volume').applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
        drawTicks: false,
        borderVisible: false,
        visible: false,
    });

    // Série invisible sur 2ème échelle de prix pour labels SL/TP
    // --- Selection Zoom (double-clic → dessiner rectangle → zoom) ---
    const zoomOverlay = document.createElement('div');
    zoomOverlay.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;z-index:100;display:none;cursor:crosshair;';
    container.appendChild(zoomOverlay);

    const selBox = document.createElement('div');
    selBox.style.cssText = 'position:absolute;border:2px dashed rgba(88,166,255,0.8);background:rgba(88,166,255,0.12);display:none;pointer-events:none;';
    zoomOverlay.appendChild(selBox);

    const zoomBanner = document.createElement('div');
    zoomBanner.style.cssText = 'position:absolute;top:8px;left:50%;transform:translateX(-50%);z-index:101;background:rgba(88,166,255,0.9);color:#000;padding:5px 18px;border-radius:6px;font-size:13px;font-weight:700;pointer-events:none;';
    zoomBanner.textContent = 'ZOOM — Dessinez un rectangle';
    zoomOverlay.appendChild(zoomBanner);

    let selStart = null;
    let popupZoomed = false;

    // Capture phase pour bloquer le dblclick avant que lightweight-charts le recoive
    container.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        e.preventDefault();
        zoomOverlay.style.display = 'block';
    }, true);

    zoomOverlay.addEventListener('mousedown', (e) => {
        selStart = { x: e.offsetX, y: e.offsetY };
        selBox.style.display = 'block';
        selBox.style.left = selStart.x + 'px';
        selBox.style.top = selStart.y + 'px';
        selBox.style.width = '0px';
        selBox.style.height = '0px';
        e.preventDefault();
    });

    zoomOverlay.addEventListener('mousemove', (e) => {
        if (!selStart) return;
        const x = Math.min(selStart.x, e.offsetX);
        const y = Math.min(selStart.y, e.offsetY);
        const w = Math.abs(e.offsetX - selStart.x);
        const h = Math.abs(e.offsetY - selStart.y);
        selBox.style.left = x + 'px';
        selBox.style.top = y + 'px';
        selBox.style.width = w + 'px';
        selBox.style.height = h + 'px';
    });

    zoomOverlay.addEventListener('mouseup', (e) => {
        const endX = e.offsetX;
        const w = Math.abs(endX - (selStart ? selStart.x : 0));
        if (selStart && w > 30) {
            const leftX = Math.min(selStart.x, endX);
            const rightX = Math.max(selStart.x, endX);
            const ts = popupChart.timeScale();

            // Calculer via logical range (plus fiable que coordinateToTime)
            const logRange = ts.getVisibleLogicalRange();
            if (logRange) {
                const chartWidth = container.clientWidth;
                const totalBars = logRange.to - logRange.from;
                const fromBar = logRange.from + (leftX / chartWidth) * totalBars;
                const toBar = logRange.from + (rightX / chartWidth) * totalBars;
                ts.setVisibleLogicalRange({ from: fromBar, to: toBar });
            }

            // Bloquer l'auto-scroll du WebSocket
            popupChart.timeScale().applyOptions({ shiftVisibleRangeOnNewBar: false });
            window._popupZoomed = true;
            setTimeout(() => drawPopupFVG(), 50);
        }
        // Fermer le mode zoom
        selStart = null;
        selBox.style.display = 'none';
        zoomOverlay.style.display = 'none';
    });

    popupFvgCanvas = document.createElement('canvas');
    popupFvgCanvas.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;z-index:2;';
    container.style.position = 'relative';
    container.appendChild(popupFvgCanvas);

    // Boucle rAF continue pour redessiner FVG + panneau niveaux
    function popupFvgLoop() {
        const modal = document.getElementById('chart-modal');
        if (popupChart && modal && modal.style.display !== 'none') {
            if (popupShowFVG) drawPopupFVG();
            updatePopupLevelsPanel();
        }
        requestAnimationFrame(popupFvgLoop);
    }
    requestAnimationFrame(popupFvgLoop);

    // Resize handler
    const resizeObs = new ResizeObserver(() => {
        if (popupChart && document.getElementById('chart-modal').style.display !== 'none') {
            popupChart.applyOptions({ width: container.clientWidth });
            drawPopupFVG();
        }
    });
    resizeObs.observe(container);
}

async function loadPopupChart() {
    if (!popupPair || !popupChart) return;
    try {
        const sym = popupPair.replace('/', '-');
        const res = await fetch(`${API}/api/ohlcv/${sym}?timeframe=${popupTimeframe}&limit=300`);
        const data = await res.json();
        const candles = data.candles || [];
        if (!candles.length) return;

        const price = candles[candles.length - 1].close;
        let precision, minMove;
        if (price >= 100)       { precision = 2; minMove = 0.01; }
        else if (price >= 1)    { precision = 4; minMove = 0.0001; }
        else if (price >= 0.01) { precision = 4; minMove = 0.0001; }
        else                    { precision = 8; minMove = 0.00000001; }

        popupCandleSeries.applyOptions({ priceFormat: { type: 'price', precision, minMove } });

        const tzOffset = new Date().getTimezoneOffset() * -60;
        const adjusted = candles.map(c => ({
            time: c.time + tzOffset,
            open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume,
        }));

        popupCandleSeries.setData(adjusted.map(c => ({
            time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
        })));

        popupVolumeSeries.setData(adjusted.map(c => ({
            time: c.time, value: c.volume,
            color: c.close >= c.open ? 'rgba(63,185,80,0.5)' : 'rgba(248,81,73,0.5)',
        })));

        popupLastCandles = adjusted;

        // Supprimer anciennes lignes
        if (popupEntryLine) {
            popupCandleSeries.removePriceLine(popupEntryLine);
            popupEntryLine = null;
        }
        popupLevelLines.forEach(l => {
            try { popupCandleSeries.removePriceLine(l); } catch {}
        });
        popupLevelLines = [];

        // Ligne d'entree (bleu pointille) - pas de label sur l'axe principal
        if (popupEntryPrice) {
            popupEntryLine = popupCandleSeries.createPriceLine({
                price: popupEntryPrice,
                color: '#58a6ff',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: false,
                title: '',
            });
        }

        // Lignes SL / TP1 / TP2 / TP3 - pas de label sur l'axe principal
        if (popupLevels) {
            const levels = [
                { key: 'sl',  label: 'SL',  color: '#f85149', width: 2, style: LightweightCharts.LineStyle.Solid },
                { key: 'tp1', label: 'TP1', color: '#3fb950', width: 1, style: LightweightCharts.LineStyle.Dashed },
                { key: 'tp2', label: 'TP2', color: '#3fb950', width: 1, style: LightweightCharts.LineStyle.Dashed },
                { key: 'tp3', label: 'TP3', color: '#d2992a', width: 2, style: LightweightCharts.LineStyle.Dashed },
            ];
            levels.forEach(lv => {
                const price = popupLevels[lv.key];
                if (!price) return;
                popupLevelLines.push(popupCandleSeries.createPriceLine({
                    price,
                    color: lv.color,
                    lineWidth: lv.width,
                    lineStyle: lv.style,
                    axisLabelVisible: false,
                    title: '',
                }));
            });
        }

        // Mettre a jour le panneau des niveaux
        updatePopupLevelsPanel();


        popupChart.timeScale().fitContent();
        setTimeout(() => drawPopupFVG(), 50);
        connectPopupWs();
    } catch (e) {
        console.error('Popup chart error:', e);
    }
}

function updatePopupLevelsPanel() {
    const panel = document.getElementById('popup-levels-panel');
    if (!panel || !popupCandleSeries || !popupChart) { if (panel) panel.innerHTML = ''; return; }

    const dec = getDecimals(popupEntryPrice || 1);
    const allLevels = [];

    if (popupEntryPrice) {
        allLevels.push({ label: popupDirection === 'short' ? 'SHORT' : 'LONG', price: popupEntryPrice, color: '#58a6ff', bg: 'rgba(88,166,255,0.15)' });
    }
    if (popupLevels) {
        if (popupLevels.sl) allLevels.push({ label: 'SL', price: popupLevels.sl, color: '#f85149', bg: 'rgba(248,81,73,0.15)' });
        if (popupLevels.tp1) allLevels.push({ label: 'TP1', price: popupLevels.tp1, color: '#3fb950', bg: 'rgba(63,185,80,0.15)' });
        if (popupLevels.tp2) allLevels.push({ label: 'TP2', price: popupLevels.tp2, color: '#3fb950', bg: 'rgba(63,185,80,0.15)' });
        if (popupLevels.tp3) allLevels.push({ label: 'TP3', price: popupLevels.tp3, color: '#d2992a', bg: 'rgba(210,153,34,0.15)' });
    }

    if (!allLevels.length) { panel.innerHTML = ''; return; }

    panel.innerHTML = allLevels.map(lv => {
        const y = popupCandleSeries.priceToCoordinate(lv.price);
        if (y === null) return '';
        return `<div class="popup-level-label" style="top:${y}px;background:${lv.bg};color:${lv.color};border:1px solid ${lv.color}">
            <span class="lvl-name">${lv.label}</span>
            <span class="lvl-price">${lv.price.toFixed(dec)}</span>
        </div>`;
    }).join('');
}

function popupChangeTimeframe(tf) {
    popupTimeframe = tf;
    lastPopupPeriod = 0;
    document.querySelectorAll('.popup-tf-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.popup-tf-btn[onclick="popupChangeTimeframe('${tf}')"]`).classList.add('active');
    loadPopupChart();
}

function popupToggleVolume() {
    popupShowVol = !popupShowVol;
    const btn = document.getElementById('popup-vol-toggle');
    btn.textContent = popupShowVol ? 'VOL' : 'VOL';
    btn.classList.toggle('active', popupShowVol);
    if (popupVolumeSeries) popupVolumeSeries.applyOptions({ visible: popupShowVol });
}

function popupToggleFVG() {
    popupShowFVG = !popupShowFVG;
    const btn = document.getElementById('popup-fvg-toggle');
    btn.textContent = popupShowFVG ? 'FVG' : 'FVG';
    btn.classList.toggle('active', popupShowFVG);
    drawPopupFVG();
}

function drawPopupFVG() {
    if (!popupFvgCanvas || !popupChart || !popupCandleSeries) return;
    const container = document.getElementById('popup-chart-container');
    popupFvgCanvas.width = container.clientWidth;
    popupFvgCanvas.height = container.clientHeight;
    const ctx = popupFvgCanvas.getContext('2d');
    ctx.clearRect(0, 0, popupFvgCanvas.width, popupFvgCanvas.height);
    if (!popupShowFVG || !popupLastCandles.length) return;

    const fvgs = detectFVG(popupLastCandles);
    const timeScale = popupChart.timeScale();

    fvgs.forEach(fvg => {
        const y1 = popupCandleSeries.priceToCoordinate(fvg.top);
        const y2 = popupCandleSeries.priceToCoordinate(fvg.bottom);
        const x1 = timeScale.timeToCoordinate(fvg.timeStart);
        const x2 = fvg.filled ? timeScale.timeToCoordinate(fvg.timeEnd) : popupFvgCanvas.width;
        if (y1 === null || y2 === null || x1 === null) return;
        const y = Math.min(y1, y2);
        const h = Math.abs(y2 - y1);
        const w = (x2 !== null ? x2 : popupFvgCanvas.width) - x1;
        if (w <= 0 || h <= 0) return;
        const alpha = fvg.filled ? 0.12 : 0.3;
        ctx.fillStyle = fvg.type === 'bull'
            ? `rgba(38, 166, 154, ${alpha})`
            : `rgba(239, 83, 80, ${alpha})`;
        ctx.fillRect(x1, y, w, h);
        if (!fvg.filled) {
            ctx.strokeStyle = fvg.type === 'bull' ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)';
            ctx.lineWidth = 1;
            ctx.strokeRect(x1, y, w, h);
        }
    });
}

function popupResetZoom() {
    if (popupChart) {
        popupChart.timeScale().applyOptions({ shiftVisibleRangeOnNewBar: true });
        popupChart.timeScale().fitContent();
        // Reset le flag zoomed (variable dans initPopupChart closure)
        window._popupZoomed = false;
        setTimeout(() => drawPopupFVG(), 50);
    }
}

function connectPopupWs() {
    disconnectPopupWs();
    if (!popupPair) return;
    const sym = popupPair.replace('/', '-');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    popupWs = new WebSocket(`${proto}//${location.host}/ws/kline/${sym}/${popupTimeframe}`);
    popupWs.onmessage = (evt) => {
        try {
            const data = JSON.parse(evt.data);
            if (!popupCandleSeries || !popupVolumeSeries) return;
            const tzOffset = new Date().getTimezoneOffset() * -60;
            const time = data.t + tzOffset;
            const candle = { time, open: data.o, high: data.h, low: data.l, close: data.c, volume: data.q || data.a || 0 };
            const last = popupLastCandles[popupLastCandles.length - 1];
            const isNewCandle = !last || candle.time > last.time;
            let savedRange = null;
            if (isNewCandle && popupChart) {
                savedRange = popupChart.timeScale().getVisibleLogicalRange();
            }
            if (last && last.time === candle.time) {
                last.open = candle.open; last.high = candle.high; last.low = candle.low;
                last.close = candle.close; last.volume = candle.volume;
            } else if (isNewCandle) {
                popupLastCandles.push(candle);
            }
            popupCandleSeries.update({ time: candle.time, open: candle.open, high: candle.high, low: candle.low, close: candle.close });
            popupVolumeSeries.update({ time: candle.time, value: candle.volume, color: candle.close >= candle.open ? 'rgba(63,185,80,0.5)' : 'rgba(248,81,73,0.5)' });
            if (savedRange) {
                popupChart.timeScale().setVisibleLogicalRange({ from: savedRange.from + 1, to: savedRange.to + 1 });
            }
        } catch {}
    };
    popupWs.onclose = () => { popupWs = null; };
    popupWs.onerror = () => { if (popupWs) popupWs.close(); };
}

function disconnectPopupWs() {
    if (popupWs) { popupWs.onclose = null; popupWs.close(); popupWs = null; }
}

// Fermer chart modal avec Escape
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.getElementById('chart-modal').style.display !== 'none') {
        closeChartModal();
    }
});

// PWA desactive - pas de Service Worker (evite les problemes de cache)

// ============================================================
// FREQTRADE INTEGRATION
// ============================================================

async function fetchFreqtradeData() {
    try {
        const [statsRes, openRes, tradesRes] = await Promise.all([
            fetch(`${API}/api/freqtrade/stats`),
            fetch(`${API}/api/freqtrade/openTrades`),
            fetch(`${API}/api/freqtrade/trades`),
        ]);
        const stats = await statsRes.json();
        const open = await openRes.json();
        const trades = await tradesRes.json();

        const openTrades = open.trades || [];
        updateFreqtradeStats(stats, openTrades.length);
        renderFreqtradeOpen(openTrades);
        renderFreqtradeTrades(trades.trades || []);
    } catch (e) {
        console.error('Freqtrade fetch error:', e);
        updateFreqtradeStats({ bot_running: false });
    }
}

function updateFreqtradeStats(stats, openCount = 0) {
    const statusEl = document.getElementById('ft-status');
    if (stats.bot_running) {
        statusEl.textContent = 'en ligne';
        statusEl.className = 'badge badge-online';
    } else {
        statusEl.textContent = 'hors ligne';
        statusEl.className = 'badge badge-offline';
    }

    const bal = stats.balance || 0;
    const balEl = document.getElementById('ft-balance');
    balEl.textContent = `$${bal.toFixed(2)}`;
    balEl.style.color = bal >= 99 ? 'var(--green)' : 'var(--red)';

    const pnl = stats.total_pnl || 0;
    const pnlEl = document.getElementById('ft-pnl');
    pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
    pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';

    document.getElementById('ft-wins').textContent = stats.wins || 0;
    document.getElementById('ft-losses').textContent = stats.losses || 0;
    document.getElementById('ft-winrate').textContent = `${stats.win_rate || 0}%`;
    document.getElementById('ft-active').textContent = openCount;
    document.getElementById('ft-trades').textContent = stats.trade_count || 0;
}

function renderFreqtradeOpen(trades) {
    const container = document.getElementById('ft-open-list');
    if (!trades.length) {
        container.innerHTML = '<div class="ft-empty">Aucune position ouverte</div>';
        return;
    }
    container.innerHTML = trades.map(t => {
        const pnl = t.pnl_usd || 0;
        const pnlPct = t.pnl_pct || 0;
        const isProfit = pnl >= 0;
        const cardClass = pnl > 0 ? 'pos-profit' : pnl < 0 ? 'pos-loss' : '';
        const pnlCls = isProfit ? 'up' : 'down';
        const sign = pnl >= 0 ? '+' : '';
        const chartSymbol = t.symbol.split(':')[0]; // TRUMP/USDT (sans :USDT)
        const sym = t.symbol.replace(':USDT', '').replace('/USDT', '');
        const dir = t.direction === 'long' ? 'LONG' : 'SHORT';
        const dirColor = t.direction === 'long' ? 'var(--green)' : 'var(--red)';
        const dec = getDecimals(t.entry_price);
        const leverage = t.leverage || 1;
        const posSize = (t.stake_amount || 0) * leverage;

        // Barre de progression : distance entre entry et stoploss vs entry et current
        let progress = 50;
        const sl = t.stoploss || 0;
        const entry = t.entry_price || 0;
        const cur = t.current_price || entry;
        if (sl && entry) {
            const maxDist = Math.abs(entry - sl) * 3; // SL = 33%, profit cote = 67%
            if (t.direction === 'long') {
                progress = Math.max(0, Math.min(100, ((cur - sl) / (maxDist || 1)) * 100));
            } else {
                progress = Math.max(0, Math.min(100, ((sl - cur) / (maxDist || 1)) * 100));
            }
        }
        const barColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';

        return `
        <div class="pos-card ${cardClass}">
            <div class="pos-header">
                <div style="display:flex;align-items:center;gap:8px">
                    <span class="ft-badge">FT</span>
                    <span class="pos-symbol">${sym}</span>
                    <span style="color:${dirColor};font-weight:700;font-size:13px">${dir}</span>
                </div>
                <div class="pos-pnl ${pnlCls}">${sign}${pnl.toFixed(2)}$ <span style="font-size:13px">(${sign}${pnlPct.toFixed(2)}%)</span></div>
            </div>
            <div class="pos-prices">
                <div class="pos-price-item">
                    <div class="pos-price-label">Entree</div>
                    <div class="pos-price-val">${entry.toFixed(dec)}</div>
                </div>
                <div class="pos-price-item">
                    <div class="pos-price-label">Prix actuel</div>
                    <div class="pos-price-val" style="color:${pnl >= 0 ? 'var(--green)' : 'var(--red)'}">${cur.toFixed(dec)}</div>
                </div>
                <div class="pos-price-item">
                    <div class="pos-price-label">Mise x${leverage}</div>
                    <div class="pos-price-val">${(t.stake_amount || 0).toFixed(0)}$ → ${posSize.toFixed(0)}$</div>
                </div>
            </div>
            <div class="pos-bar"><div class="pos-bar-fill" style="width:${progress}%;background:${barColor}"></div></div>
            <div class="pos-levels">
                <span class="pos-level pos-level-sl">SL ${sl ? sl.toFixed(dec) : '--'}</span>
                <span class="ft-reason-inline">${t.strategy} · ${t.timeframe}</span>
                <button class="pos-chart-btn" onclick="openChartModal('${chartSymbol}', ${entry}, '${t.direction}', {sl:${sl || 0}})">CHART</button>
            </div>
        </div>`;
    }).join('');
}

function renderFreqtradeTrades(trades) {
    const container = document.getElementById('ft-closed-list');
    if (!trades.length) {
        container.innerHTML = '<div class="ft-empty">Aucun trade ferme</div>';
        return;
    }
    container.innerHTML = trades.map(t => {
        const pnl = t.pnl_usd || 0;
        const pnlPct = t.pnl_pct || 0;
        const isProfit = pnl >= 0;
        const cardClass = pnl > 0 ? 'pos-profit' : pnl < 0 ? 'pos-loss' : '';
        const pnlCls = isProfit ? 'up' : 'down';
        const sign = pnl >= 0 ? '+' : '';
        const sym = t.symbol.replace(':USDT', '').replace('/USDT', '');
        const dir = t.direction === 'long' ? 'LONG' : 'SHORT';
        const dirColor = t.direction === 'long' ? 'var(--green)' : 'var(--red)';
        const dec = getDecimals(t.entry_price);
        const resultBadge = isProfit
            ? '<span style="background:rgba(63,185,80,0.15);color:var(--green);padding:2px 6px;border-radius:3px;font-size:10px;font-weight:600">WIN</span>'
            : '<span style="background:rgba(248,81,73,0.15);color:var(--red);padding:2px 6px;border-radius:3px;font-size:10px;font-weight:600">LOSS</span>';
        return `
        <div class="pos-card ${cardClass}">
            <div class="pos-header">
                <div style="display:flex;align-items:center;gap:8px">
                    <span class="ft-badge">FT</span>
                    <span class="pos-symbol">${sym}</span>
                    <span style="color:${dirColor};font-weight:700;font-size:13px">${dir}</span>
                    ${resultBadge}
                </div>
                <div class="pos-pnl ${pnlCls}">${sign}${pnl.toFixed(2)}$ <span style="font-size:13px">(${sign}${pnlPct.toFixed(2)}%)</span></div>
            </div>
            <div class="pos-prices">
                <div class="pos-price-item">
                    <div class="pos-price-label">Entree</div>
                    <div class="pos-price-val">${(t.entry_price || 0).toFixed(dec)}</div>
                </div>
                <div class="pos-price-item">
                    <div class="pos-price-label">Sortie</div>
                    <div class="pos-price-val" style="color:${pnl >= 0 ? 'var(--green)' : 'var(--red)'}">${(t.exit_price || 0).toFixed(dec)}</div>
                </div>
                <div class="pos-price-item">
                    <div class="pos-price-label">Raison</div>
                    <div class="pos-price-val" style="font-size:13px">${t.close_reason || '-'}</div>
                </div>
            </div>
            <div class="pos-levels">
                <span class="ft-reason-inline">${t.strategy} · ${t.open_date} → ${t.close_date || '?'}</span>
            </div>
        </div>`;
    }).join('');
}

// --- Comparaison V1 vs V2 ---

async function loadCompareData() {
    try {
        const [v1Hist, v2Hist, v1Port, v2Port, ftStats, ftTrades] = await Promise.all([
            fetch(`${API}/api/pnl-history?bot_version=V1&days=${comparePeriod}`).then(r => r.json()),
            fetch(`${API}/api/pnl-history?bot_version=V2&days=${comparePeriod}`).then(r => r.json()),
            fetch(`${API}/api/paper/portfolio?bot_version=V1`).then(r => r.json()),
            fetch(`${API}/api/paper/portfolio?bot_version=V2`).then(r => r.json()),
            fetch(`${API}/api/freqtrade/stats`).then(r => r.json()).catch(() => ({})),
            fetch(`${API}/api/freqtrade/trades?limit=200`).then(r => r.json()).catch(() => ({ trades: [] })),
        ]);
        updateCompareStats('v1', v1Port);
        updateCompareStats('v2', v2Port);
        updateCompareFtStats(ftStats);

        // Reconstruire l'historique P&L FT depuis les trades fermes
        const ftHistory = buildFtPnlHistory(ftTrades.trades || [], ftStats.balance || 0, ftStats.total_pnl || 0);

        renderCompareChart(
            v1Hist.history || [], v2Hist.history || [], ftHistory,
            v1Port.initial_balance || 100, v2Port.initial_balance || 100
        );
    } catch (e) {
        console.error('loadCompareData error:', e);
    }
}

function updateCompareStats(v, portfolio) {
    const bal = portfolio.current_balance || 0;
    const pnl = portfolio.total_pnl || 0;
    const trades = portfolio.total_trades || 0;
    const wins = portfolio.wins || 0;
    const losses = portfolio.losses || 0;
    const wr = trades > 0 ? (wins / trades * 100).toFixed(1) : '0';
    const pnlSign = pnl >= 0 ? '+' : '';
    const pnlColor = pnl >= 0 ? 'color:var(--green)' : 'color:var(--red)';

    const el = (id) => document.getElementById(id);
    el(`comp-${v}-balance`).textContent = `$${bal.toFixed(2)}`;
    el(`comp-${v}-pnl`).innerHTML = `<span style="${pnlColor}">${pnlSign}${pnl.toFixed(2)}$</span>`;
    el(`comp-${v}-trades`).textContent = trades;
    el(`comp-${v}-winrate`).textContent = `${wr}%`;
    el(`comp-${v}-wl`).innerHTML = `<span style="color:var(--green)">${wins}</span> / <span style="color:var(--red)">${losses}</span>`;
}

function updateCompareFtStats(ftStats) {
    const el = (id) => document.getElementById(id);
    if (!ftStats || ftStats.error) {
        el('comp-ft-balance').textContent = 'hors ligne';
        el('comp-ft-pnl').textContent = '--';
        el('comp-ft-trades').textContent = '--';
        el('comp-ft-winrate').textContent = '--';
        el('comp-ft-wl').textContent = '-- / --';
        return;
    }
    const bal = ftStats.balance || 0;
    const pnl = ftStats.total_pnl || 0;
    const trades = ftStats.trade_count || 0;
    const wins = ftStats.wins || 0;
    const losses = ftStats.losses || 0;
    const wr = ftStats.win_rate || 0;
    const pnlSign = pnl >= 0 ? '+' : '';
    const pnlColor = pnl >= 0 ? 'color:var(--green)' : 'color:var(--red)';

    el('comp-ft-balance').textContent = `$${bal.toFixed(2)}`;
    el('comp-ft-pnl').innerHTML = `<span style="${pnlColor}">${pnlSign}${pnl.toFixed(2)}$</span>`;
    el('comp-ft-trades').textContent = trades;
    el('comp-ft-winrate').textContent = `${wr}%`;
    el('comp-ft-wl').innerHTML = `<span style="color:var(--green)">${wins}</span> / <span style="color:var(--red)">${losses}</span>`;
}

function buildFtPnlHistory(trades, currentBalance, totalPnl) {
    // Filtrer les trades fermes (avec close_date) et trier par close_date croissant
    const closed = trades.filter(t => t.close_date);
    closed.sort((a, b) => new Date(a.close_date) - new Date(b.close_date));
    if (!closed.length) return [];

    // Reconstruire le cumul P&L depuis les trades fermes
    let cumPnl = 0;
    const history = [];
    for (const t of closed) {
        const pnl = t.pnl_usd || 0;
        cumPnl += pnl;
        history.push({ timestamp: t.close_date, cumulative_pnl: cumPnl });
    }
    return history;
}

function renderCompareChart(v1Data, v2Data, ftData, v1Base, v2Base) {
    const container = document.getElementById('compare-chart-container');
    if (!container) return;

    // Detruire ancien chart
    if (compareChart) {
        compareChart.remove();
        compareChart = null;
    }
    container.innerHTML = '';

    // Cas vide
    if (!v1Data.length && !v2Data.length && !ftData.length) {
        container.innerHTML = '<div class="compare-empty">Pas encore de trades — les courbes apparaitront ici</div>';
        return;
    }

    const utcOffset = new Date().getTimezoneOffset() * 60;

    compareChart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 400,
        layout: { background: { color: '#161b22' }, textColor: '#e6edf3' },
        grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
        rightPriceScale: { borderColor: '#30363d' },
        timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
        crosshair: { mode: 0 },
    });

    // V1 line (bleu)
    compareV1Series = compareChart.addLineSeries({
        color: '#58a6ff',
        lineWidth: 2,
        title: 'V1',
        priceFormat: { type: 'custom', formatter: (p) => '$' + p.toFixed(2) },
    });

    // V2 line (violet)
    compareV2Series = compareChart.addLineSeries({
        color: '#bc8cff',
        lineWidth: 2,
        title: 'V2',
        priceFormat: { type: 'custom', formatter: (p) => '$' + p.toFixed(2) },
    });

    // FT line (orange)
    compareFtSeries = compareChart.addLineSeries({
        color: '#d29922',
        lineWidth: 2,
        title: 'FT',
        priceFormat: { type: 'custom', formatter: (p) => '$' + p.toFixed(2) },
    });

    // Baseline 100$ (gris)
    const baselineSeries = compareChart.addLineSeries({
        color: '#484f58',
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
    });

    function mapData(data, base) {
        const points = [];
        if (data.length > 0) {
            const firstTs = Math.floor(new Date(data[0].timestamp).getTime() / 1000) - utcOffset - 1;
            points.push({ time: firstTs, value: base });
        }
        for (const d of data) {
            const ts = Math.floor(new Date(d.timestamp).getTime() / 1000) - utcOffset;
            points.push({ time: ts, value: base + (d.cumulative_pnl || 0) });
        }
        return points;
    }

    const v1Points = mapData(v1Data, v1Base);
    const v2Points = mapData(v2Data, v2Base);
    // FT: balance initiale estimee = balance actuelle - pnl total
    const ftBase = ftData.length > 0 ? 100 : 100; // normalise a 100$ pour comparaison
    const ftPoints = mapData(ftData, ftBase);

    if (v1Points.length) compareV1Series.setData(v1Points);
    if (v2Points.length) compareV2Series.setData(v2Points);
    if (ftPoints.length) compareFtSeries.setData(ftPoints);

    // Baseline: ligne plate a 100$ sur toute la plage
    const allTimes = [...v1Points, ...v2Points, ...ftPoints].map(p => p.time).filter(Boolean);
    if (allTimes.length >= 2) {
        const minT = Math.min(...allTimes);
        const maxT = Math.max(...allTimes);
        baselineSeries.setData([
            { time: minT, value: 100 },
            { time: maxT, value: 100 },
        ]);
    }

    compareChart.timeScale().fitContent();

    // Responsive
    new ResizeObserver(() => {
        if (compareChart) compareChart.applyOptions({ width: container.clientWidth });
    }).observe(container);
}

function changeCompPeriod(days) {
    comparePeriod = days;
    document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
    loadCompareData();
}
