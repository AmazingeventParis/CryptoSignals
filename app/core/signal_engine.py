"""
Signal Engine : combine les 4 couches (Tradeability + Direction + Entry + Sentiment)
pour produire un signal final avec scoring 0-100.
Accepte settings en parametre pour supporter V1 et V2.
"""
import logging
import math
from datetime import datetime
from app.config import SETTINGS, get_mode_config
from app.core.indicators import compute_all_indicators
from app.core.tradeability import evaluate_tradeability
from app.core.direction import evaluate_direction
from app.core.entry import find_best_entry, calculate_rr_score, candle_confirmation
from app.core.risk_manager import calculate_risk
from app.core.market_regime import detect_regime, regime_score_modifier
from app.services.sentiment import sentiment_analyzer

logger = logging.getLogger(__name__)


def _safe_val(val, default=0):
    if val is None:
        return default
    if isinstance(val, float) and math.isnan(val):
        return default
    return val


async def analyze_pair(symbol: str, market_data_dict: dict, mode: str, settings=None) -> dict:
    """
    Analyse complete d'une paire pour un mode donne.
    4 couches : Tradeability + Direction + Entry + Sentiment.
    settings: config a utiliser (V1 ou V2). Si None, utilise SETTINGS global (V2).
    """
    s = settings or SETTINGS
    scoring = s["scoring"]

    mode_cfg = get_mode_config(mode, s)
    if not mode_cfg:
        return _no_trade(symbol, mode, "Mode non configure")

    tf_analysis = mode_cfg["timeframes"]["analysis"]
    tf_filter = mode_cfg["timeframes"]["filter"]

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
    indicators_analysis = compute_all_indicators(df_analysis, s["direction"])

    if not indicators_analysis:
        return _no_trade(symbol, mode, "Pas assez de donnees pour les indicateurs")

    atr_current = indicators_analysis.get("last_atr", 0)
    atr_series = indicators_analysis.get("atr")
    atr_mean = atr_series.mean() if atr_series is not None and not atr_series.empty else 0

    vol_current = df_analysis["volume"].tail(5).mean() if len(df_analysis) >= 5 else 0
    vol_mean = df_analysis["volume"].tail(50).mean() if len(df_analysis) >= 50 else 0

    adx_val = indicators_analysis.get("last_adx")

    tradeability = evaluate_tradeability(
        atr_current=atr_current,
        atr_mean=atr_mean,
        vol_current=vol_current,
        vol_mean=vol_mean,
        spread_pct=orderbook.get("spread_pct", 999),
        bid_depth=orderbook.get("bid_depth", 0),
        ask_depth=orderbook.get("ask_depth", 0),
        funding_rate=market_data_dict.get("funding_rate", 0),
        oi_change_pct=0,
        mode=mode,
        adx_val=adx_val,
        settings=s,
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
    indicators_filter = compute_all_indicators(df_filter, s["direction"])

    if not indicators_filter:
        return _no_trade(symbol, mode, "Indicateurs TF filtre insuffisants")

    direction = evaluate_direction(indicators_filter)
    direction_bias = direction["bias"]

    # Gestion direction neutre en swing
    swing_neutral_allowed = s.get("swing_neutral_allowed", True)
    if mode == "swing" and direction_bias == "neutral":
        if swing_neutral_allowed:
            direction["score"] = max(20, direction["score"] // 2)
        else:
            return _no_trade(
                symbol, mode, "Direction neutre - swing bloque",
                direction["signals"], tradeability["score"]
            )

    # =========================================
    # V4 ONLY: REGIME DE MARCHE + MTF CONFLUENCE
    # =========================================
    is_v4 = s.get("_bot_version") == "V4"
    regime_info = {}
    market_regime = ""
    mtf_confluence = 0.0
    if is_v4:
        regime_info = detect_regime(indicators_analysis)
        market_regime = regime_info["regime"]
        mtf_confluence = _compute_mtf_confluence(indicators_analysis, indicators_filter)

    # =========================================
    # COUCHE C : ENTRY TRIGGER (timeframe analyse)
    # =========================================
    from app.core.trade_learner import trade_learner
    allowed_setups = list(mode_cfg["entry"]["setups"])
    allowed_setups = await trade_learner.filter_setups(allowed_setups, symbol, mode)
    if not allowed_setups:
        return _no_trade(
            symbol, mode, "Tous les setups desactives par apprentissage",
            direction["signals"], tradeability["score"]
        )
    entry = find_best_entry(indicators_analysis, df_analysis, direction_bias, allowed_setups)

    if not entry:
        return _no_trade(
            symbol, mode, "Aucun setup valide detecte",
            direction["signals"], tradeability["score"]
        )

    # =========================================
    # CONFIRMATION BOUGIES
    # =========================================
    candle_check = candle_confirmation(entry, indicators_analysis, df_analysis)

    if not candle_check["confirmed"]:
        return _no_trade(
            symbol, mode,
            f"Bougie invalide: {candle_check['reason']}",
            direction["signals"], tradeability["score"]
        )

    candle_modifier = candle_check["score_modifier"]

    # =========================================
    # COUCHE D : SENTIMENT
    # =========================================
    sentiment = await sentiment_analyzer.get_sentiment()
    sentiment_score = sentiment["score"]

    direction_score = direction["score"]
    sentiment_bias = sentiment["bias"]

    if sentiment_bias == "bearish" and entry["direction"] == "long":
        direction_score = int(direction_score * 0.6)
    elif sentiment_bias == "bearish" and entry["direction"] == "short":
        direction_score = int(min(100, direction_score * 1.3))
    elif sentiment_bias == "bullish" and entry["direction"] == "long":
        direction_score = int(min(100, direction_score * 1.3))
    elif sentiment_bias == "bullish" and entry["direction"] == "short":
        direction_score = int(direction_score * 0.6)

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
    # SCORING FINAL (4 couches)
    # =========================================
    rr_score = calculate_rr_score(
        entry["entry_price"], risk["stop_loss"], risk["tp1"]
    )
    setup_score = entry["pattern_score"] + entry["vol_score"] + rr_score + entry.get("confluence_score", 0)
    setup_score = min(100, max(0, setup_score + candle_modifier))

    # V4 ONLY: Regime modifier sur le setup_score
    regime_mod = 0
    mtf_modifier = 0
    if is_v4:
        regime_mod = regime_score_modifier(market_regime, entry["type"])
        setup_score = max(0, min(100, setup_score + regime_mod))
        mtf_modifier = int(mtf_confluence)

    if entry["direction"] == "long":
        sentiment_normalized = (sentiment_score + 100) / 2
    else:
        sentiment_normalized = (-sentiment_score + 100) / 2

    weights = scoring["weights"]
    final_score = int(
        tradeability["score"] * 100 * weights.get("tradeability", 0.30)
        + direction_score * weights.get("direction", 0.25)
        + setup_score * weights.get("setup", 0.25)
        + sentiment_normalized * weights.get("sentiment", 0.20)
    )

    # V4 ONLY: MTF + Learning modifiers
    learning_modifier = 0
    learning_reasons = []
    if is_v4:
        final_score += mtf_modifier
        adaptive_learner = _get_adaptive_learner("V4")
        if adaptive_learner:
            now = datetime.utcnow()
            signal_ctx = {
                "setup_type": entry["type"],
                "symbol": symbol,
                "mode": mode,
                "regime": market_regime,
                "hour_utc": now.hour,
                "score": final_score,
                "direction": entry["direction"],
                "mtf_confluence": mtf_confluence,
            }
            learning_modifier, learning_reasons = adaptive_learner.get_total_modifier(signal_ctx)
            final_score += learning_modifier

    final_score = max(0, min(100, final_score))

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
    for s_item in direction["signals"]:
        reasons.append(s_item)
    reasons.append(entry["reason"])
    if candle_check.get("reason") and candle_check["reason"] != "bougies neutres":
        reasons.append(f"Bougies: {candle_check['reason']}")
    reasons.append(f"Funding rate {market_data_dict.get('funding_rate', 0):+.4f}%")
    if sentiment["reasons"]:
        reasons.append(f"Sentiment: {', '.join(sentiment['reasons'][:2])}")
    if regime_mod != 0:
        reasons.append(f"Regime {market_regime} {regime_mod:+d}pts")
    if mtf_modifier != 0:
        reasons.append(f"MTF confluence {mtf_modifier:+d}pts")
    if learning_modifier != 0:
        reasons.append(f"Learning {learning_modifier:+d}pts")

    # V4 only: Build indicator snapshot for learning context propagation
    _indicator_snapshot = {}
    if is_v4:
        atr_mean_val = atr_series.mean() if atr_series is not None and not atr_series.empty else 0
        _indicator_snapshot = {
            "rsi": _safe_val(indicators_analysis.get("last_rsi")),
            "adx": _safe_val(indicators_analysis.get("last_adx")),
            "atr": _safe_val(atr_current),
            "atr_ratio": round(atr_current / atr_mean_val, 3) if atr_mean_val > 0 else 1.0,
            "bb_bandwidth": _safe_val(indicators_analysis.get("last_bb_bandwidth")),
            "volume_ratio": _safe_val(indicators_analysis.get("last_volume_ratio")),
            "ema_spread_pct": round(abs(_safe_val(indicators_analysis.get("last_ema_fast")) - _safe_val(indicators_analysis.get("last_ema_slow"))) / max(_safe_val(indicators_analysis.get("last_ema_slow")), 1) * 100, 4),
            "vwap_distance_pct": round(abs(_safe_val(indicators_analysis.get("last_close")) - _safe_val(indicators_analysis.get("last_vwap"))) / max(_safe_val(indicators_analysis.get("last_vwap")), 1) * 100, 4),
            "macd_histogram": _safe_val(indicators_analysis.get("last_macd_histogram")),
            "stoch_k": _safe_val(indicators_analysis.get("last_stoch_k")),
            "stoch_d": _safe_val(indicators_analysis.get("last_stoch_d")),
            "funding_rate": market_data_dict.get("funding_rate", 0),
            "spread_pct": orderbook.get("spread_pct", 0),
        }

    result = {
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
        "tp1_close_pct": mode_cfg["take_profit"]["tp1_close_pct"],
        "tp2_close_pct": mode_cfg["take_profit"]["tp2_close_pct"],
        "tp3_close_pct": mode_cfg["take_profit"]["tp3_close_pct"],
        "reasons": reasons,
        "tradeability_score": tradeability["score"],
        "direction_score": direction_score,
        "direction_bias": direction_bias,
        "setup_score": setup_score,
        "sentiment": {
            "score": sentiment_score,
            "bias": sentiment_bias,
            "fear_greed": sentiment.get("fear_greed", 50),
        },
        "all_setups": entry.get("all_setups", []),
    }

    # V4 only: enrichments for learning context propagation
    if is_v4:
        result.update({
            "market_regime": market_regime,
            "regime_confidence": regime_info.get("confidence", 0),
            "mtf_confluence": mtf_confluence,
            "learning_modifier": learning_modifier,
            "_entry_atr": _safe_val(atr_current),
            "_indicator_snapshot": _indicator_snapshot,
            "_regime_snapshot": regime_info,
            "_scores_snapshot": {
                "final_score": final_score,
                "tradeability_score": tradeability["score"],
                "direction_score": direction_score,
                "setup_score": setup_score,
                "sentiment_score": sentiment_score,
                "mtf_confluence": mtf_confluence,
            },
        })

    return result


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


def _compute_mtf_confluence(indicators_analysis: dict, indicators_filter: dict) -> float:
    """
    Compare la structure et le RSI du TF analyse vs TF filtre.
    Retourne un score de -10 a +10.
    """
    score = 0.0

    # Structure alignment
    struct_analysis = indicators_analysis.get("structure")
    struct_filter = indicators_filter.get("structure")
    if struct_analysis and struct_filter:
        if struct_analysis.trend == struct_filter.trend and struct_analysis.trend != "neutral":
            score += 5  # Aligned trending
        elif struct_analysis.trend != "neutral" and struct_filter.trend != "neutral" and struct_analysis.trend != struct_filter.trend:
            score -= 5  # Conflicting trends

    # RSI alignment
    rsi_analysis = _safe_val(indicators_analysis.get("last_rsi"), 50)
    rsi_filter = _safe_val(indicators_filter.get("last_rsi"), 50)
    both_bullish = rsi_analysis > 55 and rsi_filter > 55
    both_bearish = rsi_analysis < 45 and rsi_filter < 45
    conflicting = (rsi_analysis > 60 and rsi_filter < 40) or (rsi_analysis < 40 and rsi_filter > 60)

    if both_bullish or both_bearish:
        score += 5
    elif conflicting:
        score -= 5

    return max(-10, min(10, score))


# Registry for adaptive learners (set from main.py)
_adaptive_learners: dict = {}


def register_adaptive_learner(bot_version: str, learner):
    _adaptive_learners[bot_version] = learner


def _get_adaptive_learner(bot_version: str = None):
    if not bot_version:
        return None
    return _adaptive_learners.get(bot_version)
