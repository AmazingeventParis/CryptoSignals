"""
Flow Intelligence v2 : Aggrege toutes les sources de flux.
- OrderFlowTracker (MEXC tick data): CVD, whales, deltas, aggressive ratio
- MicrostructureAnalyzer: VPIN, sweeps, tape speed, imbalance
- Binance REST (gratuit): L/S global, top traders L/S, taker buy/sell, OI, basis
- Binance WS: liquidation stream
- Funding momentum tracking
V4 only.
"""
import asyncio
import json
import logging
from collections import deque
from datetime import datetime

import httpx
import websockets

logger = logging.getLogger(__name__)

# Binance API endpoints (all free, no key required)
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_API = "https://api.binance.com"
BINANCE_LIQ_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"

LEVERAGE_LEVELS = [5, 10, 25, 50, 100]
MAINTENANCE_MARGIN = 0.005


def _mexc_to_binance_symbol(symbol: str) -> str:
    """Convert 'BTC/USDT:USDT' -> 'BTCUSDT'"""
    return symbol.split(":")[0].replace("/", "")


class FlowIntelligence:
    def __init__(self, order_flow_tracker, microstructure_analyzer, symbols: list[str]):
        self.oft = order_flow_tracker
        self.micro = microstructure_analyzer
        self.symbols = symbols
        self.running = False

        # --- Caches ---
        # Binance global L/S ratio
        self._binance_ls_cache: dict[str, dict] = {}
        # Binance top traders L/S (account ratio)
        self._top_traders_ls_cache: dict[str, dict] = {}
        # Binance top traders L/S (position ratio)
        self._top_traders_pos_cache: dict[str, dict] = {}
        # Binance taker buy/sell volume ratio
        self._taker_volume_cache: dict[str, dict] = {}
        # Binance OI + OI/Price divergence
        self._oi_cache: dict[str, dict] = {}
        # Spot vs Futures basis
        self._basis_cache: dict[str, dict] = {}
        # Funding rate momentum (rolling history)
        self._funding_history: dict[str, deque] = {s: deque(maxlen=12) for s in symbols}
        self._funding_momentum_cache: dict[str, dict] = {}
        # Liquidations
        self._binance_liquidations: deque = deque(maxlen=500)
        # Estimated liquidation levels
        self._liquidation_levels: dict[str, dict] = {}

        self._tasks: list[asyncio.Task] = []
        self._binance_symbols = {s: _mexc_to_binance_symbol(s) for s in symbols}
        self._reverse_map = {v: k for k, v in self._binance_symbols.items()}

    async def start(self):
        self.running = True
        self._tasks.append(asyncio.create_task(self._fetch_binance_data_loop()))
        self._tasks.append(asyncio.create_task(self._liquidation_ws_loop()))
        self._tasks.append(asyncio.create_task(self._recalc_derived_loop()))
        logger.info(f"FlowIntelligence v2 started for {len(self.symbols)} symbols")

    async def stop(self):
        self.running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        logger.info("FlowIntelligence v2 stopped")

    # =============================
    # Background: Binance REST data (all in one loop, rate-friendly)
    # =============================

    async def _fetch_binance_data_loop(self):
        """Fetch all Binance REST data every 5 minutes. ~40 requests per cycle = negligible."""
        while self.running:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    for symbol in self.symbols:
                        if not self.running:
                            break
                        bsym = self._binance_symbols.get(symbol)
                        if not bsym:
                            continue

                        await self._fetch_symbol_data(client, symbol, bsym)
                        await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"FlowIntel data loop error: {e}")
            await asyncio.sleep(300)

    async def _fetch_symbol_data(self, client: httpx.AsyncClient, symbol: str, bsym: str):
        """Fetch all data for one symbol. 5 API calls per symbol."""
        now_iso = datetime.utcnow().isoformat()

        # 1. Global L/S ratio
        try:
            r = await client.get(f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
                                 params={"symbol": bsym, "period": "5m", "limit": 1})
            if r.status_code == 200:
                data = r.json()
                if data:
                    e = data[0]
                    self._binance_ls_cache[symbol] = {
                        "ratio": round(float(e.get("longShortRatio", 1)), 3),
                        "long_pct": round(float(e.get("longAccount", 0.5)) * 100, 1),
                        "short_pct": round(float(e.get("shortAccount", 0.5)) * 100, 1),
                        "ts": now_iso,
                    }
        except Exception as e:
            logger.debug(f"FlowIntel global L/S {bsym}: {e}")

        # 2. Top Traders L/S (account — smart money)
        try:
            r = await client.get(f"{BINANCE_FAPI}/futures/data/topLongShortAccountRatio",
                                 params={"symbol": bsym, "period": "5m", "limit": 1})
            if r.status_code == 200:
                data = r.json()
                if data:
                    e = data[0]
                    self._top_traders_ls_cache[symbol] = {
                        "ratio": round(float(e.get("longShortRatio", 1)), 3),
                        "long_pct": round(float(e.get("longAccount", 0.5)) * 100, 1),
                        "short_pct": round(float(e.get("shortAccount", 0.5)) * 100, 1),
                        "ts": now_iso,
                    }
        except Exception as e:
            logger.debug(f"FlowIntel top traders L/S {bsym}: {e}")

        # 3. Top Traders L/S (position — big money direction)
        try:
            r = await client.get(f"{BINANCE_FAPI}/futures/data/topLongShortPositionRatio",
                                 params={"symbol": bsym, "period": "5m", "limit": 1})
            if r.status_code == 200:
                data = r.json()
                if data:
                    e = data[0]
                    self._top_traders_pos_cache[symbol] = {
                        "ratio": round(float(e.get("longShortRatio", 1)), 3),
                        "long_pct": round(float(e.get("longAccount", 0.5)) * 100, 1),
                        "short_pct": round(float(e.get("shortAccount", 0.5)) * 100, 1),
                        "ts": now_iso,
                    }
        except Exception as e:
            logger.debug(f"FlowIntel top pos L/S {bsym}: {e}")

        # 4. Taker Buy/Sell Volume Ratio
        try:
            r = await client.get(f"{BINANCE_FAPI}/futures/data/takerlongshortRatio",
                                 params={"symbol": bsym, "period": "5m", "limit": 3})
            if r.status_code == 200:
                data = r.json()
                if data:
                    latest = data[0]
                    ratio = float(latest.get("buyVol", 1)) / max(float(latest.get("sellVol", 1)), 1e-8)
                    buy_vol = float(latest.get("buySellRatio", 1))
                    # Compute trend from 3 datapoints
                    trend = "neutral"
                    if len(data) >= 3:
                        ratios = [float(d.get("buySellRatio", 1)) for d in data]
                        if ratios[0] > ratios[1] > ratios[2]:
                            trend = "increasing_buy"
                        elif ratios[0] < ratios[1] < ratios[2]:
                            trend = "increasing_sell"
                    self._taker_volume_cache[symbol] = {
                        "ratio": round(buy_vol, 3),
                        "trend": trend,
                        "ts": now_iso,
                    }
        except Exception as e:
            logger.debug(f"FlowIntel taker volume {bsym}: {e}")

        # 5. Open Interest (current + recent history for divergence)
        try:
            r = await client.get(f"{BINANCE_FAPI}/futures/data/openInterestHist",
                                 params={"symbol": bsym, "period": "5m", "limit": 6})
            if r.status_code == 200:
                data = r.json()
                if data and len(data) >= 2:
                    latest_oi = float(data[0].get("sumOpenInterestValue", 0))
                    prev_oi = float(data[-1].get("sumOpenInterestValue", 0))
                    oi_change_pct = ((latest_oi - prev_oi) / max(prev_oi, 1e-8)) * 100

                    self._oi_cache[symbol] = {
                        "current": round(latest_oi, 0),
                        "prev_30m": round(prev_oi, 0),
                        "change_pct": round(oi_change_pct, 2),
                        "ts": now_iso,
                    }
        except Exception as e:
            logger.debug(f"FlowIntel OI {bsym}: {e}")

        # 6. Spot vs Futures Basis
        try:
            r_spot = await client.get(f"{BINANCE_API}/api/v3/ticker/price",
                                      params={"symbol": bsym})
            r_futures = await client.get(f"{BINANCE_FAPI}/fapi/v1/ticker/price",
                                         params={"symbol": bsym})
            if r_spot.status_code == 200 and r_futures.status_code == 200:
                spot_price = float(r_spot.json().get("price", 0))
                futures_price = float(r_futures.json().get("price", 0))
                if spot_price > 0:
                    basis_pct = ((futures_price - spot_price) / spot_price) * 100
                    self._basis_cache[symbol] = {
                        "spot_price": round(spot_price, 6),
                        "futures_price": round(futures_price, 6),
                        "basis_pct": round(basis_pct, 4),
                        "ts": now_iso,
                    }
        except Exception as e:
            logger.debug(f"FlowIntel basis {bsym}: {e}")

    # =============================
    # Background: Liquidation WS
    # =============================

    async def _liquidation_ws_loop(self):
        """Stream Binance forced liquidations."""
        while self.running:
            try:
                async with websockets.connect(BINANCE_LIQ_WS) as ws:
                    logger.info("FlowIntel v2: Binance liquidation WS connected")
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                            order = msg.get("o", {})
                            sym = order.get("s", "")
                            side = order.get("S", "")
                            qty = float(order.get("q", 0))
                            price = float(order.get("p", 0))
                            notional = qty * price
                            if notional < 100:
                                continue
                            mexc_sym = self._reverse_map.get(sym)
                            self._binance_liquidations.append({
                                "symbol": mexc_sym or sym,
                                "binance_symbol": sym,
                                "side": side,
                                "qty": qty,
                                "price": price,
                                "notional": round(notional, 2),
                                "ts": datetime.utcnow().isoformat(),
                            })
                        except Exception:
                            pass
            except Exception as e:
                if self.running:
                    logger.warning(f"FlowIntel v2: liq WS error: {e}, reconnecting...")
                    await asyncio.sleep(5)

    # =============================
    # Background: Derived calculations
    # =============================

    async def _recalc_derived_loop(self):
        """Recalculate liquidation levels, funding momentum, OI divergence every 2 min."""
        while self.running:
            try:
                for symbol in self.symbols:
                    # Liquidation levels
                    price = self.oft._last_prices.get(symbol, 0)
                    if price > 0:
                        levels = {}
                        for lev in LEVERAGE_LEVELS:
                            long_liq = round(price * (1 - 1 / lev + MAINTENANCE_MARGIN), 6)
                            short_liq = round(price * (1 + 1 / lev - MAINTENANCE_MARGIN), 6)
                            levels[f"{lev}x"] = {
                                "long_liq": long_liq,
                                "short_liq": short_liq,
                                "long_dist_pct": round((price - long_liq) / price * 100, 2),
                                "short_dist_pct": round((short_liq - price) / price * 100, 2),
                            }
                        self._liquidation_levels[symbol] = {
                            "price": price, "levels": levels,
                            "ts": datetime.utcnow().isoformat(),
                        }

                    # Funding momentum: track current funding rate from MEXC data
                    from app.core.market_data import market_data
                    try:
                        fr = await market_data.fetch_funding_rate(symbol)
                        if fr is not None:
                            self._funding_history[symbol].append({
                                "rate": fr,
                                "ts": datetime.utcnow().timestamp(),
                            })
                            self._compute_funding_momentum(symbol)
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(f"FlowIntel derived calc error: {e}")
            await asyncio.sleep(120)

    def _compute_funding_momentum(self, symbol: str):
        """Compute funding rate momentum from rolling history."""
        history = list(self._funding_history.get(symbol, []))
        if len(history) < 3:
            self._funding_momentum_cache[symbol] = {
                "current": history[-1]["rate"] if history else 0,
                "trend": "neutral", "acceleration": 0, "extreme": False,
            }
            return

        rates = [h["rate"] for h in history]
        current = rates[-1]
        avg = sum(rates) / len(rates)
        slope = (rates[-1] - rates[0]) / max(len(rates), 1)

        trend = "neutral"
        if slope > 0.001:
            trend = "rising"
        elif slope < -0.001:
            trend = "falling"

        extreme = abs(current) > 0.05

        self._funding_momentum_cache[symbol] = {
            "current": round(current, 5),
            "avg": round(avg, 5),
            "slope": round(slope, 6),
            "trend": trend,
            "extreme": extreme,
        }

    # =============================
    # OI/Price Divergence
    # =============================

    def _compute_oi_price_divergence(self, symbol: str) -> dict:
        """
        Compute OI vs Price divergence signal.
        OI up + Price up = trend continuation (bullish)
        OI up + Price down = new shorts (bearish)
        OI down + Price up = fake pump / short squeeze
        OI down + Price down = capitulation (potential reversal)
        """
        oi = self._oi_cache.get(symbol, {})
        if not oi:
            return {"signal": "neutral", "oi_change": 0, "interpretation": "no data"}

        oi_change = oi.get("change_pct", 0)

        # We need price change too — approximate from tick data
        trades = self.oft._trades.get(symbol, deque())
        if len(trades) < 20:
            return {"signal": "neutral", "oi_change": oi_change, "interpretation": "insufficient trades"}

        now = datetime.utcnow().timestamp()
        recent_prices = [p for ts, _, _, p in trades if ts >= now - 300]
        older_prices = [p for ts, _, _, p in trades if now - 1800 <= ts < now - 300]

        if not recent_prices or not older_prices:
            return {"signal": "neutral", "oi_change": oi_change, "interpretation": "insufficient price data"}

        price_recent = sum(recent_prices) / len(recent_prices)
        price_older = sum(older_prices) / len(older_prices)
        price_change_pct = ((price_recent - price_older) / max(price_older, 1e-8)) * 100

        oi_up = oi_change > 1.0
        oi_down = oi_change < -1.0
        price_up = price_change_pct > 0.1
        price_down = price_change_pct < -0.1

        if oi_up and price_up:
            return {"signal": "bullish_continuation", "oi_change": oi_change,
                    "price_change": round(price_change_pct, 2),
                    "interpretation": "New longs opening — trend continuation"}
        elif oi_up and price_down:
            return {"signal": "bearish_continuation", "oi_change": oi_change,
                    "price_change": round(price_change_pct, 2),
                    "interpretation": "New shorts opening — bearish pressure"}
        elif oi_down and price_up:
            return {"signal": "fake_pump", "oi_change": oi_change,
                    "price_change": round(price_change_pct, 2),
                    "interpretation": "Shorts closing / fake pump — caution longs"}
        elif oi_down and price_down:
            return {"signal": "capitulation", "oi_change": oi_change,
                    "price_change": round(price_change_pct, 2),
                    "interpretation": "Capitulation — potential reversal zone"}

        return {"signal": "neutral", "oi_change": oi_change,
                "price_change": round(price_change_pct, 2), "interpretation": "No clear signal"}

    # =============================
    # Smart Money Divergence
    # =============================

    def _compute_smart_money(self, symbol: str) -> dict:
        """
        Compare top traders (smart money) vs global crowd.
        When they diverge, follow smart money.
        """
        global_ls = self._binance_ls_cache.get(symbol, {})
        top_ls = self._top_traders_ls_cache.get(symbol, {})
        top_pos = self._top_traders_pos_cache.get(symbol, {})

        if not global_ls or not top_ls:
            return {"divergence": False, "smart_bias": "neutral", "crowd_bias": "neutral", "signal": "neutral"}

        global_ratio = global_ls.get("ratio", 1)
        top_ratio = top_ls.get("ratio", 1)
        top_pos_ratio = top_pos.get("ratio", 1) if top_pos else top_ratio

        # Determine biases
        crowd_bias = "long" if global_ratio > 1.2 else "short" if global_ratio < 0.8 else "neutral"
        smart_bias = "long" if top_ratio > 1.2 else "short" if top_ratio < 0.8 else "neutral"

        divergence = False
        signal = "neutral"

        # Smart money diverges from crowd
        if crowd_bias == "long" and smart_bias == "short":
            divergence = True
            signal = "smart_short"  # Smart money is short while crowd is long
        elif crowd_bias == "short" and smart_bias == "long":
            divergence = True
            signal = "smart_long"  # Smart money is long while crowd is short

        return {
            "divergence": divergence,
            "signal": signal,
            "smart_bias": smart_bias,
            "crowd_bias": crowd_bias,
            "global_ratio": global_ratio,
            "top_account_ratio": top_ratio,
            "top_position_ratio": top_pos_ratio,
        }

    # =============================
    # Public API
    # =============================

    def get_intelligence(self, symbol: str) -> dict:
        """SYNCHRONOUS — returns full flow intelligence for a symbol."""
        last_trade_ts = self.oft.get_last_trade_ts(symbol)
        now = datetime.utcnow().timestamp()
        is_stale = (now - last_trade_ts) > 60 if last_trade_ts > 0 else True

        # OrderFlow data
        cvd = self.oft.get_cvd_divergence_v2(symbol) if not is_stale else {"divergence": 0, "signal": "neutral", "confidence": 0}
        whale = self.oft.get_whale_activity(symbol) if not is_stale else {"whale_pressure": 0, "whale_count": 0, "whale_buy_vol": 0, "whale_sell_vol": 0, "threshold": 0}
        deltas = self.oft.get_multi_delta(symbol)
        aggressive = self.oft.get_aggressive_ratio(symbol) if not is_stale else {"ratio": 1.0, "buy_count": 0, "sell_count": 0, "buy_vol": 0, "sell_vol": 0, "total_trades": 0, "total_vol": 0}

        # Microstructure data
        micro = self.micro.get_full_report(symbol) if not is_stale else {
            "vpin": {"vpin": 0.5, "bias": "neutral", "bucket_count": 0, "confidence": 0},
            "sweep": {"sweep_detected": False, "sweep_direction": "none", "sweep_volume": 0, "sweep_levels": 0, "sweep_intensity": 0},
            "tape_speed": {"tps_current": 0, "tps_avg": 0, "acceleration": 1.0, "intensity": "low", "count_30s": 0, "count_5m": 0},
            "imbalance": {"max_run": 0, "run_direction": "none", "imbalance": 0, "run_volume": 0},
            "is_stale": True,
        }

        # Binance caches
        ls_ratio = self._binance_ls_cache.get(symbol, {})
        top_traders = self._top_traders_ls_cache.get(symbol, {})
        top_positions = self._top_traders_pos_cache.get(symbol, {})
        taker_volume = self._taker_volume_cache.get(symbol, {})
        oi_data = self._oi_cache.get(symbol, {})
        basis = self._basis_cache.get(symbol, {})
        funding_mom = self._funding_momentum_cache.get(symbol, {})
        liq_levels = self._liquidation_levels.get(symbol, {})
        recent_liqs = self._get_recent_liqs(symbol)

        # Derived signals
        oi_divergence = self._compute_oi_price_divergence(symbol)
        smart_money = self._compute_smart_money(symbol)

        # Composite score
        flow_score, flow_bias, flow_signals = self._compute_flow_score(
            symbol, cvd, whale, deltas, aggressive, micro,
            ls_ratio, top_traders, taker_volume, oi_divergence,
            smart_money, basis, funding_mom, is_stale
        )

        return {
            # OrderFlow
            "cvd": cvd,
            "whale_trades": whale,
            "deltas": deltas,
            "aggressive_ratio": aggressive,
            # Microstructure
            "microstructure": micro,
            # Binance data
            "long_short_ratio": ls_ratio,
            "top_traders_ls": top_traders,
            "top_traders_positions": top_positions,
            "taker_volume": taker_volume,
            "oi_data": oi_data,
            "oi_divergence": oi_divergence,
            "basis": basis,
            "funding_momentum": funding_mom,
            "smart_money": smart_money,
            # Liquidations
            "liquidation_levels": liq_levels,
            "recent_liquidations": recent_liqs,
            # Composite
            "flow_score": flow_score,
            "flow_bias": flow_bias,
            "flow_signals": flow_signals,
            "is_stale": is_stale,
        }

    def _get_recent_liqs(self, symbol: str, limit: int = 10) -> list:
        result = []
        bsym = _mexc_to_binance_symbol(symbol)
        for liq in reversed(self._binance_liquidations):
            if liq["symbol"] == symbol or liq["binance_symbol"] == bsym:
                result.append(liq)
                if len(result) >= limit:
                    break
        return result

    def _compute_flow_score(self, symbol, cvd, whale, deltas, aggressive, micro,
                            ls_ratio, top_traders, taker_vol, oi_div,
                            smart_money, basis, funding_mom, is_stale):
        """Compute composite flow score (0-100), bias, signals."""
        score = 50
        signals = []

        if is_stale:
            return score, "neutral", ["Flow data stale"]

        # --- CVD component (±12 pts) ---
        cvd_sig = cvd.get("signal", "neutral")
        cvd_conf = cvd.get("confidence", 0)
        if "bullish" in cvd_sig:
            pts = int(12 * cvd_conf)
            score += pts
            signals.append(f"CVD {cvd_sig} +{pts}pts")
        elif "bearish" in cvd_sig:
            pts = int(12 * cvd_conf)
            score -= pts
            signals.append(f"CVD {cvd_sig} -{pts}pts")

        # --- Whale component (±10 pts) ---
        wp = whale.get("whale_pressure", 0)
        if abs(wp) > 0.3:
            pts = int(10 * wp)
            score += pts
            signals.append(f"Whale {'buy' if wp > 0 else 'sell'} {wp:.2f} {pts:+d}pts")

        # --- VPIN component (±10 pts) ---
        vpin_data = micro.get("vpin", {})
        vpin_val = vpin_data.get("vpin", 0.5)
        vpin_bias = vpin_data.get("bias", "neutral")
        vpin_conf = vpin_data.get("confidence", 0)
        if vpin_val > 0.65 and vpin_conf > 0.3:
            pts = int(10 * (vpin_val - 0.5) * 2 * vpin_conf)
            if vpin_bias == "buy":
                score += pts
                signals.append(f"VPIN {vpin_val:.2f} informed buy +{pts}pts")
            else:
                score -= pts
                signals.append(f"VPIN {vpin_val:.2f} informed sell -{pts}pts")

        # --- Sweep component (±8 pts) ---
        sweep = micro.get("sweep", {})
        if sweep.get("sweep_detected"):
            intensity = sweep.get("sweep_intensity", 0)
            pts = int(8 * intensity)
            if sweep["sweep_direction"] == "buy":
                score += pts
                signals.append(f"BUY SWEEP detected +{pts}pts")
            else:
                score -= pts
                signals.append(f"SELL SWEEP detected -{pts}pts")

        # --- Tape speed component (±5 pts) ---
        tape = micro.get("tape_speed", {})
        accel = tape.get("acceleration", 1)
        if accel > 3.0:
            # High intensity — amplify existing bias
            d5m = deltas.get("5m", {})
            if d5m.get("ratio", 0.5) > 0.55:
                score += 5
                signals.append(f"Tape speed {accel:.1f}x + buy flow +5pts")
            elif d5m.get("ratio", 0.5) < 0.45:
                score -= 5
                signals.append(f"Tape speed {accel:.1f}x + sell flow -5pts")
        elif accel < 0.3:
            # Dead tape — reduce conviction
            score = int(50 + (score - 50) * 0.7)
            signals.append(f"Dead tape {accel:.1f}x — conviction reduced")

        # --- Taker volume (±5 pts) ---
        taker = taker_vol.get("ratio", 1)
        if taker > 1.2:
            score += 5
            signals.append(f"Taker buy dominant {taker:.2f} +5pts")
        elif taker < 0.8:
            score -= 5
            signals.append(f"Taker sell dominant {taker:.2f} -5pts")

        # --- Smart money (±8 pts) ---
        if smart_money.get("divergence"):
            sm_signal = smart_money.get("signal", "neutral")
            if sm_signal == "smart_long":
                score += 8
                signals.append(f"Smart money LONG vs crowd short +8pts")
            elif sm_signal == "smart_short":
                score -= 8
                signals.append(f"Smart money SHORT vs crowd long -8pts")

        # --- OI/Price divergence (±6 pts) ---
        oi_sig = oi_div.get("signal", "neutral")
        if oi_sig == "bullish_continuation":
            score += 6
            signals.append(f"OI confirms bullish +6pts")
        elif oi_sig == "bearish_continuation":
            score -= 6
            signals.append(f"OI confirms bearish -6pts")
        elif oi_sig == "fake_pump":
            score -= 4
            signals.append(f"OI divergence: fake pump -4pts")
        elif oi_sig == "capitulation":
            score += 3
            signals.append(f"OI capitulation: potential reversal +3pts")

        # --- Basis (±3 pts) ---
        basis_pct = basis.get("basis_pct", 0)
        if basis_pct > 0.1:
            score += 3
            signals.append(f"Futures premium {basis_pct:.3f}% (bullish) +3pts")
        elif basis_pct < -0.05:
            score -= 3
            signals.append(f"Futures discount {basis_pct:.3f}% (bearish) -3pts")

        # --- Funding momentum (±4 pts) ---
        fm = funding_mom
        if fm.get("extreme"):
            if fm.get("current", 0) > 0.05:
                score -= 4
                signals.append(f"Extreme positive funding {fm['current']:.4f} (contrarian short) -4pts")
            elif fm.get("current", 0) < -0.03:
                score += 4
                signals.append(f"Extreme negative funding {fm['current']:.4f} (contrarian long) +4pts")
        elif fm.get("trend") == "rising" and fm.get("slope", 0) > 0.002:
            score -= 2
            signals.append(f"Funding rising fast (longs crowded) -2pts")
        elif fm.get("trend") == "falling" and fm.get("slope", 0) < -0.002:
            score += 2
            signals.append(f"Funding falling fast (shorts crowded) +2pts")

        # --- L/S contrarian (±3 pts) ---
        if ls_ratio:
            ls_val = ls_ratio.get("ratio", 1)
            if ls_val > 2.5:
                score -= 3
                signals.append(f"Crowd extreme long {ls_val:.2f} -3pts")
            elif ls_val < 0.4:
                score += 3
                signals.append(f"Crowd extreme short {ls_val:.2f} +3pts")

        score = max(0, min(100, score))
        bias = "bullish" if score >= 60 else "bearish" if score <= 40 else "neutral"
        return score, bias, signals

    def get_all_intelligence(self) -> dict:
        result = {}
        for symbol in self.symbols:
            result[symbol] = self.get_intelligence(symbol)
        return result
