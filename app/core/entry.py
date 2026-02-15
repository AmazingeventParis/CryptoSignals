"""
COUCHE C : Entry Triggers
Detecte les setups d'entree sur le timeframe d'analyse.
Confirmations avancees: OBV, MACD, Stoch RSI, Ichimoku, divergence MACD.
"""
import logging
import math
from app.config import SETTINGS

logger = logging.getLogger(__name__)

ENTRY_CFG = SETTINGS["entry"]
SCORING_CFG = SETTINGS["scoring"]


def _safe_val(val, default=0):
    """Retourne default si val est None ou NaN."""
    if val is None:
        return default
    if isinstance(val, float) and math.isnan(val):
        return default
    return val


def detect_breakout(indicators: dict, direction_bias: str) -> dict | None:
    bb_bw = indicators.get("last_bb_bandwidth", 999)
    vol_ratio = indicators.get("last_volume_ratio", 0)
    price = indicators.get("last_close", 0)
    bb_upper = indicators.get("last_bb_upper", 0)
    bb_lower = indicators.get("last_bb_lower", 0)

    squeeze_threshold = ENTRY_CFG["bb_squeeze_threshold"]
    vol_spike = ENTRY_CFG["volume_spike_ratio"]

    if bb_bw > squeeze_threshold:
        return None
    if vol_ratio < vol_spike:
        return None

    # Confirmations avancees pour breakout
    confirmations = 0
    conf_reasons = []

    # OBV croissant : confirme le volume derriere le mouvement
    obv_series = indicators.get("obv")
    if obv_series is not None and len(obv_series) >= 5:
        obv_recent = obv_series.tail(5)
        if obv_recent.iloc[-1] > obv_recent.iloc[0]:
            confirmations += 1
            conf_reasons.append("OBV croissant")

    # MACD positif (long) ou negatif (short)
    macd_hist = _safe_val(indicators.get("last_macd_histogram"))
    if direction_bias in ("long", "neutral") and macd_hist > 0:
        confirmations += 1
        conf_reasons.append("MACD+")
    elif direction_bias in ("short", "neutral") and macd_hist < 0:
        confirmations += 1
        conf_reasons.append("MACD-")

    # Bonus score pour confirmations
    conf_bonus = confirmations * 5

    # Breakout haussier
    if price > bb_upper and direction_bias in ("long", "neutral"):
        pattern_score = min(30, int((vol_ratio - vol_spike) / vol_spike * 30) + 15) + conf_bonus
        conf_str = f" [{', '.join(conf_reasons)}]" if conf_reasons else ""
        return {
            "type": "breakout",
            "direction": "long",
            "entry_price": price,
            "pattern_score": min(40, pattern_score),
            "vol_score": min(20, int(vol_ratio / vol_spike * 10)),
            "reason": f"Breakout BB haussier (BW={bb_bw:.3f}%, vol={vol_ratio:.1f}x){conf_str}",
        }

    # Breakout baissier
    if price < bb_lower and direction_bias in ("short", "neutral"):
        pattern_score = min(30, int((vol_ratio - vol_spike) / vol_spike * 30) + 15) + conf_bonus
        conf_str = f" [{', '.join(conf_reasons)}]" if conf_reasons else ""
        return {
            "type": "breakout",
            "direction": "short",
            "entry_price": price,
            "pattern_score": min(40, pattern_score),
            "vol_score": min(20, int(vol_ratio / vol_spike * 10)),
            "reason": f"Breakout BB baissier (BW={bb_bw:.3f}%, vol={vol_ratio:.1f}x){conf_str}",
        }

    return None


