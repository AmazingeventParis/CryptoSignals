"""
Order Executor : place les ordres sur MEXC Futures via ccxt.
Verifie le prix actuel, puis: Entree market + Stop Loss + Take Profits (3 niveaux).
"""
import logging
from app.core.market_data import market_data
from app.core.risk_manager import calculate_position_size
from app.core.position_monitor import position_monitor

logger = logging.getLogger(__name__)

# Max deviation autorisee entre le prix du signal et le prix actuel
MAX_PRICE_DEVIATION_PCT = 0.3


async def execute_signal(signal: dict, margin_usdt: float = None, order_type: str = "market") -> dict:
    """
    Execute un signal sur MEXC Futures.
    0. Verifie le prix actuel
    1. Set leverage + margin mode
    2. Calcule la taille de position depuis la marge choisie
    3. Place l'ordre (market = immediat, limit = en attente)
    4. Place SL (stop market)
    5. Place TP1, TP2, TP3 (take profit market)
    """
    exchange = market_data.exchange_private
    if not exchange:
        return {"success": False, "error": "Exchange prive non connecte"}

    symbol = signal["symbol"]
    direction = signal["direction"]
    leverage = signal.get("leverage", 10)
    signal_entry = signal["entry_price"]
    stop_loss = signal["stop_loss"]
    tp1 = signal["tp1"]
    tp2 = signal["tp2"]
    tp3 = signal["tp3"]

    side = "buy" if direction == "long" else "sell"
    exit_side = "sell" if direction == "long" else "buy"

    try:
        # --- 0. Verifier le prix actuel ---
        ticker = await market_data.fetch_ticker(symbol)
        current_price = ticker.get("price", 0)
        if current_price <= 0:
            return {"success": False, "error": "Prix actuel indisponible"}

        if order_type == "market":
            # MARKET : verifier que le prix n'a pas trop devie
            deviation_pct = abs(current_price - signal_entry) / signal_entry * 100
            if deviation_pct > MAX_PRICE_DEVIATION_PCT:
                return {
                    "success": False,
                    "error": (
                        f"Prix a trop bouge ! Signal: {signal_entry}, "
                        f"Actuel: {current_price} (deviation {deviation_pct:.2f}% > {MAX_PRICE_DEVIATION_PCT}%)"
                    ),
                }

            if direction == "long" and current_price <= stop_loss:
                return {"success": False, "error": f"Prix ({current_price}) deja sous le SL ({stop_loss})"}
            if direction == "short" and current_price >= stop_loss:
                return {"success": False, "error": f"Prix ({current_price}) deja au-dessus du SL ({stop_loss})"}

            # Recalculer SL et TPs relatifs au prix actuel
            sl_distance = abs(signal_entry - stop_loss)
            tp1_distance = abs(tp1 - signal_entry)
            tp2_distance = abs(tp2 - signal_entry)
            tp3_distance = abs(tp3 - signal_entry)

            if direction == "long":
                entry_price = current_price
                stop_loss = current_price - sl_distance
                tp1 = current_price + tp1_distance
                tp2 = current_price + tp2_distance
                tp3 = current_price + tp3_distance
            else:
                entry_price = current_price
                stop_loss = current_price + sl_distance
                tp1 = current_price - tp1_distance
                tp2 = current_price - tp2_distance
                tp3 = current_price - tp3_distance

            logger.info(f"MARKET - prix actuel {current_price} (signal {signal_entry})")

        else:
            # LIMIT : on garde le prix du signal comme prix d'entree
            entry_price = signal_entry
            logger.info(f"LIMIT - ordre en attente a {entry_price} (prix actuel {current_price})")

        # --- 1. Leverage et margin mode ---
        # MEXC Futures necessite openType (1=isolated, 2=cross) et positionType (1=long, 2=short)
        position_type = 1 if direction == "long" else 2  # 1=long, 2=short
        open_type = 1  # 1=isolated

        try:
            await exchange.set_margin_mode("isolated", symbol, params={
                "openType": open_type,
                "positionType": position_type,
            })
        except Exception as e:
            logger.debug(f"set_margin_mode: {e}")

        await exchange.set_leverage(leverage, symbol, params={
            "openType": open_type,
            "positionType": position_type,
        })
        logger.info(f"Leverage {leverage}x set pour {symbol} ({direction})")

        # --- 2. Taille de position ---
        balance = await market_data.fetch_balance()
        total_balance = balance.get("free", 0)

        if margin_usdt and margin_usdt > 0:
            # Montant choisi par l'utilisateur
            if margin_usdt > total_balance:
                return {
                    "success": False,
                    "error": f"Marge demandee ({margin_usdt}$) > balance disponible ({total_balance:.2f}$)",
                }
            position_size_usd = margin_usdt * leverage
            quantity = round(position_size_usd / entry_price, 6)
            margin_required = margin_usdt
        else:
            # Fallback : 1% de la balance
            if total_balance <= 0:
                return {"success": False, "error": f"Balance insuffisante: {total_balance} USDT"}
            sizing = calculate_position_size(
                balance=total_balance,
                risk_pct=1.0,
                entry_price=entry_price,
                stop_loss=stop_loss,
                leverage=leverage,
            )
            quantity = sizing["quantity"]
            position_size_usd = sizing["position_size_usd"]
            margin_required = sizing["margin_required"]

        if quantity <= 0:
            return {"success": False, "error": "Quantite calculee = 0"}

        logger.info(
            f"Position: {quantity} {symbol.split('/')[0]} "
            f"({position_size_usd}$ / marge {margin_required}$)"
        )

        # --- 3. Ordre d'entree ---
        if order_type == "limit":
            # LIMIT : ordre en attente au prix du signal
            entry_order = await exchange.create_limit_order(symbol, side, quantity, entry_price)
            entry_id = entry_order.get("id")
            actual_entry = entry_price
            logger.info(f"LIMIT {side.upper()} {quantity} {symbol} @ {entry_price} (order {entry_id})")
        else:
            # MARKET : execution immediate
            entry_order = await exchange.create_market_order(symbol, side, quantity)
            entry_id = entry_order.get("id")
            actual_entry = entry_order.get("average") or entry_order.get("price") or entry_price
            logger.info(f"MARKET {side.upper()} {quantity} {symbol} @ {actual_entry} (order {entry_id})")

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

        result_dict = {
            "success": True,
            "order_type": order_type,
            "entry_order_id": entry_id,
            "actual_entry_price": actual_entry,
            "sl_order_id": sl_order_id,
            "tp_order_ids": tp_order_ids,
            "quantity": quantity,
            "position_size_usd": round(position_size_usd, 2),
            "margin_required": round(margin_required, 2),
            "balance": total_balance,
        }

        # Enregistrer pour le position monitor (trailing stop)
        try:
            signal_for_monitor = {
                **signal,
                "entry_price": actual_entry,
                "stop_loss": stop_loss,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
            }
            await position_monitor.register_trade(signal_for_monitor, result_dict)
        except Exception as e:
            logger.error(f"Erreur register position: {e}")

        return result_dict

    except Exception as e:
        logger.error(f"Erreur execution signal: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
