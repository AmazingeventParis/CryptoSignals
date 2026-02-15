"""
Signal Engine : combine les 3 couches (Tradeability + Direction + Entry)
pour produire un signal final avec scoring 0-100.
"""
import logging
from app.config import SETTINGS, get_mode_config
from app.core.indicators import compute_all_indicators
from app.core.tradeability import evaluate_tradeability
from app.core.direction import evaluate_direction
from app.core.entry import find_best_entry, calculate_rr_score
from app.core.risk_manager import calculate_risk

logger = logging.getLogger(__name__)

SCORING = SETTINGS["scoring"]


async def analyze_pair(symbol: str, market_data_dict: dict, mode: str) -> dict:
    """
    Analyse complete d'une paire pour un mode donne.
    Retourne un signal ou un NO-TRADE.
    """
    mode_cfg = get_mode_config(mode)
    if not mode_cfg:
        return _no_trade(symbol, mode, "Mode non configure")

    tf_analysis = mode_cfg["timeframes"]["analysis"]
    tf_filter = mode_cfg["timeframes"]["filter"]

    # Verifier qu'on a les donnees
    ohlcv = market_data_dict.get("ohlcv", {})
    for tf in tf_analysis + [tf_filter]:
        if tf not in ohlcv or ohlcv[tf].empty:
            return _no_trade(symbol, mode, f"Donnees manquantes pour {tf}")

    orderbook = market_data_dict.get("orderbook", {})
    ticker = market_data_dict.get("ticker", {})

    # =========================================
    # COUCHE A : TRADEABILITY
    # =========================================
    df_analysis = ohlcv[tf_analysis[0]]
    indicators_analysis = compute_all_indicators(df_analysis, SETTINGS["direction"])

    if not indicators_analysis:
        return _no_trade(symbol, mode, "Pas assez de donnees pour les indicateurs")

    atr_current = indicators_analysis.get("last_atr", 0)
    atr_series = indicators_analysis.get("atr")
    atr_mean = atr_series.mean() if atr_series is not None and not atr_series.empty else 0

    vol_current = df_analysis["volume"].tail(5).mean() if len(df_analysis) >= 5 else 0
    vol_mean = df_analysis["volume"].tail(50).mean() if len(df_analysis) >= 50 else 0

    tradeability = evaluate_tradeability(
        atr_current=atr_current,
        atr_mean=atr_mean,
        vol_current=vol_current,
        vol_mean=vol_mean,
        spread_pct=orderbook.get("spread_pct", 999),
        bid_depth=orderbook.get("bid_depth", 0),
        ask_depth=orderbook.get("ask_depth", 0),
        funding_rate=market_data_dict.get("funding_rate", 0),
        oi_change_pct=0,  # A calculer avec historique OI
        mode=mode,
    )

    if not tradeability["is_tradable"]:
        reasons = []
        if tradeability.get("kill_reason"):
            reasons.append(tradeability["kill_reason"])
        for name, check in tradeability["checks"].items():
            if check["score"] <= 0:
                reasons.append(check["reason"])
        result = _no_trade(symbol, mode, "NON-TRADABLE", reasons, tradeability["score"])
        result["tradeability_checks"] = tradeability["checks"]
        return result

    # =========================================
    # COUCHE B : DIRECTION (timeframe superieur)
    # =========================================
    df_filter = ohlcv[tf_filter]
    indicators_filter = compute_all_indicators(df_filter, SETTINGS["direction"])

    if not indicators_filter:
        return _no_trade(symbol, mode, "Indicateurs TF filtre insuffisants")

    direction = evaluate_direction(indicators_filter)
    direction_bias = direction["bias"]

    # En swing, si direction neutre = no trade
    if mode == "swing" and direction_bias == "neutral":
        return _no_trade(
            symbol, mode, "Direction neutre sur TF superieur (swing = no trade)",
            direction["signals"], tradeability["score"]
        )

    # =========================================
    # COUCHE C : ENTRY TRIGGER (timeframe analyse)
    # =========================================
    allowed_setups = mode_cfg["entry"]["setups"]
    entry = find_best_entry(indicators_analysis, df_analysis, direction_bias, allowed_setups)

    if not entry:
        return _no_trade(
            symbol, mode, "Aucun setup valide detecte",
            direction["signals"], tradeability["score"]
        )

    # =========================================
    # RISK MANAGEMENT
    # =========================================
    risk = calculate_risk(
        entry_price=entry["entry_price"],
        direction=entry["direction"],
        atr=atr_current,
        mode_config=mode_cfg,
        indicators=indicators_analysis,
        df=df_analysis,
    )

    # =========================================
    # SCORING FINAL
    # =========================================
    rr_score = calculate_rr_score(
        entry["entry_price"], risk["stop_loss"], risk["tp1"]
    )
    setup_score = entry["pattern_score"] + entry["vol_score"] + rr_score + entry.get("confluence_score", 0)
    setup_score = min(100, setup_score)

    final_score = int(
        tradeability["score"] * 100 * SCORING["weights"]["tradeability"]
        + direction["score"] * SCORING["weights"]["direction"]
        + setup_score * SCORING["weights"]["setup"]
    )
    final_score = max(0, min(100, final_score))

    # Filtrer par score minimum
    min_score = mode_cfg["entry"]["min_score"]
    if final_score < min_score:
        return _no_trade(
            symbol, mode, f"Score {final_score} < {min_score} minimum",
            direction["signals"], tradeability["score"]
        )

    # =========================================
    # SIGNAL VALIDE
    # =========================================
    reasons = []
    for s in direction["signals"]:
        reasons.append(s)
    reasons.append(entry["reason"])
    reasons.append(f"Funding rate {market_data_dict.get('funding_rate', 0):+.4f}%")
    reasons.append(f"Spread {orderbook.get('spread_pct', 0):.4f}%")

    return {
        "type": "signal",
        "symbol": symbol,
        "mode": mode,
        "direction": entry["direction"],
        "score": final_score,
        "entry_price": entry["entry_price"],
        "stop_loss": risk["stop_loss"],
        "tp1": risk["tp1"],
        "tp2": risk["tp2"],
        "tp3": risk["tp3"],
        "setup_type": entry["type"],
        "leverage": risk["leverage"],
        "risk_pct": risk["risk_pct"],
        "rr_ratio": risk["rr_ratio"],
        "reasons": reasons,
        "tradeability_score": tradeability["score"],
        "direction_score": direction["score"],
        "direction_bias": direction_bias,
        "setup_score": setup_score,
        "all_setups": entry.get("all_setups", []),
    }


def _no_trade(
    symbol: str,
    mode: str,
    reason: str,
    details: list[str] = None,
    tradeability_score: float = 0,
) -> dict:
    return {
        "type": "no_trade",
        "symbol": symbol,
        "mode": mode,
        "direction": "none",
        "score": 0,
        "reason": reason,
        "details": details or [],
        "tradeability_score": tradeability_score,
    }
