"""
Risk Manager : calcul SL, TP1/TP2/TP3, taille position, levier.
"""
import logging

logger = logging.getLogger(__name__)


def calculate_risk(
    entry_price: float,
    direction: str,
    atr: float,
    mode_config: dict,
    indicators: dict,
    df,
) -> dict:
    sl_cfg = mode_config["stop_loss"]
    tp_cfg = mode_config["take_profit"]
    risk_cfg = mode_config["risk"]

    # --- STOP LOSS ---
    if sl_cfg["method"] == "atr":
        sl_distance = atr * sl_cfg["atr_multiplier"]
    elif sl_cfg["method"] == "structure":
        sl_distance = _structure_stop(df, direction, atr, sl_cfg.get("buffer_atr", 0.5))
    else:
        sl_distance = atr * 1.5

    # Limiter le stop au max autorise
    max_stop = entry_price * (sl_cfg.get("max_stop_pct", 1.0) / 100)
    sl_distance = min(sl_distance, max_stop)

    if direction == "long":
        stop_loss = entry_price - sl_distance
    else:
        stop_loss = entry_price + sl_distance

    # --- TAKE PROFITS ---
    risk = sl_distance

    if direction == "long":
        tp1 = entry_price + risk * tp_cfg["tp1_rr"]
        tp2 = entry_price + risk * tp_cfg["tp2_rr"]
        tp3 = entry_price + risk * tp_cfg["tp3_rr"]
    else:
        tp1 = entry_price - risk * tp_cfg["tp1_rr"]
        tp2 = entry_price - risk * tp_cfg["tp2_rr"]
        tp3 = entry_price - risk * tp_cfg["tp3_rr"]

    # --- LEVIER ---
    lev_min, lev_max = risk_cfg["leverage_range"]
    sl_pct = (sl_distance / entry_price) * 100

    # Levier adaptatif : plus le stop est serré, plus on peut leverager
    if sl_pct <= 0.2:
        leverage = lev_max
    elif sl_pct >= 1.0:
        leverage = lev_min
    else:
        leverage = int(lev_max - (sl_pct - 0.2) / 0.8 * (lev_max - lev_min))
    leverage = max(lev_min, min(lev_max, leverage))

    # --- R:R RATIO ---
    rr_ratio = round(tp1_distance / risk, 2) if (risk > 0 and (tp1_distance := abs(tp1 - entry_price))) else 0

    return {
        "stop_loss": round(stop_loss, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "tp3": round(tp3, 8),
        "sl_distance": round(sl_distance, 8),
        "risk_pct": round(sl_pct, 3),
        "leverage": leverage,
        "rr_ratio": rr_ratio,
        "tp1_close_pct": tp_cfg["tp1_close_pct"],
        "tp2_close_pct": tp_cfg["tp2_close_pct"],
        "tp3_close_pct": tp_cfg["tp3_close_pct"],
    }


def _structure_stop(df, direction: str, atr: float, buffer_atr: float) -> float:
    if df is None or len(df) < 10:
        return atr * 1.5

    recent = df.tail(10)

    if direction == "long":
        swing_low = recent["low"].min()
        current_price = df["close"].iloc[-1]
        distance = current_price - swing_low + (atr * buffer_atr)
    else:
        swing_high = recent["high"].max()
        current_price = df["close"].iloc[-1]
        distance = swing_high - current_price + (atr * buffer_atr)

    return max(distance, atr * 0.5)  # minimum 0.5 ATR


def calculate_position_size(
    balance: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    leverage: int,
) -> dict:
    risk_amount = balance * (risk_pct / 100)
    sl_distance = abs(entry_price - stop_loss)

    if sl_distance == 0 or entry_price == 0:
        return {"position_size_usd": 0, "quantity": 0, "margin_required": 0}

    sl_pct = sl_distance / entry_price
    position_size_usd = risk_amount / sl_pct
    margin_required = position_size_usd / leverage
    quantity = position_size_usd / entry_price

    return {
        "position_size_usd": round(position_size_usd, 2),
        "quantity": round(quantity, 6),
        "margin_required": round(margin_required, 2),
        "risk_amount": round(risk_amount, 2),
    }
