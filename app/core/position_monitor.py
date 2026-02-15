"""
Position Monitor : Surveille les positions ouvertes, gere le trailing stop.
- Detecte TP1/TP2/TP3 hits via fetch_positions (taille position)
- Deplace le SL (breakeven apres TP1, TP1 price apres TP2)
- Ferme et journalise quand TP3 ou SL touche
"""
import asyncio
import logging
from datetime import datetime

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

POLL_INTERVAL = 8
QTY_TOLERANCE_PCT = 5


class PositionMonitor:
    def __init__(self):
        self.running = False

    async def start(self):
        self.running = True
        logger.info("PositionMonitor demarre")
        while self.running:
            try:
                await self._monitor_cycle()
            except Exception as e:
                logger.error(f"Erreur cycle monitor: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self):
        self.running = False
        logger.info("PositionMonitor arrete")

    async def register_trade(self, signal: dict, result: dict) -> int | None:
        if not result.get("success"):
            return None

        # Verifier qu'il n'y a pas deja une position active sur ce symbol+direction
        existing = await get_active_positions()
        for p in existing:
            if p["symbol"] == signal["symbol"] and p["direction"] == signal["direction"]:
                logger.warning(f"Position deja active pour {signal['symbol']} {signal['direction']}")
                return None

        pos_id = await insert_active_position({
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
        })
        logger.info(f"Position enregistree: {signal['symbol']} {signal['direction']} qty={result['quantity']} (id={pos_id})")
        return pos_id

    # --- Cycle principal ---

    async def _monitor_cycle(self):
        positions = await get_active_positions()
        if not positions:
            return

        for pos in positions:
            try:
                await self._check_position(pos)
            except Exception as e:
                logger.error(f"Erreur check position {pos['symbol']}: {e}", exc_info=True)
            await asyncio.sleep(0.5)

    async def _check_position(self, pos: dict):
        symbol = pos["symbol"]
        exchange = market_data.exchange_private
        if not exchange:
            return

        # Fetch position reelle sur MEXC
        try:
            exchange_pos = await self._fetch_exchange_position(symbol)
        except Exception as e:
            logger.warning(f"Fetch position {symbol} echoue, skip: {e}")
            return

        if exchange_pos is None:
            # Position fermee ? Double check apres 2s
            await asyncio.sleep(2)
            try:
                confirm = await self._fetch_exchange_position(symbol)
            except Exception:
                return
            if confirm is not None:
                return

            # Position vraiment fermee : TP3 ou SL ?
            ticker = await market_data.fetch_ticker(symbol)
            current_price = ticker.get("price", 0)
            if self._price_hit_tp3(current_price, pos):
                await self._handle_tp3_hit(pos)
            else:
                await self._handle_sl_hit(pos)
            return

        actual_qty = abs(exchange_pos.get("contracts", 0))
        original_qty = pos["original_quantity"]

        qty_after_tp1 = original_qty * (1 - pos["tp1_close_pct"] / 100)
        qty_after_tp2 = qty_after_tp1 - original_qty * (pos["tp2_close_pct"] / 100)

        # Detecter TP1
        if not pos["tp1_hit"] and actual_qty <= qty_after_tp1 * (1 + QTY_TOLERANCE_PCT / 100):
            await self._handle_tp1_hit(pos)

        # Detecter TP2
        elif pos["tp1_hit"] and not pos["tp2_hit"] and actual_qty <= qty_after_tp2 * (1 + QTY_TOLERANCE_PCT / 100):
            await self._handle_tp2_hit(pos)

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

        dec = self._get_decimals(tp1_price)
        await send_trade_update(
            symbol, "tp2_hit",
            f"TP2 touche ! {pos['tp2_close_pct']}% ferme\nSL -> TP1 @ {tp1_price:.{dec}f}"
        )

    async def _handle_tp3_hit(self, pos: dict):
        symbol = pos["symbol"]
        logger.info(f"TP3 HIT {symbol} - position fermee")

        await update_position(pos["id"], {"tp3_hit": 1})
        pnl = self._calculate_total_pnl(pos, "tp3")
        await self._close_and_journal(pos, "tp3", pos["tp3"], pnl)

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

        pnl = self._calculate_total_pnl(pos, "sl")
        await self._close_and_journal(pos, "sl", pos["stop_loss"], pnl)

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

    async def _fetch_exchange_position(self, symbol: str) -> dict | None:
        exchange = market_data.exchange_private
        if not exchange:
            return None
        positions = await exchange.fetch_positions([symbol])
        for p in positions:
            contracts = abs(float(p.get("contracts", 0) or 0))
            if contracts > 0:
                return {
                    "contracts": contracts,
                    "unrealizedPnl": float(p.get("unrealizedPnl", 0) or 0),
                    "markPrice": float(p.get("markPrice", 0) or 0),
                    "side": p.get("side"),
                }
        return None

    # --- Helpers calcul ---

    def _price_hit_tp3(self, current_price: float, pos: dict) -> bool:
        if pos["direction"] == "long":
            return current_price >= pos["tp3"]
        return current_price <= pos["tp3"]

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
