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

    direction = evaluate_direction(indicators_filter, config=s.get("direction"))
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
    # V4 ONLY: SNIPER GATES
    # =========================================
    is_v4 = s.get("_bot_version") == "V4"

    if is_v4:
        sniper_cfg = s.get("sniper", {})

        # Gate 1: Direction Hard Gate — block neutral and weak direction
        if sniper_cfg.get("direction_gate", False):
            min_dir_score = sniper_cfg.get("direction_min_score", 65)
            if direction_bias == "neutral":
                return _no_trade(
                    symbol, mode, "Sniper: direction neutre bloquee",
                    direction["signals"], tradeability["score"]
                )
            if direction["score"] < min_dir_score:
                return _no_trade(
                    symbol, mode,
                    f"Sniper: direction score {direction['score']} < {min_dir_score}",
                    direction["signals"], tradeability["score"]
                )

        # Gate 2: OBI (Order Book Imbalance) — legacy, replaced by flow intelligence
        if sniper_cfg.get("obi_gate", False):
            bid_d = orderbook.get("bid_depth", 0)
            ask_d = orderbook.get("ask_depth", 0)
            total = bid_d + ask_d
            if total > 0:
                obi = (bid_d - ask_d) / total
                obi_thresh = sniper_cfg.get("obi_min_threshold", 0.15)
                if direction_bias == "long" and obi < obi_thresh:
                    return _no_trade(
                        symbol, mode,
                        f"Sniper: OBI {obi:.2f} < {obi_thresh} pour long",
                        direction["signals"], tradeability["score"]
                    )
                elif direction_bias == "short" and obi > -obi_thresh:
                    return _no_trade(
                        symbol, mode,
                        f"Sniper: OBI {obi:.2f} > {-obi_thresh} pour short",
                        direction["signals"], tradeability["score"]
                    )

        # Gate 2b: Flow Intelligence Gates — replaces OBI when enabled
        flow_cfg = s.get("flow_intelligence", {})
        flow_data = market_data_dict.get("flow_intelligence")
        if flow_data and v4f.get("order_flow", False) and not flow_data.get("is_stale", True):
            # CVD Gate: block if CVD diverges against direction
            if flow_cfg.get("cvd_gate", False):
                cvd_info = flow_data.get("cvd", {})
                cvd_signal = cvd_info.get("signal", "neutral")
                cvd_confidence = cvd_info.get("confidence", 0)
                cvd_min_conf = flow_cfg.get("cvd_min_confidence", 0.3)

                if cvd_confidence >= cvd_min_conf:
                    if direction_bias == "long" and cvd_signal == "bearish_divergence":
                        return _no_trade(
                            symbol, mode,
                            f"Flow: CVD bearish divergence blocks long (conf={cvd_confidence:.2f})",
                            direction["signals"], tradeability["score"]
                        )
                    elif direction_bias == "short" and cvd_signal == "bullish_divergence":
                        return _no_trade(
                            symbol, mode,
                            f"Flow: CVD bullish divergence blocks short (conf={cvd_confidence:.2f})",
                            direction["signals"], tradeability["score"]
                        )

            # Whale Gate: block if whale pressure opposes direction
            if flow_cfg.get("whale_gate", False):
                whale_info = flow_data.get("whale_trades", {})
                whale_pressure = whale_info.get("whale_pressure", 0)
                whale_thresh = flow_cfg.get("whale_pressure_threshold", 0.5)

                if direction_bias == "long" and whale_pressure < -whale_thresh:
                    return _no_trade(
                        symbol, mode,
                        f"Flow: whale sell pressure {whale_pressure:.2f} blocks long",
                        direction["signals"], tradeability["score"]
                    )
                elif direction_bias == "short" and whale_pressure > whale_thresh:
                    return _no_trade(
                        symbol, mode,
                        f"Flow: whale buy pressure {whale_pressure:.2f} blocks short",
                        direction["signals"], tradeability["score"]
                    )

            # Sweep Gate: block if aggressive sweep opposes direction
            if flow_cfg.get("sweep_gate", False):
                sweep = flow_data.get("microstructure", {}).get("sweep", {})
                if sweep.get("sweep_detected") and sweep.get("sweep_intensity", 0) > 0.4:
                    sd = sweep["sweep_direction"]
                    if direction_bias == "long" and sd == "sell":
                        return _no_trade(
                            symbol, mode,
                            f"Flow: SELL SWEEP blocks long (intensity={sweep['sweep_intensity']:.2f})",
                            direction["signals"], tradeability["score"]
                        )
                    elif direction_bias == "short" and sd == "buy":
                        return _no_trade(
                            symbol, mode,
                            f"Flow: BUY SWEEP blocks short (intensity={sweep['sweep_intensity']:.2f})",
                            direction["signals"], tradeability["score"]
                        )

            # OI Divergence Gate: block longs on fake pump
            if flow_cfg.get("oi_divergence_gate", False):
                oi_div = flow_data.get("oi_divergence", {})
                oi_sig = oi_div.get("signal", "neutral")
                if direction_bias == "long" and oi_sig == "fake_pump":
                    return _no_trade(
                        symbol, mode,
                        f"Flow: OI divergence fake pump blocks long (OI {oi_div.get('oi_change', 0):+.1f}%)",
                        direction["signals"], tradeability["score"]
                    )

            # Smart Money Gate: block if smart money strongly opposes
            if flow_cfg.get("smart_money_gate", False):
                sm = flow_data.get("smart_money", {})
                if sm.get("divergence"):
                    if direction_bias == "long" and sm.get("signal") == "smart_short":
                        return _no_trade(
                            symbol, mode,
                            f"Flow: smart money SHORT blocks long (top ratio={sm.get('top_account_ratio', 0):.2f})",
                            direction["signals"], tradeability["score"]
                        )
                    elif direction_bias == "short" and sm.get("signal") == "smart_long":
                        return _no_trade(
                            symbol, mode,
                            f"Flow: smart money LONG blocks short (top ratio={sm.get('top_account_ratio', 0):.2f})",
                            direction["signals"], tradeability["score"]
                        )

            # Session Edge Gate
            session_edge = flow_data.get("session_edge", {})
            if session_edge.get("gate", False):
                se_stats = session_edge.get("stats", {})
                return _no_trade(
                    symbol, mode,
                    f"Session gate: WR {se_stats.get('wr', 0)}% in {session_edge.get('session', '?')} session ({se_stats.get('total', 0)} trades)",
                    direction["signals"], tradeability["score"]
                )

        # Gate 3: Momentum (last 4 candles)
        if sniper_cfg.get("momentum_gate", False):
            min_candles = sniper_cfg.get("momentum_min_candles", 3)
            last_4 = df_analysis.tail(4)
            if len(last_4) >= 4:
                green = sum(1 for _, c in last_4.iterrows() if c["close"] > c["open"])
                red = 4 - green
                if direction_bias == "long" and green < min_candles:
                    return _no_trade(
                        symbol, mode,
                        f"Sniper: momentum {green}/4 green < {min_candles}",
                        direction["signals"], tradeability["score"]
                    )
                elif direction_bias == "short" and red < min_candles:
                    return _no_trade(
                        symbol, mode,
                        f"Sniper: momentum {red}/4 red < {min_candles}",
                        direction["signals"], tradeability["score"]
                    )

    # =========================================
    # V4 ONLY: REGIME DE MARCHE + MTF CONFLUENCE
    # =========================================
    v4f = s.get("v4_features", {})
    regime_info = {}
    market_regime = ""
    mtf_confluence = 0.0
    if is_v4 and v4f.get("regime_detection", False):
        regime_info = detect_regime(indicators_analysis)
        market_regime = regime_info["regime"]
    if is_v4 and v4f.get("mtf_confluence", False):
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
    entry = find_best_entry(indicators_analysis, df_analysis, direction_bias, allowed_setups, entry_cfg=s.get("entry"))

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
    if is_v4 and v4f.get("regime_detection", False) and regime_info:
        regime_mod = regime_score_modifier(market_regime, entry["type"], regime_info.get("confidence", 0))
        setup_score = max(0, min(100, setup_score + regime_mod))
    if is_v4 and v4f.get("mtf_confluence", False):
        mtf_modifier = int(mtf_confluence)
    if is_v4 and v4f.get("vwap_confluence", False):
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

    # V4 with features ON: Mode-specific weights
    # V4 with features OFF: use standard YAML weights (same as V1/V2/V3)
    v4_has_active_features = is_v4 and any(v4f.get(k, False) for k in ("regime_detection", "mtf_confluence", "vwap_confluence", "adaptive_learning"))
    if v4_has_active_features and mode == "scalping":
        w_trade = 0.20
        w_dir = 0.30
        w_setup = 0.45
        w_sent = 0.05
    elif v4_has_active_features and mode == "swing":
        w_trade = 0.20
        w_dir = 0.25
        w_setup = 0.40
        w_sent = 0.15
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

    # V4 with active features: early reject if base score is too far below threshold
    if v4_has_active_features:
        base_score = max(0, min(100, final_score))
        if base_score < min_score - 10:
            return _no_trade(
                symbol, mode,
                f"V4 base score {base_score} < {min_score - 10} (too far below threshold)",
                direction["signals"], tradeability["score"]
            )

    # V4 ONLY: MTF + VWAP + Learning modifiers
    learning_modifier = 0
    learning_reasons = []
    _candle_pattern = "none"
    flow_modifier = 0
    flow_modifier_reasons = []
    if is_v4:
        final_score += mtf_modifier + vwap_modifier

        if v4f.get("adaptive_learning", False):
            adaptive_learner = _get_adaptive_learner("V4")
            if adaptive_learner:
                now = datetime.utcnow()
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

        # Flow Intelligence Modifiers (v2 — all sources)
        flow_data = market_data_dict.get("flow_intelligence")
        flow_cfg = s.get("flow_intelligence", {})
        if flow_data and v4f.get("order_flow", False) and not flow_data.get("is_stale", True):
            _dir = entry["direction"]

            # +5 pts if CVD confirms direction
            cvd_info = flow_data.get("cvd", {})
            cvd_signal = cvd_info.get("signal", "neutral")
            cvd_conf = cvd_info.get("confidence", 0)
            bonus = flow_cfg.get("flow_confirmation_bonus", 5)
            if _dir == "long" and cvd_signal == "bullish_confirmation" and cvd_conf >= 0.2:
                flow_modifier += bonus
                flow_modifier_reasons.append(f"CVD confirms long +{bonus}")
            elif _dir == "short" and cvd_signal == "bearish_confirmation" and cvd_conf >= 0.2:
                flow_modifier += bonus
                flow_modifier_reasons.append(f"CVD confirms short +{bonus}")

            # +5 pts if VPIN confirms direction (informed traders aligned)
            vpin = flow_data.get("microstructure", {}).get("vpin", {})
            vpin_val = vpin.get("vpin", 0.5)
            vpin_bias = vpin.get("bias", "neutral")
            vpin_conf = vpin.get("confidence", 0)
            if vpin_val > 0.6 and vpin_conf >= 0.3:
                if (_dir == "long" and vpin_bias == "buy") or (_dir == "short" and vpin_bias == "sell"):
                    flow_modifier += 5
                    flow_modifier_reasons.append(f"VPIN {vpin_val:.2f} confirms {_dir} +5")

            # +5 pts if sweep aligns with direction
            sweep = flow_data.get("microstructure", {}).get("sweep", {})
            if sweep.get("sweep_detected"):
                sd = sweep.get("sweep_direction", "none")
                if (_dir == "long" and sd == "buy") or (_dir == "short" and sd == "sell"):
                    pts = min(5, int(sweep.get("sweep_intensity", 0) * 8))
                    if pts > 0:
                        flow_modifier += pts
                        flow_modifier_reasons.append(f"Sweep {sd} confirms {_dir} +{pts}")

            # +4 pts if OI confirms direction
            oi_div = flow_data.get("oi_divergence", {})
            oi_sig = oi_div.get("signal", "neutral")
            if _dir == "long" and oi_sig == "bullish_continuation":
                flow_modifier += 4
                flow_modifier_reasons.append("OI bullish continuation +4")
            elif _dir == "short" and oi_sig == "bearish_continuation":
                flow_modifier += 4
                flow_modifier_reasons.append("OI bearish continuation +4")

            # +3 pts if taker volume confirms
            taker = flow_data.get("taker_volume", {})
            taker_ratio = taker.get("ratio", 1)
            if _dir == "long" and taker_ratio > 1.15:
                flow_modifier += 3
                flow_modifier_reasons.append(f"Taker buy {taker_ratio:.2f} +3")
            elif _dir == "short" and taker_ratio < 0.85:
                flow_modifier += 3
                flow_modifier_reasons.append(f"Taker sell {taker_ratio:.2f} +3")

            # +3 pts if L/S ratio is contrarian (crowd on wrong side)
            ls_info = flow_data.get("long_short_ratio", {})
            ls_ratio_val = ls_info.get("ratio", 1)
            ls_thresh = flow_cfg.get("ls_extreme_threshold", 2.5)
            ls_pts = flow_cfg.get("ls_modifier_points", 3)
            if flow_cfg.get("ls_ratio_enabled", False):
                if _dir == "short" and ls_ratio_val > ls_thresh:
                    flow_modifier += ls_pts
                    flow_modifier_reasons.append(f"L/S contrarian long {ls_ratio_val:.1f} +{ls_pts}")
                elif _dir == "long" and ls_ratio_val < (1 / ls_thresh):
                    flow_modifier += ls_pts
                    flow_modifier_reasons.append(f"L/S contrarian short {ls_ratio_val:.2f} +{ls_pts}")

            # +3 pts tape speed acceleration + aligned flow
            tape = flow_data.get("microstructure", {}).get("tape_speed", {})
            if tape.get("acceleration", 1) > 3.0:
                d5m = flow_data.get("deltas", {}).get("5m", {})
                if _dir == "long" and d5m.get("ratio", 0.5) > 0.55:
                    flow_modifier += 3
                    flow_modifier_reasons.append(f"Tape accel {tape['acceleration']:.1f}x + buy +3")
                elif _dir == "short" and d5m.get("ratio", 0.5) < 0.45:
                    flow_modifier += 3
                    flow_modifier_reasons.append(f"Tape accel {tape['acceleration']:.1f}x + sell +3")
            elif tape.get("acceleration", 1) < 0.3:
                flow_modifier -= 3
                flow_modifier_reasons.append("Dead tape -3")

            # +3 pts funding momentum contrarian
            fm = flow_data.get("funding_momentum", {})
            if fm.get("extreme"):
                if _dir == "short" and fm.get("current", 0) > 0.05:
                    flow_modifier += 3
                    flow_modifier_reasons.append(f"Extreme funding short contrarian +3")
                elif _dir == "long" and fm.get("current", 0) < -0.03:
                    flow_modifier += 3
                    flow_modifier_reasons.append(f"Extreme funding long contrarian +3")

            # +2 pts basis confirms
            basis = flow_data.get("basis", {})
            basis_pct = basis.get("basis_pct", 0)
            if _dir == "long" and basis_pct > 0.05:
                flow_modifier += 2
                flow_modifier_reasons.append(f"Basis premium {basis_pct:.3f}% +2")
            elif _dir == "short" and basis_pct < -0.03:
                flow_modifier += 2
                flow_modifier_reasons.append(f"Basis discount {basis_pct:.3f}% +2")

            # +2 pts if near liquidation cluster
            if flow_cfg.get("liquidation_levels_enabled", False):
                liq_info = flow_data.get("liquidation_levels", {})
                levels = liq_info.get("levels", {})
                for lev_key in ("10x", "25x"):
                    lev_data = levels.get(lev_key, {})
                    if _dir == "long":
                        dist = lev_data.get("short_dist_pct", 999)
                        if 0 < dist < 1.5:
                            flow_modifier += 2
                            flow_modifier_reasons.append(f"Near {lev_key} short liq {dist:.1f}% +2")
                            break
                    elif _dir == "short":
                        dist = lev_data.get("long_dist_pct", 999)
                        if 0 < dist < 1.5:
                            flow_modifier += 2
                            flow_modifier_reasons.append(f"Near {lev_key} long liq {dist:.1f}% +2")
                            break

            # Session edge modifier
            se = flow_data.get("session_edge", {})
            se_mod = se.get("modifier", 0)
            if se_mod != 0:
                flow_modifier += se_mod
                flow_modifier_reasons.append(f"Session {se.get('session', '?')} edge {se_mod:+d}")

            final_score += flow_modifier

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
    if flow_modifier != 0:
        reasons.append(f"Flow {flow_modifier:+d}pts ({', '.join(flow_modifier_reasons)})")

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
            "flow_modifier": flow_modifier,
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
