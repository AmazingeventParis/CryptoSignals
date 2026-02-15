"""
COUCHE B : Filtre Direction
Determine le biais directionnel a partir du timeframe superieur.
6 votes : EMA Cross, Structure, RSI, MACD, ADX+DI, EMA200.
"""
import logging
import math
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

    # --- Vote 1: EMA Cross 20/50 ---
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

    # --- Vote 2: Market Structure ---
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

    # --- Vote 3: RSI ---
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

    # --- Vote 4: MACD Histogram ---
    macd_hist = indicators.get("last_macd_histogram")
    if macd_hist is not None and not (isinstance(macd_hist, float) and math.isnan(macd_hist)):
        if macd_hist > 0:
            long_votes += 1
            signals.append(f"MACD histogram positif ({macd_hist:.6f})")
        elif macd_hist < 0:
            short_votes += 1
            signals.append(f"MACD histogram negatif ({macd_hist:.6f})")
        else:
            neutral_votes += 1
            signals.append("MACD histogram nul")
    else:
        neutral_votes += 1
        signals.append("MACD indisponible")

    # --- Vote 5: ADX + DI (force et direction de tendance) ---
    adx_val = indicators.get("last_adx")
    plus_di = indicators.get("last_plus_di")
    minus_di = indicators.get("last_minus_di")

    if adx_val is not None and not (isinstance(adx_val, float) and math.isnan(adx_val)):
        if adx_val > 20:
            # Tendance assez forte pour voter
            if plus_di and minus_di:
                if plus_di > minus_di:
                    long_votes += 1
                    signals.append(f"ADX {adx_val:.1f} DI+ > DI- (tendance haussiere)")
                else:
                    short_votes += 1
                    signals.append(f"ADX {adx_val:.1f} DI- > DI+ (tendance baissiere)")
            else:
                neutral_votes += 1
                signals.append(f"ADX {adx_val:.1f} mais DI indisponible")
        else:
            neutral_votes += 1
            signals.append(f"ADX {adx_val:.1f} < 20 (pas de tendance)")
    else:
        neutral_votes += 1
        signals.append("ADX indisponible")

    # --- Vote 6: Prix vs EMA 200 (tendance macro) ---
    ema_200 = indicators.get("last_ema_200")
    if ema_200 is not None and price > 0 and not (isinstance(ema_200, float) and math.isnan(ema_200)):
        distance_200 = ((price - ema_200) / ema_200) * 100
        if price > ema_200:
            long_votes += 1
            signals.append(f"Prix au-dessus EMA200 (+{distance_200:.2f}%)")
        else:
            short_votes += 1
            signals.append(f"Prix sous EMA200 ({distance_200:.2f}%)")
    else:
        neutral_votes += 1
        signals.append("EMA200 indisponible")

    # --- Consensus (6 votes) ---
    total = long_votes + short_votes + neutral_votes
    if total == 0:
        return {"bias": "neutral", "score": 0, "signals": signals}

    # Forte conviction : 4+ votes sur 6
    if long_votes >= 5:
        bias = "long"
        score = 100
    elif long_votes >= 4:
        bias = "long"
        score = 85
    elif long_votes >= 3 and short_votes <= 1:
        bias = "long"
        score = 65
    elif short_votes >= 5:
        bias = "short"
        score = 100
    elif short_votes >= 4:
        bias = "short"
        score = 85
    elif short_votes >= 3 and long_votes <= 1:
        bias = "short"
        score = 65
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
