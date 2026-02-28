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
        oi_change_pct=market_data_dict.get("oi_change_pct", 0),
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

    # V4 ONLY: Regime modifier sur le setup_score + VWAP confluence
    regime_mod = 0
    mtf_modifier = 0
    vwap_modifier = 0
    if is_v4:
        regime_mod = regime_score_modifier(market_regime, entry["type"], regime_info.get("confidence", 0))
        setup_score = max(0, min(100, setup_score + regime_mod))
        mtf_modifier = int(mtf_confluence)

        # VWAP confluence: LONG above VWAP = +5, LONG far below = -5
        vwap_val = _safe_val(indicators_analysis.get("last_vwap"), 0)
        last_close = _safe_val(indicators_analysis.get("last_close"), 0)
        if vwap_val > 0 and last_close > 0:
            vwap_dist_pct = (last_close - vwap_val) / vwap_val * 100
            if entry["direction"] == "long":
                if vwap_dist_pct > 0.1:
                    vwap_modifier = 5   # LONG above VWAP
                elif vwap_dist_pct < -0.5:
                    vwap_modifier = -5  # LONG far below VWAP
            else:  # short
                if vwap_dist_pct < -0.1:
                    vwap_modifier = 5   # SHORT below VWAP
                elif vwap_dist_pct > 0.5:
                    vwap_modifier = -5  # SHORT far above VWAP

    if entry["direction"] == "long":
        sentiment_normalized = (sentiment_score + 100) / 2
    else:
        sentiment_normalized = (-sentiment_score + 100) / 2

    weights = scoring["weights"]

    # V4: Mode-specific weights (reduce sentiment for scalping)
    if is_v4 and mode == "scalping":
        w_trade = 0.35
        w_dir = 0.30
        w_setup = 0.30
        w_sent = 0.05
    elif is_v4 and mode == "swing":
        w_trade = 0.30
        w_dir = 0.25
        w_setup = 0.25
        w_sent = 0.20
    else:
        w_trade = weights.get("tradeability", 0.30)
        w_dir = weights.get("direction", 0.25)
        w_setup = weights.get("setup", 0.25)
        w_sent = weights.get("sentiment", 0.20)

    final_score = int(
        tradeability["score"] * 100 * w_trade
        + direction_score * w_dir
        + setup_score * w_setup
        + sentiment_normalized * w_sent
    )

    min_score = mode_cfg["entry"]["min_score"]

    # V4 ONLY: Gate base score BEFORE modifiers to prevent inflation
    if is_v4:
        base_score = max(0, min(100, final_score))
        if base_score < min_score:
            return _no_trade(
                symbol, mode,
                f"V4 base score {base_score} < {min_score} (before modifiers)",
                direction["signals"], tradeability["score"]
            )

    # V4 ONLY: MTF + VWAP + Learning modifiers
    learning_modifier = 0
    learning_reasons = []
    _candle_pattern = "none"
    if is_v4:
        final_score += mtf_modifier + vwap_modifier
        adaptive_learner = _get_adaptive_learner("V4")
        if adaptive_learner:
            now = datetime.utcnow()
            # Detect confirmed candle pattern for learning
            _candle_pattern = "none"
            for pat_name in ("engulfing", "hammer", "shooting_star", "pin_bar", "doji"):
                pat_val = indicators_analysis.get(pat_name, "none")
                if pat_val != "none":
                    _candle_pattern = pat_name
                    break
            signal_ctx = {
                "setup_type": entry["type"],
                "symbol": symbol,
                "mode": mode,
                "regime": market_regime,
                "hour_utc": now.hour,
                "score": final_score,
                "direction": entry["direction"],
                "mtf_confluence": mtf_confluence,
                "candle_pattern": _candle_pattern,
            }
            learning_modifier, learning_reasons = adaptive_learner.get_total_modifier(signal_ctx)
            final_score += learning_modifier

    final_score = max(0, min(100, final_score))

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
    if vwap_modifier != 0:
        reasons.append(f"VWAP confluence {vwap_modifier:+d}pts")
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
            "candle_pattern": _candle_pattern,
            "vwap_modifier": vwap_modifier,
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
    Compare la structure, RSI et ADX du TF analyse vs TF filtre.
    Retourne un score gradue de -15 a +15.
    """
    score = 0.0

    # Structure alignment (0 a ±7 pts selon force HH/HL count)
    struct_analysis = indicators_analysis.get("structure")
    struct_filter = indicators_filter.get("structure")
    if struct_analysis and struct_filter:
        if struct_analysis.trend == struct_filter.trend and struct_analysis.trend != "neutral":
            # Aligned: score based on strength
            strength = getattr(struct_filter, "strength", 1)
            score += min(7, 3 + strength * 2)  # 3 a 7 pts
        elif struct_analysis.trend != "neutral" and struct_filter.trend != "neutral" and struct_analysis.trend != struct_filter.trend:
            score -= 7  # Conflicting trends

    # RSI alignment: scoring continu normalise (0 a ±5 pts)
    rsi_analysis = _safe_val(indicators_analysis.get("last_rsi"), 50)
    rsi_filter = _safe_val(indicators_filter.get("last_rsi"), 50)
    rsi_product = (rsi_analysis - 50) * (rsi_filter - 50)
    if rsi_product > 0:
        # Same side: scale 0 to 5
        rsi_score = min(5, abs(rsi_product) / 200)
        score += rsi_score
    elif rsi_product < -100:
        # Conflicting: scale 0 to -5
        rsi_score = min(5, abs(rsi_product) / 200)
        score -= rsi_score

    # ADX alignment: both trending or ranging (±3 pts)
    adx_analysis = _safe_val(indicators_analysis.get("last_adx"), 20)
    adx_filter = _safe_val(indicators_filter.get("last_adx"), 20)
    both_trending = adx_analysis >= 25 and adx_filter >= 25
    both_ranging = adx_analysis < 20 and adx_filter < 20
    if both_trending:
        score += 3
    elif both_ranging:
        score -= 2

    return max(-15, min(15, round(score, 1)))


# Registry for adaptive learners (set from main.py)
_adaptive_learners: dict = {}


def register_adaptive_learner(bot_version: str, learner):
    _adaptive_learners[bot_version] = learner


def _get_adaptive_learner(bot_version: str = None):
    if not bot_version:
        return None
    return _adaptive_learners.get(bot_version)
