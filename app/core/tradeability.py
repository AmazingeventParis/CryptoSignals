"""
COUCHE A : Filtre Tradeability
Determine si le marche est tradable ou non pour une paire donnee.
Accepte settings en parametre pour supporter V1 et V2.
"""
import logging
from app.config import SETTINGS

logger = logging.getLogger(__name__)


def _get_thresholds(settings=None):
    s = settings or SETTINGS
    return s["tradeability"]["thresholds"]


def _get_weights(settings=None):
    s = settings or SETTINGS
    return s["tradeability"]["weights"]


def _get_min_score(settings=None):
    s = settings or SETTINGS
    return s["tradeability"]["min_score"]


def check_volatility(atr_current: float, atr_mean: float, thresholds=None) -> tuple[float, str]:
    t = thresholds or _get_thresholds()
    if atr_mean == 0:
        return 0.0, "ATR moyen = 0 (pas de donnees)"
    ratio = atr_current / atr_mean
    min_r = t["atr_min_ratio"]
    max_r = t["atr_max_ratio"]

    if ratio < min_r:
        return 0.0, f"Volatilite trop basse (ATR ratio {ratio:.2f} < {min_r})"
    elif ratio > max_r:
        return 0.0, f"Volatilite trop haute (ATR ratio {ratio:.2f} > {max_r})"
    elif 0.8 <= ratio <= 2.0:
        return 1.0, f"ATR ratio {ratio:.2f} OK"
    elif ratio < 0.8:
        score = (ratio - min_r) / (0.8 - min_r)
        return max(0.0, min(1.0, score)), f"ATR ratio {ratio:.2f} OK"
    else:
        score = 1.0 - (ratio - 2.0) / (max_r - 2.0)
        return max(0.0, min(1.0, score)), f"ATR ratio {ratio:.2f} OK"


def check_volume(vol_current: float, vol_mean: float, thresholds=None) -> tuple[float, str]:
    t = thresholds or _get_thresholds()
    if vol_mean == 0:
        return 0.0, "Volume moyen = 0"
    ratio = vol_current / vol_mean
    min_r = t["volume_min_ratio"]

    if ratio < min_r:
        return 0.0, f"Volume trop bas ({ratio:.2f}x < {min_r}x moyenne)"
    elif ratio >= 2.0:
        return 1.0, f"Volume eleve ({ratio:.2f}x moyenne)"
    else:
        score = (ratio - min_r) / (2.0 - min_r)
        return min(1.0, score), f"Volume {ratio:.2f}x moyenne"


def check_spread(spread_pct: float, mode: str, thresholds=None) -> tuple[float, str]:
    t = thresholds or _get_thresholds()
    if spread_pct >= 900:
        return 0.7, "Spread non disponible (orderbook indisponible)"

    kill = t["spread_kill"]
    if spread_pct >= kill:
        return -1.0, f"Spread {spread_pct:.4f}% > {kill}% KILL"

    max_spread = t["spread_max_scalp"] if mode == "scalping" else t["spread_max_swing"]
    if spread_pct >= max_spread:
        return 0.0, f"Spread {spread_pct:.4f}% > {max_spread}% max"
    else:
        score = 1.0 - (spread_pct / max_spread)
        return max(0.0, score), f"Spread {spread_pct:.4f}% OK"


def check_depth(bid_depth: float, ask_depth: float, min_depth: float = 1000) -> tuple[float, str]:
    total = bid_depth + ask_depth
    if total == 0:
        return 0.7, "Profondeur non disponible (orderbook indisponible)"
    if total < min_depth:
        return 0.0, f"Profondeur {total:.0f} < {min_depth:.0f} min"
    elif total >= min_depth * 5:
        return 1.0, f"Profondeur {total:.0f} excellente"
    else:
        score = (total - min_depth) / (min_depth * 4)
        return min(1.0, score), f"Profondeur {total:.0f} OK"