def detect_retest(indicators: dict, df, direction_bias: str) -> dict | None:
    if df is None or len(df) < 20:
        return None

    price = indicators.get("last_close", 0)
    atr_val = indicators.get("last_atr", 0)
    buffer = ENTRY_CFG["retest_buffer_pct"] / 100

    highs = df["high"].tail(20)
    lows = df["low"].tail(20)
    recent_high = highs.max()
    recent_low = lows.min()

    candle = df.iloc[-1]
    body = abs(candle["close"] - candle["open"])
    total_range = candle["high"] - candle["low"]
    if total_range == 0:
        return None

    # Confirmation Stoch RSI
    stoch_k = _safe_val(indicators.get("last_stoch_k"), 50)
    stoch_confirmation = ""
    stoch_bonus = 0

    # Retest support (bullish)
    if direction_bias in ("long", "neutral"):
        support_zone = recent_low * (1 + buffer)
        if price <= support_zone and price > recent_low:
            lower_wick = min(candle["open"], candle["close"]) - candle["low"]
            if lower_wick > body * ENTRY_CFG["rejection_wick_ratio"]:
                # Stoch RSI oversold = confirmation forte
                if stoch_k < 20:
                    stoch_bonus = 8
                    stoch_confirmation = " + Stoch RSI oversold"
                elif stoch_k < 35:
                    stoch_bonus = 4
                    stoch_confirmation = " + Stoch RSI bas"

                return {
                    "type": "retest",
                    "direction": "long",
                    "entry_price": price,
                    "pattern_score": 20 + stoch_bonus,
                    "vol_score": min(20, int(indicators.get("last_volume_ratio", 0) * 10)),
                    "reason": f"Retest support {recent_low:.6f} avec rejection{stoch_confirmation}",
                    "key_level": recent_low,
                }

    # Retest resistance (bearish)
    if direction_bias in ("short", "neutral"):
        resistance_zone = recent_high * (1 - buffer)
        if price >= resistance_zone and price < recent_high:
            upper_wick = candle["high"] - max(candle["open"], candle["close"])
            if upper_wick > body * ENTRY_CFG["rejection_wick_ratio"]:
                # Stoch RSI overbought = confirmation forte
                if stoch_k > 80:
                    stoch_bonus = 8
                    stoch_confirmation = " + Stoch RSI overbought"
                elif stoch_k > 65:
                    stoch_bonus = 4
                    stoch_confirmation = " + Stoch RSI haut"

                return {
                    "type": "retest",
                    "direction": "short",
                    "entry_price": price,
                    "pattern_score": 20 + stoch_bonus,
                    "vol_score": min(20, int(indicators.get("last_volume_ratio", 0) * 10)),
                    "reason": f"Retest resistance {recent_high:.6f} avec rejection{stoch_confirmation}",
                    "key_level": recent_high,
                }

    return None


def detect_divergence_setup(indicators: dict, direction_bias: str) -> dict | None:
    div = indicators.get("divergence", "none")
    macd_div = indicators.get("macd_divergence", "none")

    # Double divergence (RSI + MACD) = plus forte
    both = div != "none" and macd_div == div
    bonus = 8 if both else 0
    div_label = "RSI+MACD" if both else ("MACD" if div == "none" and macd_div != "none" else "RSI")

    # Utiliser la divergence RSI ou MACD
    effective_div = div if div != "none" else macd_div

    if effective_div == "bullish" and direction_bias in ("long", "neutral"):
        return {
            "type": "divergence",
            "direction": "long",
            "entry_price": indicators.get("last_close", 0),
            "pattern_score": 22 + bonus,
            "vol_score": 10,
            "reason": f"Divergence haussiere {div_label} (prix lower low, indicateur higher low)",
        }
    elif effective_div == "bearish" and direction_bias in ("short", "neutral"):
        return {
            "type": "divergence",
            "direction": "short",
            "entry_price": indicators.get("last_close", 0),
            "pattern_score": 22 + bonus,
            "vol_score": 10,
            "reason": f"Divergence baissiere {div_label} (prix higher high, indicateur lower high)",
        }

    return None


