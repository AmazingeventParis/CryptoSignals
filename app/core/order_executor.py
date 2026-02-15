"""
Order Executor : place les ordres sur MEXC Futures via ccxt.
Entree market + Stop Loss + Take Profits (3 niveaux).
"""
import logging
from app.core.market_data import market_data
from app.core.risk_manager import calculate_position_size

logger = logging.getLogger(__name__)


async def execute_signal(signal: dict) -> dict:
    """
    Execute un signal sur MEXC Futures.
    1. Set leverage + margin mode
    2. Calcule la taille de position
    3. Place l'ordre market (entree)
    4. Place SL (stop market)
    5. Place TP1, TP2, TP3 (take profit market)
    """
    exchange = market_data.exchange_private
    if not exchange:
        return {"success": False, "error": "Exchange prive non connecte"}

    symbol = signal["symbol"]
    direction = signal["direction"]
    leverage = signal.get("leverage", 10)
    entry_price = signal["entry_price"]
    stop_loss = signal["stop_loss"]
    tp1 = signal["tp1"]
    tp2 = signal["tp2"]
    tp3 = signal["tp3"]

    side = "buy" if direction == "long" else "sell"
    exit_side = "sell" if direction == "long" else "buy"

    try:
        # --- 1. Leverage et margin mode ---
        try:
            await exchange.set_margin_mode("isolated", symbol)
        except Exception as e:
            # Peut echouer si deja en isolated
            logger.debug(f"set_margin_mode: {e}")

        await exchange.set_leverage(leverage, symbol)
        logger.info(f"Leverage {leverage}x set pour {symbol}")

        # --- 2. Taille de position ---
        balance = await market_data.fetch_balance()
        total_balance = balance.get("free", 0)
        if total_balance <= 0:
            return {"success": False, "error": f"Balance insuffisante: {total_balance} USDT"}

        risk_pct = 1.0  # 1% du capital par trade
        sizing = calculate_position_size(
            balance=total_balance,
            risk_pct=risk_pct,
            entry_price=entry_price,
            stop_loss=stop_loss,
            leverage=leverage,
        )
        quantity = sizing["quantity"]
        if quantity <= 0:
            return {"success": False, "error": "Quantite calculee = 0"}

        logger.info(
            f"Position: {quantity} {symbol.split('/')[0]} "
            f"({sizing['position_size_usd']}$ / marge {sizing['margin_required']}$)"
        )

        # --- 3. Ordre d'entree (market) ---
        entry_order = await exchange.create_market_order(symbol, side, quantity)
        entry_id = entry_order.get("id")
        actual_entry = entry_order.get("average") or entry_order.get("price") or entry_price
        logger.info(f"ENTRY {side.upper()} {quantity} {symbol} @ {actual_entry} (order {entry_id})")

        # --- 4. Stop Loss ---
        sl_order_id = None
        try:
            sl_order = await exchange.create_order(
                symbol, "market", exit_side, quantity, None,
                params={
                    "stopPrice": stop_loss,
                    "reduceOnly": True,
                    "triggerType": "mark_price",
                }
            )
            sl_order_id = sl_order.get("id")
            logger.info(f"SL set @ {stop_loss} (order {sl_order_id})")
        except Exception as e:
            logger.error(f"Erreur SL: {e}")

        # --- 5. Take Profits ---
        tp_close_pcts = [
            signal.get("tp1_close_pct", 40),
            signal.get("tp2_close_pct", 30),
            signal.get("tp3_close_pct", 30),
        ]
        tp_prices = [tp1, tp2, tp3]
        tp_order_ids = []

        remaining = quantity
        for i, (tp_price, close_pct) in enumerate(zip(tp_prices, tp_close_pcts)):
            if i < 2:
                tp_qty = round(quantity * close_pct / 100, 6)
            else:
                tp_qty = remaining  # derniere tranche = tout le reste
            remaining -= tp_qty

            try:
                tp_order = await exchange.create_order(
                    symbol, "market", exit_side, tp_qty, None,
                    params={
                        "stopPrice": tp_price,
                        "reduceOnly": True,
                        "triggerType": "mark_price",
                    }
                )
                tp_order_ids.append(tp_order.get("id"))
                logger.info(f"TP{i+1} set @ {tp_price} qty={tp_qty} (order {tp_order.get('id')})")
            except Exception as e:
                logger.error(f"Erreur TP{i+1}: {e}")
                tp_order_ids.append(None)

        return {
            "success": True,
            "entry_order_id": entry_id,
            "actual_entry_price": actual_entry,
            "sl_order_id": sl_order_id,
            "tp_order_ids": tp_order_ids,
            "quantity": quantity,
            "position_size_usd": sizing["position_size_usd"],
            "margin_required": sizing["margin_required"],
            "balance": total_balance,
        }

    except Exception as e:
        logger.error(f"Erreur execution signal: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
