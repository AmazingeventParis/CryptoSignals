"""
COUCHE B : Filtre Direction
Determine le biais directionnel a partir du timeframe superieur.
"""
import logging
from app.core.indicators import compute_all_indicators, MarketStructure
from app.config import SETTINGS

logger = logging.getLogger(__name__)

DIR_CFG = SETTINGS["direction"]


def evaluate_direction(indicators: dict) -> dict:
    if not indicators:
        return {"bias": "neutral", "score": 0, "signals": []}

    signals = []
    long_votes = 0
    short_votes = 0
    neutral_votes = 0

    # --- Signal 1: EMA Cross ---
    ema_fast = indicators.get("last_ema_fast", 0)
    ema_slow = indicators.get("last_ema_slow", 0)
    price = indicators.get("last_close", 0)

    if ema_slow > 0:
        ema_spread = ((ema_fast - ema_slow) / ema_slow) * 100
        threshold = DIR_CFG["ema_neutral_threshold"]

        if ema_spread > threshold and price > ema_fast:
            long_votes += 1
            signals.append(f"EMA20 > EMA50 (+{ema_spread:.2f}%) + prix au-dessus")
        elif ema_spread < -threshold and price < ema_fast:
            short_votes += 1
            signals.append(f"EMA20 < EMA50 ({ema_spread:.2f}%) + prix en-dessous")
        else:
            neutral_votes += 1
            signals.append(f"EMAs neutres (ecart {ema_spread:.2f}%)")

    # --- Signal 2: Market Structure ---
    structure: MarketStructure = indicators.get("structure")
    if structure:
        if structure.trend == "bullish":
            long_votes += 1
            signals.append("Structure: Higher Highs + Higher Lows")
        elif structure.trend == "bearish":
            short_votes += 1
            signals.append("Structure: Lower Highs + Lower Lows")
        else:
            neutral_votes += 1
            signals.append("Structure: neutre / range")

    # --- Signal 3: RSI ---
    rsi_val = indicators.get("last_rsi", 50)
    long_thresh = DIR_CFG["rsi_long_threshold"]
    short_thresh = DIR_CFG["rsi_short_threshold"]

    if rsi_val > long_thresh:
        long_votes += 1
        signals.append(f"RSI {rsi_val:.1f} > {long_thresh} (momentum haussier)")
    elif rsi_val < short_thresh:
        short_votes += 1
        signals.append(f"RSI {rsi_val:.1f} < {short_thresh} (momentum baissier)")
    else:
        neutral_votes += 1
        signals.append(f"RSI {rsi_val:.1f} neutre")

    # --- Consensus ---
    total = long_votes + short_votes + neutral_votes
    if total == 0:
        return {"bias": "neutral", "score": 0, "signals": signals}

    if long_votes == 3:
        bias = "long"
        score = 100
    elif long_votes == 2:
        bias = "long"
        score = 70
    elif short_votes == 3:
        bias = "short"
        score = 100
    elif short_votes == 2:
        bias = "short"
        score = 70
    else:
        bias = "neutral"
        score = 40

    return {
        "bias": bias,
        "score": score,
        "long_votes": long_votes,
        "short_votes": short_votes,
        "neutral_votes": neutral_votes,
        "signals": signals,
    }
