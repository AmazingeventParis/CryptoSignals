"""
Order Flow Tracker : Analyse les trades en temps reel via WebSocket MEXC push.deal.
Calcule le delta roulant (buy_vol - sell_vol) sur 1m, 5m, 15m.
Detecte les divergences CVD (Cumulative Volume Delta) vs prix.
V4 only.
"""
import asyncio
import json
import logging
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

        # Per-symbol rolling trade data: deque of (timestamp, volume, is_buy)
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
                self._trades[symbol].append((ts, volume, is_buy))
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
        for ts, vol, is_buy in trades:
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
            return {"divergence": 0, "signal": "neutral"}

        now = datetime.utcnow().timestamp()

        # Compare 5m windows: recent vs previous
        mid = now - 300
        cutoff = now - 600

        recent_delta = 0
        prev_delta = 0
        recent_prices = []
        prev_prices = []

        for ts, vol, is_buy in trades:
            if ts >= mid:
                recent_delta += vol if is_buy else -vol
                if symbol in self._last_prices:
                    recent_prices.append(self._last_prices[symbol])
            elif ts >= cutoff:
                prev_delta += vol if is_buy else -vol

        if not recent_prices:
            return {"divergence": 0, "signal": "neutral"}

        # Simplified CVD divergence detection
        delta_change = recent_delta - prev_delta

        # Price direction (we only have current price, approximate)
        current_price = self._last_prices.get(symbol, 0)
        if current_price <= 0:
            return {"divergence": 0, "signal": "neutral"}

        # Bearish divergence: delta falling but price stable/rising
        if delta_change < -100 and recent_delta < 0:
            return {"divergence": -0.5, "signal": "bearish_divergence"}

        # Bullish divergence: delta rising but price stable/falling
        if delta_change > 100 and recent_delta > 0:
            return {"divergence": 0.5, "signal": "bullish_divergence"}

        return {"divergence": 0, "signal": "neutral"}

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
