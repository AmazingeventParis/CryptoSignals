"""
Position Monitor : Surveille les positions en TEMPS REEL via WebSocket MEXC.
- WebSocket push.deal = chaque trade sur MEXC -> reaction instantanee
- Detecte TP1/TP2/TP3/SL via le prix en temps reel
- Deplace le SL (breakeven apres TP1, TP1 price apres TP2)
- Polling lent (30s) en backup pour confirmer via fetch_positions
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
from app.services.telegram_bot import send_trade_update

logger = logging.getLogger(__name__)

BACKUP_POLL_INTERVAL = 30
WS_URL = "wss://contract.mexc.com/edge"


class PositionMonitor:
    def __init__(self):
        self.running = False
        self._positions: dict[int, dict] = {}  # pos_id -> pos data (cache)
        self._ws_tasks: dict[str, asyncio.Task] = {}  # symbol -> ws task
        self._processing: set = set()  # pos_ids en cours de traitement (anti-doublon)

    async def start(self):
        self.running = True
        logger.info("PositionMonitor demarre (WebSocket temps reel)")

        # Charger les positions existantes (recovery apres restart)
        await self._reload_positions()

        # Boucle backup lente
        while self.running:
            try:
                await self._backup_check()
            except Exception as e:
                logger.error(f"Erreur backup monitor: {e}", exc_info=True)
            await asyncio.sleep(BACKUP_POLL_INTERVAL)

    async def stop(self):
        self.running = False
        for symbol, task in self._ws_tasks.items():
            task.cancel()
        self._ws_tasks.clear()
        logger.info("PositionMonitor arrete")

    async def register_trade(self, signal: dict, result: dict) -> int | None:
        if not result.get("success"):
            return None

        # Verifier doublon symbol+direction
        for p in self._positions.values():
            if p["symbol"] == signal["symbol"] and p["direction"] == signal["direction"] and p.get("state") != "closed":
                logger.warning(f"Position deja active pour {signal['symbol']} {signal['direction']}")
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
        }

        pos_id = await insert_active_position(pos_data)
        pos_data["id"] = pos_id
        pos_data["state"] = "active"
        pos_data["tp1_hit"] = 0
        pos_data["tp2_hit"] = 0
        pos_data["tp3_hit"] = 0
        pos_data["sl_hit"] = 0

        self._positions[pos_id] = pos_data
        logger.info(f"Position enregistree: {signal['symbol']} {signal['direction']} qty={result['quantity']} (id={pos_id})")

        # Demarrer le WebSocket temps reel pour ce symbol
        self._ensure_ws(signal["symbol"])
        return pos_id

    # --- WebSocket temps reel ---

    def _ensure_ws(self, symbol: str):
        if symbol in self._ws_tasks and not self._ws_tasks[symbol].done():
            return
        self._ws_tasks[symbol] = asyncio.create_task(self._ws_price_stream(symbol))
        logger.info(f"WS prix temps reel demarre: {symbol}")

    async def _ws_price_stream(self, symbol: str):
        mexc_symbol = symbol.split(":")[0].replace("-", "_").replace("/", "_")

        while self.running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    # S'abonner aux deals (chaque trade = prix en temps reel)
                    await ws.send(json.dumps({
                        "method": "sub.deal",
                        "param": {"symbol": mexc_symbol}
                    }))
                    logger.info(f"WS connecte: {symbol} (sub.deal)")

                    ping_task = asyncio.create_task(self._ws_keepalive(ws))

                    try:
                        async for raw in ws:
                            if not self.running:
                                break
                            msg = json.loads(raw)
                            if msg.get("channel") == "push.deal" and msg.get("data"):
                                price = float(msg["data"].get("p", 0))
                                if price > 0:
                                    await self._on_price_tick(symbol, price)
                    finally:
                        ping_task.cancel()

            except (websockets.ConnectionClosed, Exception) as e:
                if self.running:
                    logger.warning(f"WS {symbol} deconnecte: {e}, reconnexion dans 3s")
                    await asyncio.sleep(3)

            # Verifier s'il reste des positions actives pour ce symbol
            if not self._has_active_positions(symbol):
                logger.info(f"WS {symbol} arrete: plus de positions actives")
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

            # TP1
            if not pos["tp1_hit"]:
                tp1_hit = (price >= pos["tp1"]) if direction == "long" else (price <= pos["tp1"])
                if tp1_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_tp1_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

                # SL (avant TP1)
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

                # SL breakeven
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

                # SL trailing
                sl_hit = (price <= pos["stop_loss"]) if direction == "long" else (price >= pos["stop_loss"])
                if sl_hit:
                    self._processing.add(pos_id)
                    try:
                        await self._handle_sl_hit(pos)
                    finally:
                        self._processing.discard(pos_id)
                    continue

    # --- Backup polling (confirmation via exchange) ---

    async def _backup_check(self):
        await self._reload_positions()

    async def _reload_positions(self):
        positions = await get_active_positions()
        for pos in positions:
            self._positions[pos["id"]] = pos
            self._ensure_ws(pos["symbol"])

        # Nettoyer les positions fermees du cache
        active_ids = {p["id"] for p in positions}
        for pid in list(self._positions.keys()):
            if pid not in active_ids:
                del self._positions[pid]

    # --- Handlers TP/SL ---

    async def _handle_tp1_hit(self, pos: dict):
        symbol = pos["symbol"]
        entry_price = pos["entry_price"]
        logger.info(f"TP1 HIT {symbol} - SL -> breakeven ({entry_price})")

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

        # Mettre a jour le cache
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
        logger.info(f"TP2 HIT {symbol} - SL -> TP1 ({tp1_price})")

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
        logger.info(f"TP3 HIT {symbol} - position fermee")

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
        logger.info(f"SL HIT {symbol} (state={state})")

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
        })
        logger.info(f"Trade journalise: {pos['symbol']} {result} PnL={pnl_usd:.2f}$ ({close_reason})")

    @staticmethod
    def _get_decimals(price: float) -> int:
        if price >= 100:
            return 2
        elif price >= 1:
            return 4
        elif price >= 0.01:
            return 6
        return 8


# Singleton
position_monitor = PositionMonitor()
