"""
COUCHE A : Filtre Tradeability
Determine si le marche est tradable ou non pour une paire donnee.
"""
import logging
from app.config import SETTINGS

logger = logging.getLogger(__name__)

THRESHOLDS = SETTINGS["tradeability"]["thresholds"]
WEIGHTS = SETTINGS["tradeability"]["weights"]
MIN_SCORE = SETTINGS["tradeability"]["min_score"]


def check_volatility(atr_current: float, atr_mean: float) -> tuple[float, str]:
    if atr_mean == 0:
        return 0.0, "ATR moyen = 0 (pas de donnees)"
    ratio = atr_current / atr_mean
    min_r = THRESHOLDS["atr_min_ratio"]
    max_r = THRESHOLDS["atr_max_ratio"]

    if ratio < min_r:
        return 0.0, f"Volatilite trop basse (ATR ratio {ratio:.2f} < {min_r})"
    elif ratio > max_r:
        return 0.0, f"Volatilite trop haute (ATR ratio {ratio:.2f} > {max_r})"
    else:
        # Score lineaire entre min et max, optimal au milieu
        mid = (min_r + max_r) / 2
        if ratio <= mid:
            score = (ratio - min_r) / (mid - min_r)
        else:
            score = 1.0 - (ratio - mid) / (max_r - mid)
        return max(0.0, min(1.0, score)), f"ATR ratio {ratio:.2f} OK"


def check_volume(vol_current: float, vol_mean: float) -> tuple[float, str]:
    if vol_mean == 0:
        return 0.0, "Volume moyen = 0"
    ratio = vol_current / vol_mean
    min_r = THRESHOLDS["volume_min_ratio"]

    if ratio < min_r:
        return 0.0, f"Volume trop bas ({ratio:.2f}x < {min_r}x moyenne)"
    elif ratio >= 2.0:
        return 1.0, f"Volume eleve ({ratio:.2f}x moyenne)"
    else:
        score = (ratio - min_r) / (2.0 - min_r)
        return min(1.0, score), f"Volume {ratio:.2f}x moyenne"


def check_spread(spread_pct: float, mode: str) -> tuple[float, str]:
    # Si orderbook indisponible (999 = valeur par defaut), score neutre-positif
    if spread_pct >= 900:
        return 0.7, "Spread non disponible (orderbook indisponible)"

    kill = THRESHOLDS["spread_kill"]
    if spread_pct >= kill:
        return -1.0, f"Spread {spread_pct:.4f}% > {kill}% KILL"

    max_spread = THRESHOLDS["spread_max_scalp"] if mode == "scalping" else THRESHOLDS["spread_max_swing"]
    if spread_pct >= max_spread:
        return 0.0, f"Spread {spread_pct:.4f}% > {max_spread}% max"
    else:
        score = 1.0 - (spread_pct / max_spread)
        return max(0.0, score), f"Spread {spread_pct:.4f}% OK"


def check_depth(bid_depth: float, ask_depth: float, min_depth: float = 1000) -> tuple[float, str]:
    total = bid_depth + ask_depth
    # Si orderbook indisponible, score neutre-positif
    if total == 0:
        return 0.7, "Profondeur non disponible (orderbook indisponible)"
    if total < min_depth:
        return 0.0, f"Profondeur {total:.0f} < {min_depth:.0f} min"
    elif total >= min_depth * 5:
        return 1.0, f"Profondeur {total:.0f} excellente"
    else:
        score = (total - min_depth) / (min_depth * 4)
        return min(1.0, score), f"Profondeur {total:.0f} OK"


def check_funding(funding_rate: float) -> tuple[float, str]:
    abs_fr = abs(funding_rate)
    kill = THRESHOLDS["funding_kill"]
    max_fr = THRESHOLDS["funding_max"]

    if abs_fr >= kill:
        return -1.0, f"Funding {funding_rate:+.4f}% EXTREME - KILL"
    elif abs_fr >= max_fr:
        return 0.0, f"Funding {funding_rate:+.4f}% eleve"
    else:
        score = 1.0 - (abs_fr / max_fr)
        return score, f"Funding {funding_rate:+.4f}% OK"


def check_oi_stability(oi_change_pct: float) -> tuple[float, str]:
    max_drop = THRESHOLDS["oi_drop_max_pct"]
    if oi_change_pct < -max_drop:
        return 0.0, f"OI chute {oi_change_pct:.1f}% (cascade liquidations)"
    elif abs(oi_change_pct) < 1.0:
        return 1.0, f"OI stable ({oi_change_pct:+.1f}%)"
    else:
        score = 1.0 - (abs(oi_change_pct) / max_drop)
        return max(0.0, score), f"OI variation {oi_change_pct:+.1f}%"


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
) -> dict:
    checks = {}

    checks["volatility"] = check_volatility(atr_current, atr_mean)
    checks["volume"] = check_volume(vol_current, vol_mean)
    checks["spread"] = check_spread(spread_pct, mode)
    checks["depth"] = check_depth(bid_depth, ask_depth)
    checks["funding"] = check_funding(funding_rate)
    checks["oi_stability"] = check_oi_stability(oi_change_pct)

    # Kill switches : si un check retourne -1, NON-TRADABLE immediat
    for name, (score, reason) in checks.items():
        if score == -1.0:
            return {
                "is_tradable": False,
                "score": 0.0,
                "kill_reason": reason,
                "checks": {k: {"score": v[0], "reason": v[1]} for k, v in checks.items()},
            }

    # Score pondere
    weighted_score = sum(
        checks[name][0] * WEIGHTS[name] for name in WEIGHTS if name in checks
    )

    is_tradable = weighted_score >= MIN_SCORE

    return {
        "is_tradable": is_tradable,
        "score": round(weighted_score, 3),
        "kill_reason": None,
        "checks": {k: {"score": round(v[0], 3), "reason": v[1]} for k, v in checks.items()},
    }
