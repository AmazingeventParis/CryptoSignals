"""
Market Regime Detection : classifie le marche en TRENDING / RANGING / VOLATILE.
Utilise les indicateurs deja calcules (ADX, BB bandwidth, ATR ratio).
"""
import logging
import math

logger = logging.getLogger(__name__)


def _safe(val, default=0):
    if val is None:
        return default
    if isinstance(val, float) and math.isnan(val):
        return default
    return val


def detect_regime(indicators: dict) -> dict:
    """
    Classifie le regime de marche a partir des indicateurs.
    Returns: {"regime": str, "confidence": float, "details": str}
    """
    adx_val = _safe(indicators.get("last_adx"), 0)
    bb_bw = _safe(indicators.get("last_bb_bandwidth"), 0)
    atr_val = _safe(indicators.get("last_atr"), 0)
    atr_series = indicators.get("atr")
    atr_mean = 0
    if atr_series is not None and len(atr_series) >= 14:
        atr_mean = _safe(atr_series.tail(50).mean(), 0)
    atr_ratio = atr_val / atr_mean if atr_mean > 0 else 1.0

    # VOLATILE : ATR ratio > 2.0 OU BB bandwidth > 5.0
    if atr_ratio > 2.0 or bb_bw > 5.0:
        confidence = min(1.0, max(atr_ratio / 3.0, bb_bw / 8.0))
        return {
            "regime": "volatile",
            "confidence": round(confidence, 2),
            "details": f"ATR ratio {atr_ratio:.2f}, BB bw {bb_bw:.2f}%",
            "atr_ratio": round(atr_ratio, 3),
        }

    # TRENDING : ADX >= 25 + BB bandwidth >= 1.5
    if adx_val >= 25 and bb_bw >= 1.5:
        confidence = min(1.0, (adx_val - 20) / 30)
        return {
            "regime": "trending",
            "confidence": round(confidence, 2),
            "details": f"ADX {adx_val:.1f}, BB bw {bb_bw:.2f}%",
            "atr_ratio": round(atr_ratio, 3),
        }

    # RANGING : ADX < 20 + BB bandwidth < 2.0
    if adx_val < 20 and bb_bw < 2.0:
        confidence = min(1.0, (20 - adx_val) / 15)
        return {
            "regime": "ranging",
            "confidence": round(confidence, 2),
            "details": f"ADX {adx_val:.1f}, BB bw {bb_bw:.2f}%",
            "atr_ratio": round(atr_ratio, 3),
        }

    # Mixed / unclear
    return {
        "regime": "trending" if adx_val >= 22 else "ranging",
        "confidence": 0.3,
        "details": f"ADX {adx_val:.1f}, BB bw {bb_bw:.2f}% (mixed)",
        "atr_ratio": round(atr_ratio, 3),
    }


def regime_score_modifier(regime: str, setup_type: str, confidence: float = 1.0) -> int:
    """
    Retourne un modificateur de score pondere par la confiance du regime.
    Base modifiers etendus, multiplies par la confiance de detection.
    """
    base = 0

    if regime == "volatile":
        base = -5
    elif regime == "ranging":
        if setup_type == "breakout":
            base = -5
        elif setup_type == "retest":
            base = 5
    elif regime == "trending":
        if setup_type in ("breakout", "momentum"):
            base = 8
        elif setup_type == "retest":
            base = 3

    return int(base * min(1.0, max(0.1, confidence)))