def detect_ema_bounce(indicators: dict, direction_bias: str) -> dict | None:
    price = indicators.get("last_close", 0)
    ema20 = indicators.get("last_ema_fast", 0)
    ema50 = indicators.get("last_ema_slow", 0)

    if ema20 == 0 or price == 0:
        return None

    proximity_pct = ENTRY_CFG["ema_bounce_proximity_pct"]
    distance_pct = abs(price - ema20) / ema20 * 100

    if distance_pct > proximity_pct:
        return None

    engulfing = indicators.get("engulfing", "none")
    pin_bar = indicators.get("pin_bar", "none")

    # Confirmation Ichimoku Cloud
    ichimoku = indicators.get("ichimoku")
    cloud_confirmation = ""
    cloud_bonus = 0
    if ichimoku is not None:
        senkou_a = ichimoku.get("senkou_a")
        senkou_b = ichimoku.get("senkou_b")
        if senkou_a is not None and senkou_b is not None:
            last_a = _safe_val(senkou_a.iloc[-1] if len(senkou_a) > 0 else None)
            last_b = _safe_val(senkou_b.iloc[-1] if len(senkou_b) > 0 else None)
            if last_a and last_b:
                cloud_top = max(last_a, last_b)
                cloud_bottom = min(last_a, last_b)
                if direction_bias == "long" and price > cloud_top:
                    cloud_bonus = 5
                    cloud_confirmation = " + au-dessus Ichimoku"
                elif direction_bias == "short" and price < cloud_bottom:
                    cloud_bonus = 5
                    cloud_confirmation = " + sous Ichimoku"

    # Bounce haussier
    if direction_bias == "long" and ema20 > ema50:
        if engulfing == "bullish" or pin_bar == "bullish":
            signal_type = "engulfing" if engulfing == "bullish" else "pin bar"
            return {
                "type": "ema_bounce",
                "direction": "long",
                "entry_price": price,
                "pattern_score": 25 + cloud_bonus,
                "vol_score": min(20, int(indicators.get("last_volume_ratio", 0) * 10)),
                "reason": f"Bounce EMA20 haussier ({signal_type}, dist={distance_pct:.3f}%){cloud_confirmation}",
            }

    # Bounce baissier
    if direction_bias == "short" and ema20 < ema50:
        if engulfing == "bearish" or pin_bar == "bearish":
            signal_type = "engulfing" if engulfing == "bearish" else "pin bar"
            return {
                "type": "ema_bounce",
                "direction": "short",
                "entry_price": price,
                "pattern_score": 25 + cloud_bonus,
                "vol_score": min(20, int(indicators.get("last_volume_ratio", 0) * 10)),
                "reason": f"Bounce EMA20 baissier ({signal_type}, dist={distance_pct:.3f}%){cloud_confirmation}",
            }

    return None


def calculate_rr_score(entry: float, stop: float, tp1: float) -> int:
    if stop == 0 or entry == stop:
        return 0
    risk = abs(entry - stop)
    reward = abs(tp1 - entry)
    rr = reward / risk if risk > 0 else 0

    if rr >= 2.0:
        return 25
    elif rr >= 1.5:
        return 15
    else:
        return 0


def calculate_confluence(setups: list[dict]) -> int:
    if len(setups) >= 3:
        return 25
    elif len(setups) == 2:
        return 15
    elif len(setups) == 1:
        return 5
    return 0


def find_best_entry(
    indicators: dict,
    df,
    direction_bias: str,
    allowed_setups: list[str],
) -> dict | None:
    setups = []

    if "breakout" in allowed_setups:
        s = detect_breakout(indicators, direction_bias)
        if s:
            setups.append(s)

    if "retest" in allowed_setups:
        s = detect_retest(indicators, df, direction_bias)
        if s:
            setups.append(s)

    if "divergence" in allowed_setups:
        s = detect_divergence_setup(indicators, direction_bias)
        if s:
            setups.append(s)

    if "ema_bounce" in allowed_setups:
        s = detect_ema_bounce(indicators, direction_bias)
        if s:
            setups.append(s)

    if not setups:
        return None

    best = max(setups, key=lambda x: x["pattern_score"] + x["vol_score"])
    best["confluence_score"] = calculate_confluence(setups)
    best["all_setups"] = [s["type"] for s in setups]

    return best