def check_funding(funding_rate: float, thresholds=None) -> tuple[float, str]:
    t = thresholds or _get_thresholds()
    abs_fr = abs(funding_rate)
    kill = t["funding_kill"]
    max_fr = t["funding_max"]

    if abs_fr >= kill:
        return -1.0, f"Funding {funding_rate:+.4f}% EXTREME - KILL"
    elif abs_fr >= max_fr:
        return 0.0, f"Funding {funding_rate:+.4f}% eleve"
    else:
        score = 1.0 - (abs_fr / max_fr)
        return score, f"Funding {funding_rate:+.4f}% OK"


def check_oi_stability(oi_change_pct: float, thresholds=None) -> tuple[float, str]:
    t = thresholds or _get_thresholds()
    max_drop = t["oi_drop_max_pct"]
    if oi_change_pct < -max_drop:
        return 0.0, f"OI chute {oi_change_pct:.1f}% (cascade liquidations)"
    elif abs(oi_change_pct) < 1.0:
        return 1.0, f"OI stable ({oi_change_pct:+.1f}%)"
    else:
        score = 1.0 - (abs(oi_change_pct) / max_drop)
        return max(0.0, score), f"OI variation {oi_change_pct:+.1f}%"


def check_adx_trend(adx_val: float) -> tuple[float, str]:
    """ADX > 25 = tendance forte (bon pour trader), < 20 = range (prudence)."""
    if adx_val is None or (isinstance(adx_val, float) and (adx_val != adx_val)):
        return 0.5, "ADX indisponible"
    if adx_val >= 30:
        return 1.0, f"ADX {adx_val:.1f} tendance forte"
    elif adx_val >= 25:
        return 0.8, f"ADX {adx_val:.1f} tendance moderee"
    elif adx_val >= 20:
        return 0.5, f"ADX {adx_val:.1f} tendance faible"
    else:
        return 0.2, f"ADX {adx_val:.1f} range (pas de tendance)"


def check_order_flow(bid_depth: float, ask_depth: float) -> tuple[float, str]:
    """Analyse le ratio bid/ask pour detecter la pression acheteuse/vendeuse."""
    total = bid_depth + ask_depth
    if total == 0:
        return 0.5, "Order flow indisponible"
    ratio = bid_depth / total
    if ratio > 0.6:
        return 1.0, f"Pression acheteuse forte (bid ratio {ratio:.2f})"
    elif ratio < 0.4:
        return 1.0, f"Pression vendeuse forte (bid ratio {ratio:.2f})"
    elif 0.45 <= ratio <= 0.55:
        return 0.5, f"Order flow equilibre (bid ratio {ratio:.2f})"
    else:
        return 0.7, f"Order flow {ratio:.2f}"


def evaluate_tradeability(
    atr_current: float,
    atr_mean: float,
    vol_current: float,
    vol_mean: float,
    spread_pct: float,
    bid_depth: float,
    ask_depth: float,
    funding_rate: float,
    oi_change_pct: float,
    mode: str = "scalping",
    adx_val: float = None,
    settings=None,
) -> dict:
    t = _get_thresholds(settings)
    w = _get_weights(settings)
    min_score = _get_min_score(settings)

    checks = {}
    checks["volatility"] = check_volatility(atr_current, atr_mean, t)
    checks["volume"] = check_volume(vol_current, vol_mean, t)
    checks["spread"] = check_spread(spread_pct, mode, t)
    checks["depth"] = check_depth(bid_depth, ask_depth)
    checks["funding"] = check_funding(funding_rate, t)
    checks["oi_stability"] = check_oi_stability(oi_change_pct, t)
    checks["adx_trend"] = check_adx_trend(adx_val)

    # Order flow only if configured (V4 only)
    if "order_flow" in w:
        checks["order_flow"] = check_order_flow(bid_depth, ask_depth)

    # Kill switches
    for name, (score, reason) in checks.items():
        if score == -1.0:
            return {
                "is_tradable": False,
                "score": 0.0,
                "kill_reason": reason,
                "checks": {k: {"score": v[0], "reason": v[1]} for k, v in checks.items()},
            }

    weighted_score = sum(
        checks[name][0] * w[name] for name in w if name in checks
    )

    is_tradable = weighted_score >= min_score

    return {
        "is_tradable": is_tradable,
        "score": round(weighted_score, 3),
        "kill_reason": None,
        "checks": {k: {"score": round(v[0], 3), "reason": v[1]} for k, v in checks.items()},
    }
