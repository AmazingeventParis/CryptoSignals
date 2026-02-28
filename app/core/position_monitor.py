"""
Position Monitor : Surveille les positions en TEMPS REEL via WebSocket MEXC.
- WebSocket push.deal = chaque trade sur MEXC -> reaction instantanee
- Detecte TP1/TP2/TP3/SL via le prix en temps reel
- Deplace le SL (breakeven apres TP1, TP1 price apres TP2)
- Polling lent (30s) en backup pour confirmer via fetch_positions
Accepte bot_version pour supporter V1 et V2 en parallele.
"""
import asyncio
import json
import logging
from datetime import datetime

import websockets

from app.core.market_data import market_data
from app.database import (
    get_active_positions,
    update_position,
    close_position,
    insert_trade,
    insert_active_position,
)


async def send_trade_update(*args, **kwargs):
    """Stub - Telegram disabled"""
    pass

logger = logging.getLogger(__name__)

BACKUP_POLL_INTERVAL = 30
WS_URL = "wss://contract.mexc.com/edge"


class PositionMonitor:
    def __init__(self, bot_version="V2", settings=None):
        self.bot_version = bot_version
        self.settings = settings
        self.running = False
        self._positions: dict[int, dict] = {}
        self._ws_tasks: dict[str, asyncio.Task] = {}
        self._processing: set = set()
        self._on_close_callbacks: list = []

    def set_adaptive_learner(self, learner):
        self._adaptive_learner = learner

    def _get_adaptive_learner(self):
        return getattr(self, "_adaptive_learner", None)

    def add_on_close_callback(self, callback):
        self._on_close_callbacks.append(callback)

    async def start(self):
        self.running = True
        logger.info(f"PositionMonitor [{self.bot_version}] demarre (WebSocket temps reel)")

        await self._reload_positions()

        while self.running:
            try:
                await self._backup_check()
            except Exception as e:
                logger.error(f"[{self.bot_version}] Erreur backup monitor: {e}", exc_info=True)
            await asyncio.sleep(BACKUP_POLL_INTERVAL)

    async def stop(self):
        self.running = False
        for symbol, task in self._ws_tasks.items():
            task.cancel()
        self._ws_tasks.clear()
        logger.info(f"PositionMonitor [{self.bot_version}] arrete")

    async def register_trade(self, signal: dict, result: dict) -> int | None:
        if not result.get("success"):
            return None

        for p in self._positions.values():
            if p["symbol"] == signal["symbol"] and p["direction"] == signal["direction"] and p.get("state") != "closed":
                logger.warning(f"[{self.bot_version}] Position deja active pour {signal['symbol']} {signal['direction']}")
                return None

        pos_data = {
            "signal_id": signal.get("id"),
            "symbol": signal["symbol"],
            "direction": signal["direction"],
            "entry_price": result["actual_entry_price"],
            "stop_loss": signal["stop_loss"],
            "tp1": signal["tp1"],
            "tp2": signal["tp2"],
            "tp3": signal["tp3"],
            "original_quantity": result["quantity"],
            "remaining_quantity": result["quantity"],
            "tp1_close_pct": signal.get("tp1_close_pct", 40),
            "tp2_close_pct": signal.get("tp2_close_pct", 30),
            "tp3_close_pct": signal.get("tp3_close_pct", 30),
            "leverage": signal.get("leverage", 10),
            "position_size_usd": result["position_size_usd"],
            "margin_required": result["margin_required"],
            "sl_order_id": result.get("sl_order_id"),
            "tp1_order_id": result["tp_order_ids"][0] if len(result.get("tp_order_ids", [])) > 0 else None,
            "tp2_order_id": result["tp_order_ids"][1] if len(result.get("tp_order_ids", [])) > 1 else None,
            "tp3_order_id": result["tp_order_ids"][2] if len(result.get("tp_order_ids", [])) > 2 else None,
            "entry_order_id": result.get("entry_order_id"),
            "mode": signal.get("mode"),
            "setup_type": signal.get("setup_type", "unknown"),
            "bot_version": self.bot_version,
        }

        # V4 only: tracking metrics + snapshots for adaptive learning
        if self.bot_version == "V4":
            pos_data["_max_profit_usd"] = 0.0
            pos_data["_max_drawdown_usd"] = 0.0
            pos_data["_original_sl"] = signal["stop_loss"]
            pos_data["_entry_atr"] = signal.get("_entry_atr", 0)
            pos_data["_indicator_snapshot"] = signal.get("_indicator_snapshot", {})
            pos_data["_regime_snapshot"] = signal.get("_regime_snapshot", {})
            pos_data["_scores_snapshot"] = signal.get("_scores_snapshot", {})
            pos_data["_candle_pattern"] = signal.get("candle_pattern", "none")

        pos_id = await insert_active_position(pos_data)
        pos_data["id"] = pos_id
        pos_data["state"] = "active"
        pos_data["tp1_hit"] = 0
        pos_data["tp2_hit"] = 0
        pos_data["tp3_hit"] = 0
        pos_data["sl_hit"] = 0

        self._positions[pos_id] = pos_data
        logger.info(f"[{self.bot_version}] Position enregistree: {signal['symbol']} {signal['direction']} qty={result['quantity']} (id={pos_id})")

        self._ensure_ws(signal["symbol"])
        return pos_id

    # --- WebSocket temps reel ---

    def _ensure_ws(self, symbol: str):
        if symbol in self._ws_tasks and not self._ws_tasks[symbol].done():
            return
        self._ws_tasks[symbol] = asyncio.create_task(self._ws_price_stream(symbol))
        logger.info(f"[{self.bot_version}] WS prix temps reel demarre: {symbol}")

    async def _ws_price_stream(self, symbol: str):
        mexc_symbol = symbol.split(":")[0].replace("-", "_").replace("/", "_")

        while self.running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    await ws.send(json.dumps({
                        "method": "sub.deal",
                        "param": {"symbol": mexc_symbol}
                    }))
                    logger.info(f"[{self.bot_version}] WS connecte: {symbol} (sub.deal)")

                    ping_task = asyncio.create_task(self._ws_keepalive(ws))

                    try:
                        async for raw in ws:
                            if not self.running:
                                break
                            msg = json.loads(raw)
                            if msg.get("channel") == "push.deal" and msg.get("data"):
                                deals = msg["data"]
                                if isinstance(deals, list):
                                    last_deal = deals[-1] if deals else {}
                                else:
                                    last_deal = deals
                                price = float(last_deal.get("p", 0))
                                if price > 0:
                                    await self._on_price_tick(symbol, price)
                    finally:
                        ping_task.cancel()

            except (websockets.ConnectionClosed, Exception) as e:
                if self.running:
                    logger.warning(f"[{self.bot_version}] WS {symbol} deconnecte: {e}, reconnexion dans 3s")
                    await asyncio.sleep(3)

            if not self._has_active_positions(symbol):
                logger.info(f"[{self.bot_version}] WS {symbol} arrete: plus de positions actives")
                break

    async def _ws_keepalive(self, ws):
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send('{"method":"ping"}')
            except Exception:
                break

    def _has_active_positions(self, symbol: str) -> bool:
        return any(
            p["symbol"] == symbol and p.get("state") != "closed"
            for p in self._positions.values()
        )

    async def _on_price_tick(self, symbol: str, price: float):
        for pos_id, pos in list(self._positions.items()):
            if pos["symbol"] != symbol or pos.get("state") == "closed":
                continue
            if pos_id in self._processing:
                continue

            # --- V4 only: Track max profit / max drawdown + stale timeout + quick exit ---
            if self.bot_version == "V4":
                current_pnl_track = self._calc_unrealized_pnl(pos, price)
                if current_pnl_track > pos.get("_max_profit_usd", 0):
                    pos["_max_profit_usd"] = current_pnl_track
                if current_pnl_track < pos.get("_max_drawdown_usd", 0):
                    pos["_max_drawdown_usd"] = current_pnl_track

                # V4: Quick exit — after N seconds, if profit > fees*mult → close
                if self._check_quick_exit(pos, current_pnl_track):
                    self._processing.add(pos_id)
                    try:
                        await self._handle_quick_exit(pos, price, current_pnl_track)
                    finally:
                        self._processing.discard(pos_id)
                    continue

                if self._check_stale_position(pos, current_pnl_track):
                    self._processing.add(pos_id)
                    try:
                        await self._handle_stale_close(pos, price, current_pnl_track)
                    finally:
                        self._processing.discard(pos_id)
                    continue

                # V4: Profit giveback protection — secure gains when trade reverses
                if self._check_profit_giveback(pos, current_pnl_track):
                    self._processing.add(pos_id)
                    try:
                        await self._handle_profit_giveback_close(pos, price, current_pnl_track)
                    finally:
                        self._processing.discard(pos_id)
                    continue

            direction = pos["direction"]

            # --- Quick profit / max loss check (V3) ---
            min_profit = self._get_min_profit_usd(pos)
            if min_profit > 0:
                current_pnl = self._calc_unrealized_pnl(pos, price)
                if current_pnl >= min_profit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_min_profit_close(pos, price, current_pnl)
                    finally:
                        self._processing.discard(pos_id)
                    continue
                max_loss = self._get_max_loss_usd(pos)
                if max_loss > 0 and current_pnl <= -max_loss:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_max_loss_close(pos, price, current_pnl)
                    finally:
                        self._processing.discard(pos_id)
                    continue

            # TP1
            if not pos["tp1_hit"]:
                # --- Early profit protection (avant TP1) ---
                await self._early_profit_protection(pos, price, direction)

                tp1_hit = (price >= pos["tp1"]) if direction == "long" else (price <= pos["tp1"])
                if tp1_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_tp1_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

                sl_hit = (price <= pos["stop_loss"]) if direction == "long" else (price >= pos["stop_loss"])
                if sl_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_sl_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

            # TP2
            elif not pos["tp2_hit"]:
                tp2_hit = (price >= pos["tp2"]) if direction == "long" else (price <= pos["tp2"])
                if tp2_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_tp2_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

                sl_hit = (price <= pos["stop_loss"]) if direction == "long" else (price >= pos["stop_loss"])
                if sl_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_sl_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

            # TP3
            else:
                tp3_hit = (price >= pos["tp3"]) if direction == "long" else (price <= pos["tp3"])
                if tp3_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_tp3_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

                sl_hit = (price <= pos["stop_loss"]) if direction == "long" else (price >= pos["stop_loss"])
                if sl_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_sl_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

    # --- Backup polling ---

    async def _backup_check(self):
        await self._reload_positions()
        # V4 only: Dynamic SL adjustment based on current volatility
        if self.bot_version == "V4":
            await self._dynamic_sl_adjust()

    async def _reload_positions(self):
        positions = await get_active_positions(bot_version=self.bot_version)
        for pos in positions:
            # V4 only: Preserve in-memory tracking fields from existing positions
            if self.bot_version == "V4":
                existing = self._positions.get(pos["id"])
                if existing:
                    for key in ("_max_profit_usd", "_max_drawdown_usd", "_original_sl",
                                "_entry_atr", "_indicator_snapshot", "_regime_snapshot", "_scores_snapshot",
                                "_candle_pattern"):
                        if key in existing:
                            pos[key] = existing[key]
            self._positions[pos["id"]] = pos
            self._ensure_ws(pos["symbol"])

        active_ids = {p["id"] for p in positions}
        for pid in list(self._positions.keys()):
            if pid not in active_ids:
                del self._positions[pid]

    async def _dynamic_sl_adjust(self):
        """Ajuste dynamiquement le SL si la volatilite augmente (avant TP1 seulement).
        V4: desactive - max_loss_usd cap remplace le widening dynamique."""
        if self.bot_version == "V4":
            return
        for pos_id, pos in list(self._positions.items()):
            if pos.get("state") == "closed" or pos.get("tp1_hit"):
                continue

            entry_atr = pos.get("_entry_atr", 0)
            original_sl = pos.get("_original_sl", 0)
            if entry_atr <= 0 or original_sl <= 0:
                continue

            # Fetch current ATR
            try:
                from app.core.indicators import atr as calc_atr
                df = await market_data.fetch_ohlcv(pos["symbol"], "5m", limit=30)
                if df.empty or len(df) < 14:
                    continue
                current_atr_series = calc_atr(df, 14)
                current_atr = current_atr_series.iloc[-1]
                if current_atr != current_atr:  # NaN check
                    continue
            except Exception:
                continue

            atr_ratio = current_atr / entry_atr if entry_atr > 0 else 1.0
            if atr_ratio <= 1.5:
                continue

            # Widen SL proportionally (cap at 2x original distance)
            entry_price = pos["entry_price"]
            original_distance = abs(entry_price - original_sl)
            new_distance = original_distance * min(atr_ratio, 2.0)

            if pos["direction"] == "long":
                new_sl = round(entry_price - new_distance, 8)
                if new_sl < pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    await update_position(pos_id, {"stop_loss": new_sl})
                    logger.info(
                        f"[{self.bot_version}] DYNAMIC SL {pos['symbol']} "
                        f"widened to {new_sl} (ATR ratio {atr_ratio:.2f})"
                    )
            else:
                new_sl = round(entry_price + new_distance, 8)
                if new_sl > pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    await update_position(pos_id, {"stop_loss": new_sl})
                    logger.info(
                        f"[{self.bot_version}] DYNAMIC SL {pos['symbol']} "
                        f"widened to {new_sl} (ATR ratio {atr_ratio:.2f})"
                    )

    # --- Handlers TP/SL ---

    async def _handle_tp1_hit(self, pos: dict):
        symbol = pos["symbol"]
        # V4: Real breakeven includes fees, not just entry price
        be_price = self._fee_adjusted_breakeven(pos)
        logger.info(f"[{self.bot_version}] TP1 HIT {symbol} - SL -> breakeven ({be_price})")

        await self._cancel_order_safe(pos["sl_order_id"], symbol)

        qty_remaining = round(pos["original_quantity"] * (1 - pos["tp1_close_pct"] / 100), 6)
        new_sl_id = await self._place_new_sl(symbol, pos["direction"], qty_remaining, be_price)

        await update_position(pos["id"], {
            "tp1_hit": 1,
            "sl_order_id": new_sl_id,
            "stop_loss": be_price,
            "remaining_quantity": qty_remaining,
            "state": "breakeven",
        })

        pos["tp1_hit"] = 1
        pos["sl_order_id"] = new_sl_id
        pos["stop_loss"] = be_price
        pos["remaining_quantity"] = qty_remaining
        pos["state"] = "breakeven"

        dec = self._get_decimals(be_price)
        await send_trade_update(
            symbol, "tp1_hit",
            f"TP1 touche ! {pos['tp1_close_pct']}% ferme\nSL -> breakeven @ {be_price:.{dec}f}"
        )

    async def _handle_tp2_hit(self, pos: dict):
        symbol = pos["symbol"]
        tp1_price = pos["tp1"]
        logger.info(f"[{self.bot_version}] TP2 HIT {symbol} - SL -> TP1 ({tp1_price})")

        await self._cancel_order_safe(pos["sl_order_id"], symbol)

        qty_remaining = round(pos["original_quantity"] * (pos["tp3_close_pct"] / 100), 6)
        new_sl_id = await self._place_new_sl(symbol, pos["direction"], qty_remaining, tp1_price)

        await update_position(pos["id"], {
            "tp2_hit": 1,
            "sl_order_id": new_sl_id,
            "stop_loss": tp1_price,
            "remaining_quantity": qty_remaining,
            "state": "trailing",
        })

        pos["tp2_hit"] = 1
        pos["sl_order_id"] = new_sl_id
        pos["stop_loss"] = tp1_price
        pos["remaining_quantity"] = qty_remaining
        pos["state"] = "trailing"

        dec = self._get_decimals(tp1_price)
        await send_trade_update(
            symbol, "tp2_hit",
            f"TP2 touche ! {pos['tp2_close_pct']}% ferme\nSL -> TP1 @ {tp1_price:.{dec}f}"
        )

    async def _handle_tp3_hit(self, pos: dict):
        symbol = pos["symbol"]

        # V4: Trailing TP after TP3 - close partial and trail remainder
        trailing_cfg = self.settings.get("trailing_tp", {}) if self.settings else {}
        if self.bot_version == "V4" and trailing_cfg.get("enabled"):
            tp3_close_pct = trailing_cfg.get("tp3_close_pct", 50)
            trail_atr = trailing_cfg.get("trail_atr", 1.0)

            logger.info(f"[{self.bot_version}] TP3 HIT {symbol} - closing {tp3_close_pct}%, trailing remainder")

            # Close partial
            close_qty = round(pos["remaining_quantity"] * tp3_close_pct / 100, 6)
            trail_qty = round(pos["remaining_quantity"] - close_qty, 6)

            if trail_qty > 0:
                # Set trailing stop at current price - ATR
                entry_atr = pos.get("_entry_atr", 0)
                trail_distance = entry_atr * trail_atr if entry_atr > 0 else abs(pos["tp3"] - pos["entry_price"]) * 0.3

                if pos["direction"] == "long":
                    trail_sl = round(pos["tp3"] - trail_distance, 8)
                else:
                    trail_sl = round(pos["tp3"] + trail_distance, 8)

                await update_position(pos["id"], {
                    "tp3_hit": 1,
                    "remaining_quantity": trail_qty,
                    "stop_loss": trail_sl,
                    "state": "trailing_tp",
                })
                pos["tp3_hit"] = 1
                pos["remaining_quantity"] = trail_qty
                pos["stop_loss"] = trail_sl
                pos["state"] = "trailing_tp"

                logger.info(f"[{self.bot_version}] Trailing TP: {symbol} trail_sl={trail_sl}, qty={trail_qty}")
                await send_trade_update(
                    symbol, "tp3_hit",
                    f"TP3 touche ! {tp3_close_pct}% ferme, trailing le reste\nTrail SL: {trail_sl}"
                )
                return

        # Default behavior: close 100%
        logger.info(f"[{self.bot_version}] TP3 HIT {symbol} - position fermee")

        await update_position(pos["id"], {"tp3_hit": 1})
        pos["tp3_hit"] = 1

        pnl = self._calculate_total_pnl(pos, "tp3")
        await self._close_and_journal(pos, "tp3", pos["tp3"], pnl)
        pos["state"] = "closed"

        await send_trade_update(
            symbol, "tp3_hit",
            f"TP3 touche ! Position fermee 100%\nPnL: +{pnl:.2f}$"
        )

    async def _handle_sl_hit(self, pos: dict):
        symbol = pos["symbol"]
        state = pos["state"]
        logger.info(f"[{self.bot_version}] SL HIT {symbol} (state={state})")

        await self._cancel_remaining_tp_orders(pos)
        await update_position(pos["id"], {"sl_hit": 1})
        pos["sl_hit"] = 1

        pnl = self._calculate_total_pnl(pos, "sl")
        await self._close_and_journal(pos, "sl", pos["stop_loss"], pnl)
        pos["state"] = "closed"

        state_label = {"active": "SL initial", "breakeven": "SL breakeven", "trailing": "SL trailing"}.get(state, "SL")
        sign = "+" if pnl >= 0 else ""
        await send_trade_update(
            symbol, "sl_hit",
            f"{state_label} touche\nPnL: {sign}{pnl:.2f}$"
        )

    # --- Early profit protection ---

    async def _early_profit_protection(self, pos: dict, price: float, direction: str):
        """Protege les gains AVANT TP1 : breakeven precoce + trailing."""
        entry_price = pos["entry_price"]
        tp1_distance = abs(pos["tp1"] - entry_price)
        if tp1_distance <= 0:
            return

        if direction == "long":
            progress = (price - entry_price) / tp1_distance
        else:
            progress = (entry_price - price) / tp1_distance

        if progress <= 0:
            return

        # Lire config early_protection depuis les settings du mode
        mode = pos.get("mode", "scalping")
        mode_cfg = self.settings.get(mode, {}) if self.settings else {}
        early_cfg = mode_cfg.get("early_protection", {})
        be_trigger = early_cfg.get("breakeven_at_pct", 50) / 100
        trail_trigger = early_cfg.get("trail_activation_pct", 65) / 100
        trail_behind = early_cfg.get("trail_behind_pct", 35) / 100

        # 1) Move SL to breakeven (V4: fee-adjusted breakeven)
        be_price = self._fee_adjusted_breakeven(pos)
        if progress >= be_trigger and pos["state"] == "active":
            sl_moved = False
            if direction == "long" and pos["stop_loss"] < be_price:
                sl_moved = True
            elif direction == "short" and pos["stop_loss"] > be_price:
                sl_moved = True

            if sl_moved:
                pos["stop_loss"] = be_price
                pos["state"] = "breakeven"
                await update_position(pos["id"], {
                    "stop_loss": be_price,
                    "state": "breakeven",
                })
                logger.info(
                    f"[{self.bot_version}] EARLY BE {pos['symbol']} "
                    f"@ {be_price} (progress {progress:.0%} toward TP1)"
                )

        # 2) Trail SL to lock profits
        if progress >= trail_trigger and pos["state"] == "breakeven" and not pos.get("tp1_hit"):
            lock_pct = progress - trail_behind
            if direction == "long":
                new_sl = round(entry_price + tp1_distance * lock_pct, 8)
                if new_sl > pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    await update_position(pos["id"], {"stop_loss": new_sl})
            else:
                new_sl = round(entry_price - tp1_distance * lock_pct, 8)
                if new_sl < pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    await update_position(pos["id"], {"stop_loss": new_sl})

    # --- Quick profit (V3) ---

    def _get_min_profit_usd(self, pos: dict) -> float:
        # V4: disabled — replaced by profit giveback protection
        if self.bot_version == "V4":
            return 0

        mode = pos.get("mode", "scalping")
        mode_cfg = self.settings.get(mode, {}) if self.settings else {}
        return mode_cfg.get("min_profit_usd", 0)

    def _get_max_loss_usd(self, pos: dict) -> float:
        mode = pos.get("mode", "scalping")
        mode_cfg = self.settings.get(mode, {}) if self.settings else {}
        return mode_cfg.get("max_loss_usd", 0)

    def _calc_unrealized_pnl(self, pos: dict, price: float) -> float:
        entry = pos["entry_price"]
        direction = pos["direction"]
        qty = pos["remaining_quantity"]
        diff = (price - entry) if direction == "long" else (entry - price)
        unrealized = diff * qty
        realized = 0.0
        if pos.get("tp1_hit"):
            tp1_qty = pos["original_quantity"] * (pos["tp1_close_pct"] / 100)
            d = (pos["tp1"] - entry) if direction == "long" else (entry - pos["tp1"])
            realized += d * tp1_qty
        if pos.get("tp2_hit"):
            tp2_qty = pos["original_quantity"] * (pos["tp2_close_pct"] / 100)
            d = (pos["tp2"] - entry) if direction == "long" else (entry - pos["tp2"])
            realized += d * tp2_qty
        return realized + unrealized

    async def _handle_min_profit_close(self, pos: dict, price: float, pnl_usd: float):
        symbol = pos["symbol"]
        logger.info(f"[{self.bot_version}] MIN_PROFIT CLOSE {symbol} PnL={pnl_usd:.4f}$")

        await self._cancel_remaining_tp_orders(pos)
        await self._cancel_order_safe(pos.get("sl_order_id"), symbol)
        await self._close_and_journal(pos, "min_profit", price, pnl_usd)
        pos["state"] = "closed"

        await send_trade_update(
            symbol, "min_profit",
            f"Quick profit ! Position fermee 100%\nPnL: +{pnl_usd:.2f}$"
        )

    async def _handle_max_loss_close(self, pos: dict, price: float, pnl_usd: float):
        symbol = pos["symbol"]
        logger.info(f"[{self.bot_version}] MAX_LOSS CLOSE {symbol} PnL={pnl_usd:.4f}$")

        await self._cancel_remaining_tp_orders(pos)
        await self._cancel_order_safe(pos.get("sl_order_id"), symbol)
        await self._close_and_journal(pos, "max_loss", price, pnl_usd)
        pos["state"] = "closed"

        await send_trade_update(
            symbol, "max_loss",
            f"Stop loss rapide ! Position fermee 100%\nPnL: {pnl_usd:.2f}$"
        )

    # --- V4: Fee-aware quick exit ---

    def _get_rt_fees(self, pos: dict) -> float:
        """Calculate round-trip fees for a position."""
        if not self.settings:
            return 0
        taker_pct = self.settings.get("fees", {}).get("taker_pct", 0)
        position_size = pos.get("position_size_usd", 0)
        return position_size * (taker_pct / 100) * 2

    def _check_quick_exit(self, pos: dict, current_pnl: float) -> bool:
        """Disabled for V4 — replaced by profit giveback protection."""
        return False

    async def _handle_quick_exit(self, pos: dict, price: float, pnl_usd: float):
        symbol = pos["symbol"]
        fees = self._get_rt_fees(pos)
        logger.info(f"[{self.bot_version}] QUICK EXIT {symbol} gross={pnl_usd:.4f}$ fees={fees:.4f}$ net={pnl_usd - fees:.4f}$")

        await self._cancel_remaining_tp_orders(pos)
        await self._cancel_order_safe(pos.get("sl_order_id"), symbol)
        await self._close_and_journal(pos, "quick_exit", price, pnl_usd)
        pos["state"] = "closed"

        await send_trade_update(
            symbol, "quick_exit",
            f"Quick exit profitable ! Net: {pnl_usd - fees:+.4f}$"
        )

    # --- Stale position timeout ---

    def _check_stale_position(self, pos: dict, current_pnl: float) -> bool:
        """Verifie si la position est stagnante et doit etre fermee."""
        mode = pos.get("mode", "scalping")
        mode_cfg = self.settings.get(mode, {}) if self.settings else {}
        max_hold = mode_cfg.get("max_hold_seconds", 0)
        if max_hold <= 0:
            return False

        entry_time = pos.get("entry_time") or pos.get("created_at")
        if not entry_time:
            return False

        try:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(entry_time)).total_seconds()
        except Exception:
            return False

        if elapsed < max_hold:
            return False

        # V4: Only close stale if losing money (let profitable trades run)
        if self.bot_version == "V4":
            return current_pnl < 0

        # Default: close if PnL < $0.05 (stagnant)
        if current_pnl < 0.05:
            return True
        return False

    async def _handle_stale_close(self, pos: dict, price: float, pnl_usd: float):
        symbol = pos["symbol"]
        logger.info(f"[{self.bot_version}] STALE TIMEOUT {symbol} PnL={pnl_usd:.4f}$")

        await self._cancel_remaining_tp_orders(pos)
        await self._cancel_order_safe(pos.get("sl_order_id"), symbol)
        await self._close_and_journal(pos, "stale_timeout", price, pnl_usd)
        pos["state"] = "closed"

        await send_trade_update(
            symbol, "stale_timeout",
            f"Position stagnante fermee\nPnL: {pnl_usd:+.2f}$"
        )

    # --- V4: Profit Giveback Protection ---

    def _check_profit_giveback(self, pos: dict, current_pnl: float) -> bool:
        """V4: Close if trade gave back >50% of peak profit (secure remaining gains)."""
        if self.bot_version != "V4" or not self.settings:
            return False

        pp_cfg = self.settings.get("profit_protection", {})
        activation_mult = pp_cfg.get("activation_fee_mult", 3.0)
        giveback_pct = pp_cfg.get("giveback_pct", 50)

        peak = pos.get("_max_profit_usd", 0)
        fees = self._get_rt_fees(pos)

        # Only activate when peak was significant (> fees * mult)
        if peak < fees * activation_mult:
            return False

        # How much has been given back?
        giveback = peak - current_pnl
        giveback_ratio = (giveback / peak * 100) if peak > 0 else 0

        if giveback_ratio < giveback_pct:
            return False

        # Only close if remaining net profit > 0
        net = current_pnl - fees
        return net > 0

    async def _handle_profit_giveback_close(self, pos: dict, price: float, current_pnl: float):
        symbol = pos["symbol"]
        fees = self._get_rt_fees(pos)
        peak = pos.get("_max_profit_usd", 0)
        net = current_pnl - fees
        giveback_ratio = ((peak - current_pnl) / peak * 100) if peak > 0 else 0

        logger.info(
            f"[{self.bot_version}] PROFIT GIVEBACK {symbol} "
            f"peak=${peak:.4f} current=${current_pnl:.4f} "
            f"giveback={giveback_ratio:.0f}% net=${net:.4f}"
        )

        await self._cancel_remaining_tp_orders(pos)
        await self._cancel_order_safe(pos.get("sl_order_id"), symbol)
        await self._close_and_journal(pos, "profit_giveback", price, current_pnl)
        pos["state"] = "closed"

        await send_trade_update(
            symbol, "profit_giveback",
            f"Profit secured ! Peak ${peak:.2f} -> giveback {giveback_ratio:.0f}%\nNet: ${net:+.4f}"
        )

    # --- Helpers ordres ---

    async def _cancel_order_safe(self, order_id: str, symbol: str) -> bool:
        if not order_id:
            return True
        exchange = market_data.exchange_private
        if not exchange:
            return False
        try:
            await exchange.cancel_order(order_id, symbol)
            logger.info(f"Ordre annule: {order_id}")
            return True
        except Exception as e:
            logger.warning(f"Cancel {order_id} echoue (peut-etre deja execute): {e}")
            return True

    async def _place_new_sl(self, symbol: str, direction: str, quantity: float, sl_price: float) -> str | None:
        exchange = market_data.exchange_private
        if not exchange:
            return None
        exit_side = "sell" if direction == "long" else "buy"
        try:
            sl_order = await exchange.create_order(
                symbol, "market", exit_side, quantity, None,
                params={"stopPrice": sl_price, "reduceOnly": True, "triggerType": "mark_price"}
            )
            sl_id = sl_order.get("id")
            logger.info(f"Nouveau SL: {symbol} @ {sl_price} qty={quantity} (order {sl_id})")
            return sl_id
        except Exception as e:
            logger.error(f"Erreur placement SL {symbol} @ {sl_price}: {e}")
            return None

    async def _cancel_remaining_tp_orders(self, pos: dict):
        for key, flag in [("tp1_order_id", "tp1_hit"), ("tp2_order_id", "tp2_hit"), ("tp3_order_id", "tp3_hit")]:
            if not pos.get(flag) and pos.get(key):
                await self._cancel_order_safe(pos[key], pos["symbol"])

    # --- Helpers calcul ---

    def _calculate_total_pnl(self, pos: dict, close_reason: str) -> float:
        entry = pos["entry_price"]
        direction = pos["direction"]
        original_qty = pos["original_quantity"]
        pnl = 0.0

        if pos.get("tp1_hit"):
            tp1_qty = original_qty * (pos["tp1_close_pct"] / 100)
            diff = (pos["tp1"] - entry) if direction == "long" else (entry - pos["tp1"])
            pnl += diff * tp1_qty

        if pos.get("tp2_hit"):
            tp2_qty = original_qty * (pos["tp2_close_pct"] / 100)
            diff = (pos["tp2"] - entry) if direction == "long" else (entry - pos["tp2"])
            pnl += diff * tp2_qty

        remaining_qty = pos["remaining_quantity"]
        exit_price = pos["tp3"] if close_reason == "tp3" else pos["stop_loss"]
        diff = (exit_price - entry) if direction == "long" else (entry - exit_price)
        pnl += diff * remaining_qty

        return pnl

    async def _close_and_journal(self, pos: dict, close_reason: str, exit_price: float, pnl_usd: float):
        now = datetime.utcnow().isoformat()

        # V4: Deduire les frais de commission (taker fee aller-retour)
        fees_usd = 0.0
        if self.bot_version == "V4" and self.settings:
            taker_pct = self.settings.get("fees", {}).get("taker_pct", 0)
            if taker_pct > 0:
                position_size = pos.get("position_size_usd", 0)
                fees_usd = position_size * (taker_pct / 100) * 2  # aller + retour
                pnl_usd -= fees_usd
                logger.info(
                    f"[{self.bot_version}] Fees deducted: ${fees_usd:.4f} "
                    f"(position ${position_size:.2f} x {taker_pct}% x2)"
                )

        pnl_pct = (pnl_usd / pos["margin_required"]) * 100 if pos.get("margin_required") else 0
        result = "win" if pnl_usd > 0 else "loss"

        await close_position(pos["id"], {
            "closed_at": now,
            "close_reason": close_reason,
            "pnl_usd": round(pnl_usd, 4),
        })

        entry_time = pos.get("entry_time") or pos.get("created_at")
        duration = 0
        if entry_time:
            try:
                duration = int((datetime.fromisoformat(now) - datetime.fromisoformat(entry_time)).total_seconds())
            except Exception:
                pass

        await insert_trade({
            "signal_id": pos.get("signal_id"),
            "symbol": pos["symbol"],
            "mode": pos.get("mode", "unknown"),
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "stop_loss": pos["stop_loss"],
            "tp1": pos["tp1"],
            "tp2": pos["tp2"],
            "tp3": pos["tp3"],
            "leverage": pos.get("leverage"),
            "position_size_usd": pos.get("position_size_usd"),
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 2),
            "result": result,
            "entry_time": entry_time,
            "exit_time": now,
            "duration_seconds": duration,
            "notes": f"{close_reason} tp1={pos.get('tp1_hit',0)} tp2={pos.get('tp2_hit',0)} tp3={pos.get('tp3_hit',0)}",
            "bot_version": self.bot_version,
        })
        logger.info(f"[{self.bot_version}] Trade journalise: {pos['symbol']} {result} PnL={pnl_usd:.2f}$ ({close_reason})")

        # Apprentissage (ancien trade_learner - backward compat)
        setup_type = pos.get("setup_type", "unknown")
        if setup_type == "unknown" and pos.get("signal_id"):
            from app.database import get_signal_by_id
            sig = await get_signal_by_id(pos["signal_id"])
            if sig:
                setup_type = sig.get("setup_type", "unknown")
        try:
            from app.core.trade_learner import trade_learner
            await trade_learner.record_trade(
                setup_type, pos["symbol"], pos.get("mode", "unknown"),
                pnl_usd > 0, pnl_usd,
            )
        except Exception as e:
            logger.error(f"Erreur trade_learner: {e}")

        # V4 only: Apprentissage adaptatif
        if self.bot_version == "V4":
            try:
                learner = self._get_adaptive_learner()
                if learner:
                    snap = pos.get("_indicator_snapshot", {})
                    regime = pos.get("_regime_snapshot", {})
                    scores = pos.get("_scores_snapshot", {})
                    margin = pos.get("margin_required", 1) or 1
                    max_profit = pos.get("_max_profit_usd", 0)
                    max_dd = pos.get("_max_drawdown_usd", 0)
                    entry_time_parsed = pos.get("entry_time") or pos.get("created_at")
                    hour_utc = 12
                    day_of_week = 0
                    if entry_time_parsed:
                        try:
                            dt = datetime.fromisoformat(entry_time_parsed)
                            hour_utc = dt.hour
                            day_of_week = dt.weekday()
                        except Exception:
                            pass
                    ctx = {
                        "trade_id": None,
                        "signal_id": pos.get("signal_id"),
                        "bot_version": self.bot_version,
                        "candle_pattern": pos.get("_candle_pattern", "none"),
                        "final_score": scores.get("final_score"),
                        "tradeability_score": scores.get("tradeability_score"),
                        "direction_score": scores.get("direction_score"),
                        "setup_score": scores.get("setup_score"),
                        "sentiment_score": scores.get("sentiment_score"),
                        "rsi": snap.get("rsi"),
                        "adx": snap.get("adx"),
                        "atr": snap.get("atr"),
                        "atr_ratio": snap.get("atr_ratio"),
                        "bb_bandwidth": snap.get("bb_bandwidth"),
                        "volume_ratio": snap.get("volume_ratio"),
                        "ema_spread_pct": snap.get("ema_spread_pct"),
                        "vwap_distance_pct": snap.get("vwap_distance_pct"),
                        "macd_histogram": snap.get("macd_histogram"),
                        "stoch_k": snap.get("stoch_k"),
                        "stoch_d": snap.get("stoch_d"),
                        "funding_rate": snap.get("funding_rate"),
                        "spread_pct": snap.get("spread_pct"),
                        "market_regime": regime.get("regime"),
                        "regime_confidence": regime.get("confidence"),
                        "setup_type": setup_type,
                        "symbol": pos["symbol"],
                        "mode": pos.get("mode", "unknown"),
                        "direction": pos["direction"],
                        "mtf_confluence": scores.get("mtf_confluence") if "mtf_confluence" in scores else None,
                        "hour_utc": hour_utc,
                        "day_of_week": day_of_week,
                        "max_profit_usd": max_profit,
                        "max_drawdown_usd": max_dd,
                        "max_profit_pct": round(max_profit / margin * 100, 2) if margin else 0,
                        "max_drawdown_pct": round(max_dd / margin * 100, 2) if margin else 0,
                        "pnl_usd": round(pnl_usd, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "result": result,
                        "close_reason": close_reason,
                        "duration_seconds": duration,
                        "entry_time": entry_time,
                        "exit_time": now,
                    }
                    await learner.record_trade_context(ctx)
            except Exception as e:
                logger.error(f"Erreur adaptive_learner: {e}", exc_info=True)

        for cb in self._on_close_callbacks:
            try:
                await cb(pos["id"], pnl_usd)
            except Exception as e:
                logger.error(f"Erreur callback on_close: {e}")

    def _fee_adjusted_breakeven(self, pos: dict) -> float:
        """Calculate real breakeven = entry + round-trip fees (V4 only).
        Without this, a 'breakeven' close actually loses the fee amount."""
        entry = pos["entry_price"]
        if self.bot_version != "V4" or not self.settings:
            return entry
        taker_pct = self.settings.get("fees", {}).get("taker_pct", 0)
        if taker_pct <= 0:
            return entry
        position_size = pos.get("position_size_usd", 0)
        qty = pos.get("remaining_quantity") or pos.get("original_quantity", 0)
        if qty <= 0:
            return entry
        fees_total = position_size * (taker_pct / 100) * 2  # round-trip
        fee_per_unit = fees_total / qty
        if pos["direction"] == "long":
            return round(entry + fee_per_unit, 8)
        else:
            return round(entry - fee_per_unit, 8)

    @staticmethod
    def _get_decimals(price: float) -> int:
        if price >= 100:
            return 2
        elif price >= 1:
            return 4
        elif price >= 0.01:
            return 6
        return 8
