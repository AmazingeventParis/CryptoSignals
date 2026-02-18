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

    async def _reload_positions(self):
        positions = await get_active_positions(bot_version=self.bot_version)
        for pos in positions:
            self._positions[pos["id"]] = pos
            self._ensure_ws(pos["symbol"])

        active_ids = {p["id"] for p in positions}
        for pid in list(self._positions.keys()):
            if pid not in active_ids:
                del self._positions[pid]

    # --- Handlers TP/SL ---

    async def _handle_tp1_hit(self, pos: dict):
        symbol = pos["symbol"]
        entry_price = pos["entry_price"]
        logger.info(f"[{self.bot_version}] TP1 HIT {symbol} - SL -> breakeven ({entry_price})")

        await self._cancel_order_safe(pos["sl_order_id"], symbol)

        qty_remaining = round(pos["original_quantity"] * (1 - pos["tp1_close_pct"] / 100), 6)
        new_sl_id = await self._place_new_sl(symbol, pos["direction"], qty_remaining, entry_price)

        await update_position(pos["id"], {
            "tp1_hit": 1,
            "sl_order_id": new_sl_id,
            "stop_loss": entry_price,
            "remaining_quantity": qty_remaining,
            "state": "breakeven",
        })

        pos["tp1_hit"] = 1
        pos["sl_order_id"] = new_sl_id
        pos["stop_loss"] = entry_price
        pos["remaining_quantity"] = qty_remaining
        pos["state"] = "breakeven"

        dec = self._get_decimals(entry_price)
        await send_trade_update(
            symbol, "tp1_hit",
            f"TP1 touche ! {pos['tp1_close_pct']}% ferme\nSL -> breakeven @ {entry_price:.{dec}f}"
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

        # 1) Move SL to breakeven
        if progress >= be_trigger and pos["state"] == "active":
            sl_moved = False
            if direction == "long" and pos["stop_loss"] < entry_price:
                sl_moved = True
            elif direction == "short" and pos["stop_loss"] > entry_price:
                sl_moved = True

            if sl_moved:
                pos["stop_loss"] = entry_price
                pos["state"] = "breakeven"
                await update_position(pos["id"], {
                    "stop_loss": entry_price,
                    "state": "breakeven",
                })
                logger.info(
                    f"[{self.bot_version}] EARLY BE {pos['symbol']} "
                    f"(progress {progress:.0%} toward TP1)"
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

        # Apprentissage
        try:
            from app.core.trade_learner import trade_learner
            setup_type = pos.get("setup_type", "unknown")
            if setup_type == "unknown" and pos.get("signal_id"):
                from app.database import get_signal_by_id
                sig = await get_signal_by_id(pos["signal_id"])
                if sig:
                    setup_type = sig.get("setup_type", "unknown")
            await trade_learner.record_trade(
                setup_type, pos["symbol"], pos.get("mode", "unknown"),
                pnl_usd > 0, pnl_usd,
            )
        except Exception as e:
            logger.error(f"Erreur trade_learner: {e}")

        for cb in self._on_close_callbacks:
            try:
                await cb(pos["id"], pnl_usd)
            except Exception as e:
                logger.error(f"Erreur callback on_close: {e}")

    @staticmethod
    def _get_decimals(price: float) -> int:
        if price >= 100:
            return 2
        elif price >= 1:
            return 4
        elif price >= 0.01:
            return 6
        return 8
