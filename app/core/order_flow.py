"""
Order Flow Tracker : Analyse les trades en temps reel via WebSocket MEXC push.deal.
Calcule le delta roulant (buy_vol - sell_vol) sur 1m, 5m, 15m.
Detecte les divergences CVD (Cumulative Volume Delta) vs prix.
Whale detection + aggressive ratio.
V4 only.
"""
import asyncio
import json
import logging
import statistics
from collections import deque
from datetime import datetime

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://contract.mexc.com/edge"


class OrderFlowTracker:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.running = False
        self._ws_tasks: dict[str, asyncio.Task] = {}

        # Per-symbol rolling trade data: deque of (timestamp, volume, is_buy, price)
        self._trades: dict[str, deque] = {s: deque(maxlen=5000) for s in symbols}

        # Cached deltas
        self._delta_cache: dict[str, dict] = {}
        self._last_prices: dict[str, float] = {}

    async def start(self):
        self.running = True
        for symbol in self.symbols:
            self._ws_tasks[symbol] = asyncio.create_task(self._ws_stream(symbol))
        logger.info(f"OrderFlowTracker started for {len(self.symbols)} symbols")

    async def stop(self):
        self.running = False
        for task in self._ws_tasks.values():
            task.cancel()
        self._ws_tasks.clear()
        logger.info("OrderFlowTracker stopped")

    async def _ws_stream(self, symbol: str):
        mexc_symbol = symbol.split(":")[0].replace("-", "_").replace("/", "_")

        while self.running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    await ws.send(json.dumps({
                        "method": "sub.deal",
                        "param": {"symbol": mexc_symbol}
                    }))

                    ping_task = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw in ws:
                            if not self.running:
                                break
                            msg = json.loads(raw)
                            if msg.get("channel") == "push.deal" and msg.get("data"):
                                deals = msg["data"]
                                if isinstance(deals, list):
                                    for deal in deals:
                                        self._process_deal(symbol, deal)
                                else:
                                    self._process_deal(symbol, deals)
                    finally:
                        ping_task.cancel()

            except Exception as e:
                if self.running:
                    logger.warning(f"OrderFlow WS {symbol} error: {e}, reconnecting...")
                    await asyncio.sleep(5)

    async def _keepalive(self, ws):
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send('{"method":"ping"}')
            except Exception:
                break

    def _process_deal(self, symbol: str, deal: dict):
        try:
            price = float(deal.get("p", 0))
            volume = float(deal.get("v", 0) or deal.get("q", 0))
            # MEXC: T=1 is buy (taker buy), T=2 is sell
            is_buy = deal.get("T", 0) == 1
            ts = float(deal.get("t", 0)) / 1000 if deal.get("t") else datetime.utcnow().timestamp()

            if price > 0 and volume > 0:
                self._trades[symbol].append((ts, volume, is_buy, price))
                self._last_prices[symbol] = price
        except Exception:
            pass

    def get_delta(self, symbol: str, window_seconds: int = 60) -> dict:
        """Get buy/sell volume delta for a rolling window."""
        now = datetime.utcnow().timestamp()
        cutoff = now - window_seconds
        trades = self._trades.get(symbol, deque())

        buy_vol = 0
        sell_vol = 0
        for ts, vol, is_buy, _price in trades:
            if ts >= cutoff:
                if is_buy:
                    buy_vol += vol
                else:
                    sell_vol += vol

        total = buy_vol + sell_vol
        delta = buy_vol - sell_vol
        ratio = buy_vol / max(total, 1)

        return {
            "buy_vol": round(buy_vol, 2),
            "sell_vol": round(sell_vol, 2),
            "delta": round(delta, 2),
            "ratio": round(ratio, 3),
            "total": round(total, 2),
        }

    def get_multi_delta(self, symbol: str) -> dict:
        """Get deltas for 1m, 5m, 15m windows."""
        return {
            "1m": self.get_delta(symbol, 60),
            "5m": self.get_delta(symbol, 300),
            "15m": self.get_delta(symbol, 900),
        }

    def get_cvd_divergence(self, symbol: str) -> dict:
        """
        Detect CVD divergence: price going up but CVD going down (or vice versa).
        Returns signal strength (-1.0 to 1.0).
        """
        trades = self._trades.get(symbol, deque())
        if len(trades) < 20:
            return {"divergence": 0, "signal": "neutral", "confidence": 0}

        now = datetime.utcnow().timestamp()

        # Compare 5m windows: recent vs previous
        mid = now - 300
        cutoff = now - 600

        recent_delta = 0
        prev_delta = 0
        recent_prices = []
        prev_prices = []

        for ts, vol, is_buy, price in trades:
            if ts >= mid:
                recent_delta += vol if is_buy else -vol
                recent_prices.append(price)
            elif ts >= cutoff:
                prev_delta += vol if is_buy else -vol
                prev_prices.append(price)

        if not recent_prices or not prev_prices:
            return {"divergence": 0, "signal": "neutral", "confidence": 0}

        delta_change = recent_delta - prev_delta
        price_change = (recent_prices[-1] - prev_prices[0]) / max(prev_prices[0], 1e-8)

        # Bearish divergence: price rising but CVD falling
        if price_change > 0.001 and delta_change < 0 and recent_delta < 0:
            confidence = min(1.0, abs(delta_change) / 500)
            return {"divergence": -confidence, "signal": "bearish_divergence", "confidence": confidence}

        # Bullish divergence: price falling but CVD rising
        if price_change < -0.001 and delta_change > 0 and recent_delta > 0:
            confidence = min(1.0, abs(delta_change) / 500)
            return {"divergence": confidence, "signal": "bullish_divergence", "confidence": confidence}

        return {"divergence": 0, "signal": "neutral", "confidence": 0}

    def get_cvd_divergence_v2(self, symbol: str) -> dict:
        """
        Enhanced CVD divergence: compare actual price movement vs CVD over two 5min windows.
        Returns signal + confidence (0-1).
        """
        trades = self._trades.get(symbol, deque())
        if len(trades) < 30:
            return {"divergence": 0, "signal": "neutral", "confidence": 0}

        now = datetime.utcnow().timestamp()
        w1_start, w1_end = now - 600, now - 300
        w2_start, w2_end = now - 300, now

        w1_delta, w2_delta = 0, 0
        w1_prices, w2_prices = [], []

        for ts, vol, is_buy, price in trades:
            if w1_start <= ts < w1_end:
                w1_delta += vol if is_buy else -vol
                w1_prices.append(price)
            elif w2_start <= ts <= w2_end:
                w2_delta += vol if is_buy else -vol
                w2_prices.append(price)

        if not w1_prices or not w2_prices:
            return {"divergence": 0, "signal": "neutral", "confidence": 0}

        # Price direction
        price_w1_avg = sum(w1_prices) / len(w1_prices)
        price_w2_avg = sum(w2_prices) / len(w2_prices)
        price_pct = (price_w2_avg - price_w1_avg) / max(price_w1_avg, 1e-8)

        # CVD direction
        cvd_change = w2_delta - w1_delta

        # Bearish divergence: price up but CVD declining
        if price_pct > 0.0005 and cvd_change < 0:
            confidence = min(1.0, abs(cvd_change) / 300)
            return {"divergence": -confidence, "signal": "bearish_divergence", "confidence": confidence}

        # Bullish divergence: price down but CVD rising
        if price_pct < -0.0005 and cvd_change > 0:
            confidence = min(1.0, abs(cvd_change) / 300)
            return {"divergence": confidence, "signal": "bullish_divergence", "confidence": confidence}

        # CVD confirming price direction
        if price_pct > 0.0005 and cvd_change > 0:
            confidence = min(1.0, abs(cvd_change) / 300)
            return {"divergence": 0, "signal": "bullish_confirmation", "confidence": confidence}
        if price_pct < -0.0005 and cvd_change < 0:
            confidence = min(1.0, abs(cvd_change) / 300)
            return {"divergence": 0, "signal": "bearish_confirmation", "confidence": confidence}

        return {"divergence": 0, "signal": "neutral", "confidence": 0}

    def get_whale_activity(self, symbol: str, window: int = 300, multiplier: float = 3.0) -> dict:
        """
        Detect whale trades: trades > multiplier x median volume.
        Returns whale_pressure (-1 to +1), whale_count, whale_volume.
        """
        now = datetime.utcnow().timestamp()
        cutoff = now - window
        trades = self._trades.get(symbol, deque())

        recent = [(ts, vol, is_buy, price) for ts, vol, is_buy, price in trades if ts >= cutoff]
        if len(recent) < 10:
            return {"whale_pressure": 0, "whale_count": 0, "whale_buy_vol": 0, "whale_sell_vol": 0, "threshold": 0}

        volumes = [vol for _, vol, _, _ in recent]
        median_vol = statistics.median(volumes)
        threshold = median_vol * multiplier

        whale_buy_vol = 0
        whale_sell_vol = 0
        whale_count = 0

        for ts, vol, is_buy, price in recent:
            if vol >= threshold:
                whale_count += 1
                if is_buy:
                    whale_buy_vol += vol
                else:
                    whale_sell_vol += vol

        total_whale = whale_buy_vol + whale_sell_vol
        if total_whale == 0:
            pressure = 0
        else:
            pressure = (whale_buy_vol - whale_sell_vol) / total_whale

        return {
            "whale_pressure": round(pressure, 3),
            "whale_count": whale_count,
            "whale_buy_vol": round(whale_buy_vol, 2),
            "whale_sell_vol": round(whale_sell_vol, 2),
            "threshold": round(threshold, 4),
        }

    def get_aggressive_ratio(self, symbol: str, window: int = 60) -> dict:
        """
        Ratio taker buy / taker sell over window.
        >1 = buyers aggressive, <1 = sellers aggressive.
        """
        now = datetime.utcnow().timestamp()
        cutoff = now - window
        trades = self._trades.get(symbol, deque())

        buy_count = 0
        sell_count = 0
        buy_vol = 0
        sell_vol = 0

        for ts, vol, is_buy, _price in trades:
            if ts >= cutoff:
                if is_buy:
                    buy_count += 1
                    buy_vol += vol
                else:
                    sell_count += 1
                    sell_vol += vol

        total_count = buy_count + sell_count
        total_vol = buy_vol + sell_vol

        return {
            "ratio": round(buy_vol / max(sell_vol, 1e-8), 3),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "buy_vol": round(buy_vol, 2),
            "sell_vol": round(sell_vol, 2),
            "total_trades": total_count,
            "total_vol": round(total_vol, 2),
        }

    def get_last_trade_ts(self, symbol: str) -> float:
        """Return timestamp of last trade, 0 if none."""
        trades = self._trades.get(symbol, deque())
        if not trades:
            return 0
        return trades[-1][0]

    def get_flow_score(self, symbol: str) -> tuple[float, str]:
        """
        Get a normalized order flow score (0.0 to 1.0) for tradeability.
        Replaces the simple bid/ask ratio check.
        """
        delta_5m = self.get_delta(symbol, 300)
        ratio = delta_5m["ratio"]

        if delta_5m["total"] < 10:
            return 0.5, "Order flow: volume insuffisant"

        # Strong directional flow
        if ratio > 0.65:
            return 1.0, f"Order flow: pression acheteuse forte (ratio {ratio:.2f})"
        elif ratio < 0.35:
            return 1.0, f"Order flow: pression vendeuse forte (ratio {ratio:.2f})"
        elif 0.45 <= ratio <= 0.55:
            return 0.5, f"Order flow: equilibre (ratio {ratio:.2f})"
        else:
            return 0.7, f"Order flow: {ratio:.2f}"
