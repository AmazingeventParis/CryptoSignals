"""
API REST endpoints.
"""
import asyncio
import httpx
from fastapi import APIRouter, Query
from app.database import get_signals, get_trades, get_stats, get_active_positions
from app.core.market_data import market_data
from app.config import get_enabled_pairs, SETTINGS, SETTINGS_V1, SETTINGS_V2, SETTINGS_V3, SETTINGS_V4, APP_MODE, reload_settings, get_mode_config

router = APIRouter(prefix="/api")

# --- Freqtrade proxy config ---
FT_URL = "https://freqtrade.swipego.app"
FT_AUTH = ("admin", "Laurytal2")


def _get_bot_instances():
    from app.main import bot_instances
    return bot_instances


@router.get("/status")
async def get_status():
    bots = _get_bot_instances()
    return {
        "status": "running",
        "mode": APP_MODE,
        "scanners": {
            "V1": bots["V1"]["scanner"].get_status(),
            "V2": bots["V2"]["scanner"].get_status(),
            "V3": bots["V3"]["scanner"].get_status(),
            "V4": bots["V4"]["scanner"].get_status(),
        },
    }


@router.get("/signals")
async def list_signals(
    limit: int = Query(50, ge=1, le=500),
    symbol: str = Query(None),
    mode: str = Query(None),
    bot_version: str = Query(None),
):
    signals = await get_signals(limit=limit, symbol=symbol, mode=mode, bot_version=bot_version)
    return {"signals": signals, "count": len(signals)}


@router.get("/trades")
async def list_trades(
    limit: int = Query(50, ge=1, le=500),
    bot_version: str = Query(None),
):
    trades = await get_trades(limit=limit, bot_version=bot_version)
    return {"trades": trades, "count": len(trades)}


@router.get("/stats")
async def trading_stats(bot_version: str = Query(None)):
    stats = await get_stats(bot_version=bot_version)
    return stats


@router.get("/stats/window")
async def stats_window(hours: int = Query(24, ge=1, le=720)):
    from app.database import get_stats_window
    rows = await get_stats_window(hours=hours)
    result = {}
    for r in rows:
        bv = r["bot_version"]
        total = r["total_trades"]
        wins = r["wins"]
        result[bv] = {
            "total_trades": total,
            "wins": wins,
            "losses": r["losses"],
            "win_rate": round(wins / max(total, 1) * 100, 1),
            "total_pnl": round(r["total_pnl"], 2),
            "avg_pnl": round(r["avg_pnl"], 3),
            "best_trade": round(r["best_trade"], 2),
            "worst_trade": round(r["worst_trade"], 2),
        }
    return {"hours": hours, "stats": result}


@router.get("/pnl-history")
async def pnl_history(
    bot_version: str = Query(None),
    days: int = Query(0, ge=0, le=365),
):
    from app.database import get_pnl_history
    history = await get_pnl_history(bot_version=bot_version, days=days)
    return {"history": history, "count": len(history)}


@router.get("/pairs")
async def list_pairs():
    return {"pairs": get_enabled_pairs()}


@router.get("/balance")
async def get_balance():
    balance = await market_data.fetch_balance()
    return balance


@router.get("/market/{symbol}")
async def get_market_data(symbol: str):
    symbol_fmt = symbol.replace("-", "/")
    ticker = await market_data.fetch_ticker(symbol_fmt)
    orderbook = await market_data.fetch_orderbook(symbol_fmt)
    funding = await market_data.fetch_funding_rate(symbol_fmt)
    return {
        "symbol": symbol_fmt,
        "ticker": ticker,
        "orderbook": {
            "spread_pct": orderbook.get("spread_pct"),
            "bid_depth": orderbook.get("bid_depth"),
            "ask_depth": orderbook.get("ask_depth"),
        },
        "funding_rate": funding,
    }


@router.get("/ohlcv/{symbol}")
async def get_ohlcv(
    symbol: str,
    timeframe: str = Query("5m"),
    limit: int = Query(200, ge=10, le=500),
):
    symbol_fmt = symbol.replace("-", "/")
    df = await market_data.fetch_ohlcv(symbol_fmt, timeframe, limit=limit)
    if df.empty:
        return {"candles": [], "symbol": symbol_fmt, "timeframe": timeframe}

    candles = []
    for ts, row in df.iterrows():
        candles.append({
            "time": int(ts.timestamp()),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        })

    return {"candles": candles, "symbol": symbol_fmt, "timeframe": timeframe}


@router.get("/tickers")
async def get_all_tickers():
    pairs = get_enabled_pairs()

    async def fetch_one(pair):
        ticker = await market_data.fetch_ticker(pair)
        ticker["symbol"] = pair
        ticker["name"] = pair.split("/")[0]
        return ticker

    tickers = await asyncio.gather(*[fetch_one(p) for p in pairs])
    return {"tickers": list(tickers)}


@router.post("/config/reload")
async def reload_config():
    reload_settings()
    return {"status": "ok", "message": "Configuration rechargee"}


@router.get("/debug/{symbol}")
async def debug_pair(symbol: str, mode: str = Query("scalping"), bot_version: str = Query("V2")):
    """Debug: analyse une paire et retourne le resultat complet."""
    from app.core.signal_engine import analyze_pair
    symbol_fmt = symbol.replace("-", "/")
    settings_map = {"V1": SETTINGS_V1, "V2": SETTINGS_V2, "V3": SETTINGS_V3, "V4": SETTINGS_V4}
    s = settings_map.get(bot_version, SETTINGS_V2)
    mode_cfg = get_mode_config(mode, s)
    if not mode_cfg:
        return {"error": "Mode inconnu"}

    tfs = mode_cfg["timeframes"]["analysis"] + [mode_cfg["timeframes"]["filter"]]
    data = await market_data.fetch_all_data(symbol_fmt, tfs)
    result = await analyze_pair(symbol_fmt, data, mode, settings=s)
    return result


@router.post("/execute/{signal_id}")
async def execute_from_web(signal_id: int, body: dict = {}):
    """Execute un signal en paper trading depuis le dashboard."""
    from app.database import get_signal_by_id, update_signal_status, get_paper_portfolio
    import json as _json

    margin_usdt = body.get("margin", 10)

    signal_db = await get_signal_by_id(signal_id)
    if not signal_db:
        return {"success": False, "error": "Signal introuvable"}

    if signal_db.get("status") == "executed":
        return {"success": False, "error": "Deja execute"}

    signal_data = {
        **signal_db,
        "type": "signal",
        "reasons": _json.loads(signal_db.get("reasons", "[]")) if isinstance(signal_db.get("reasons"), str) else signal_db.get("reasons", []),
    }

    # Determiner le bot version du signal
    bv = signal_db.get("bot_version", "V2")
    bots = _get_bot_instances()
    bot = bots.get(bv, bots["V2"])
    paper_trader = bot["paper_trader"]
    position_monitor = bot["position_monitor"]

    portfolio = await get_paper_portfolio(bv)
    available = portfolio["current_balance"] - portfolio["reserved_margin"]
    if margin_usdt > available:
        return {"success": False, "error": f"Solde insuffisant ({available:.2f}$ dispo)"}

    lev = signal_data.get("leverage", 10)
    entry_price = signal_data["entry_price"]
    if not entry_price or entry_price <= 0:
        return {"success": False, "error": "Prix d'entree invalide"}
    position_size_usd = margin_usdt * lev
    quantity = round(position_size_usd / entry_price, 6)

    fake_result = {
        "success": True,
        "order_type": "market",
        "entry_order_id": None,
        "actual_entry_price": entry_price,
        "sl_order_id": None,
        "tp_order_ids": [None, None, None],
        "quantity": quantity,
        "position_size_usd": round(position_size_usd, 2),
        "margin_required": round(margin_usdt, 2),
        "balance": round(available - margin_usdt, 2),
    }

    signal_data["bot_version"] = bv
    pos_id = await position_monitor.register_trade(signal_data, fake_result)
    if pos_id is None:
        return {"success": False, "error": "Position deja active sur ce symbol"}

    from app.database import reserve_paper_margin
    await reserve_paper_margin(margin_usdt, bv)
    paper_trader._open_positions[pos_id] = margin_usdt

    await update_signal_status(signal_id, "executed")

    return {
        "success": True,
        "pos_id": pos_id,
        "margin": round(margin_usdt, 2),
        "position_usd": round(position_size_usd, 2),
        "quantity": quantity,
        "entry_price": entry_price,
        "leverage": lev,
        "balance_after": round(available - margin_usdt, 2),
    }


@router.get("/learning")
async def get_learning_stats():
    from app.core.trade_learner import trade_learner
    stats = await trade_learner.get_all_stats()
    return {"stats": stats, "count": len(stats)}


@router.get("/learning/weights")
async def get_learning_weights(bot_version: str = Query("V4")):
    """Tous les poids appris par l'adaptive learner (V4 only)."""
    from app.database import get_all_learning_weights
    weights = await get_all_learning_weights(bot_version)
    return {"weights": weights, "count": len(weights), "bot_version": bot_version}


@router.get("/learning/calibration")
async def get_learning_calibration(bot_version: str = Query("V4")):
    """Win rate par tranche de score (calibration des scores, V4 only)."""
    bots = _get_bot_instances()
    bot = bots.get(bot_version, bots.get("V4", {}))
    learner = bot.get("adaptive_learner")
    if not learner:
        return {"calibration": [], "bot_version": bot_version}
    calibration = await learner.get_calibration()
    return {"calibration": calibration, "bot_version": bot_version}


@router.get("/learning/edge-decay")
async def get_edge_decay(bot_version: str = Query("V4")):
    """Alertes de degradation d'edge (WR 7j chute vs WR 30j, V4 only)."""
    bots = _get_bot_instances()
    bot = bots.get(bot_version, bots.get("V4", {}))
    learner = bot.get("adaptive_learner")
    if not learner:
        return {"alerts": [], "bot_version": bot_version}
    alerts = await learner.get_edge_decay_alerts()
    return {"alerts": alerts, "count": len(alerts), "bot_version": bot_version}


@router.get("/learning/context")
async def get_trade_contexts(
    bot_version: str = Query("V4"),
    limit: int = Query(50, ge=1, le=500),
    days: int = Query(0, ge=0, le=365),
):
    """Historique des contextes de trades pour analyse (V4 only)."""
    from app.database import get_trade_context_window
    contexts = await get_trade_context_window(bot_version, days=days, limit=limit)
    return {"contexts": contexts, "count": len(contexts), "bot_version": bot_version}


@router.get("/learning/decayed-dimensions")
async def get_decayed_dimensions(bot_version: str = Query("V4")):
    """Dimensions en edge decay actif (V4 only)."""
    bots = _get_bot_instances()
    bot = bots.get(bot_version, bots.get("V4", {}))
    learner = bot.get("adaptive_learner")
    if not learner:
        return {"decayed": [], "bot_version": bot_version}
    decayed = learner.get_decayed_dimensions()
    return {"decayed": decayed, "count": len(decayed), "bot_version": bot_version}


@router.get("/stats/advanced")
async def advanced_stats(bot_version: str = Query(None)):
    """Advanced metrics: profit factor, Sharpe ratio, max drawdown, streaks."""
    trades = await get_trades(limit=500, bot_version=bot_version)
    if not trades:
        return {
            "profit_factor": 0, "sharpe_ratio": 0, "max_drawdown_pct": 0,
            "max_win_streak": 0, "max_loss_streak": 0, "avg_win": 0, "avg_loss": 0,
            "total_fees": 0,
        }

    pnls = [t.get("pnl_usd", 0) for t in trades]
    gross_profits = sum(p for p in pnls if p > 0)
    gross_losses = abs(sum(p for p in pnls if p < 0))
    profit_factor = round(gross_profits / max(gross_losses, 0.01), 2)

    # Sharpe ratio (annualized, assuming ~50 trades/day)
    import numpy as np
    if len(pnls) >= 2:
        pnl_array = np.array(pnls)
        mean_pnl = float(np.mean(pnl_array))
        std_pnl = float(np.std(pnl_array))
        sharpe = round(mean_pnl / max(std_pnl, 0.001) * (50 ** 0.5), 2)  # annualized
    else:
        sharpe = 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for p in reversed(pnls):  # oldest first
        cumulative += p
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    initial_balance = 100.0
    max_dd_pct = round(max_dd / max(initial_balance, 1) * 100, 2)

    # Win/loss streaks
    max_win_streak = 0
    max_loss_streak = 0
    current_streak = 0
    current_type = None
    for t in reversed(trades):  # oldest first
        r = t.get("result", "")
        if r == current_type:
            current_streak += 1
        else:
            current_streak = 1
            current_type = r
        if r == "win":
            max_win_streak = max(max_win_streak, current_streak)
        elif r == "loss":
            max_loss_streak = max(max_loss_streak, current_streak)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = round(sum(wins) / max(len(wins), 1), 4)
    avg_loss = round(sum(losses) / max(len(losses), 1), 4)

    # Estimate fees (V4 with 0.06% taker)
    total_fees = 0.0
    for t in trades:
        pos_size = t.get("position_size_usd", 0)
        if pos_size > 0:
            total_fees += pos_size * 0.0006 * 2

    return {
        "profit_factor": profit_factor,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd_pct,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "total_fees": round(total_fees, 2),
        "trade_count": len(trades),
    }


@router.get("/sentiment")
async def get_sentiment():
    from app.services.sentiment import sentiment_analyzer
    sentiment = await sentiment_analyzer.get_sentiment()
    return sentiment


@router.get("/positions")
async def list_positions(bot_version: str = Query(None)):
    positions = await get_active_positions(bot_version=bot_version)
    return {"positions": positions, "count": len(positions)}


@router.get("/positions/live")
async def live_positions(bot_version: str = Query(None)):
    """Positions actives avec prix en temps reel et P&L."""
    positions = await get_active_positions(bot_version=bot_version)
    if not positions:
        return {"positions": [], "count": 0}

    result = []
    for pos in positions:
        symbol = pos["symbol"]
        try:
            ticker = await market_data.fetch_ticker(symbol)
            current_price = ticker.get("price", 0)
        except Exception:
            current_price = 0

        entry = pos["entry_price"]
        direction = pos["direction"]
        remaining_qty = pos["remaining_quantity"]
        original_qty = pos["original_quantity"]

        realized_pnl = 0.0
        if pos.get("tp1_hit"):
            tp1_qty = original_qty * (pos.get("tp1_close_pct", 40) / 100)
            diff = (pos["tp1"] - entry) if direction == "long" else (entry - pos["tp1"])
            realized_pnl += diff * tp1_qty
        if pos.get("tp2_hit"):
            tp2_qty = original_qty * (pos.get("tp2_close_pct", 30) / 100)
            diff = (pos["tp2"] - entry) if direction == "long" else (entry - pos["tp2"])
            realized_pnl += diff * tp2_qty

        if current_price > 0:
            diff = (current_price - entry) if direction == "long" else (entry - current_price)
            unrealized_pnl = diff * remaining_qty
        else:
            unrealized_pnl = 0.0

        total_pnl = realized_pnl + unrealized_pnl
        pnl_pct = (total_pnl / pos.get("margin_required", 1)) * 100 if pos.get("margin_required") else 0

        result.append({
            **pos,
            "current_price": current_price,
            "unrealized_pnl": round(unrealized_pnl, 4),
            "realized_pnl": round(realized_pnl, 4),
            "total_pnl": round(total_pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
        })

    return {"positions": result, "count": len(result)}


@router.get("/paper/portfolio")
async def paper_portfolio(bot_version: str = Query("V2")):
    from app.database import get_paper_portfolio
    portfolio = await get_paper_portfolio(bot_version)
    return portfolio


@router.post("/paper/reset")
async def paper_reset(bot_version: str = Query(None)):
    from app.database import reset_paper_portfolio
    bots = _get_bot_instances()
    if bot_version:
        await reset_paper_portfolio(100.0, bot_version)
        bot = bots.get(bot_version)
        if bot:
            bot["paper_trader"]._open_positions.clear()
        return {"status": "ok", "message": f"Portfolio {bot_version} reset a 100$"}
    else:
        await reset_paper_portfolio(100.0)
        for b in bots.values():
            b["paper_trader"]._open_positions.clear()
        return {"status": "ok", "message": "Tous les portfolios reset a 100$"}


@router.post("/positions/{position_id}/close")
async def close_position_manual(position_id: int, body: dict = {}):
    """Ferme manuellement une position au prix actuel."""
    from app.database import get_active_positions, close_position, insert_trade, update_paper_balance
    from datetime import datetime

    positions = await get_active_positions()
    pos = next((p for p in positions if p["id"] == position_id), None)
    if not pos:
        return {"success": False, "error": "Position introuvable ou deja fermee"}

    bv = pos.get("bot_version", "V2")
    bots = _get_bot_instances()
    bot = bots.get(bv, bots["V2"])
    paper_trader = bot["paper_trader"]
    position_monitor = bot["position_monitor"]

    current_price = body.get("price", 0)
    if not current_price or current_price <= 0:
        try:
            ticker = await market_data.fetch_ticker(pos["symbol"])
            current_price = ticker.get("price", 0)
        except Exception:
            current_price = 0

    if current_price <= 0:
        return {"success": False, "error": "Impossible de recuperer le prix actuel"}

    entry = pos["entry_price"]
    direction = pos["direction"]
    original_qty = pos["original_quantity"]
    remaining_qty = pos["remaining_quantity"]

    realized_pnl = 0.0
    if pos.get("tp1_hit"):
        tp1_qty = original_qty * (pos.get("tp1_close_pct", 40) / 100)
        diff = (pos["tp1"] - entry) if direction == "long" else (entry - pos["tp1"])
        realized_pnl += diff * tp1_qty
    if pos.get("tp2_hit"):
        tp2_qty = original_qty * (pos.get("tp2_close_pct", 30) / 100)
        diff = (pos["tp2"] - entry) if direction == "long" else (entry - pos["tp2"])
        realized_pnl += diff * tp2_qty

    diff = (current_price - entry) if direction == "long" else (entry - current_price)
    unrealized_pnl = diff * remaining_qty
    total_pnl = realized_pnl + unrealized_pnl

    now = datetime.utcnow().isoformat()
    pnl_pct = (total_pnl / pos.get("margin_required", 1)) * 100 if pos.get("margin_required") else 0
    result = "win" if total_pnl > 0 else "loss"

    await close_position(position_id, {
        "closed_at": now,
        "close_reason": "manual",
        "pnl_usd": round(total_pnl, 4),
    })

    entry_time = pos.get("entry_time") or pos.get("created_at")
    duration = 0
    if entry_time:
        try:
            duration = int((datetime.fromisoformat(now) - datetime.fromisoformat(entry_time)).total_seconds())
        except Exception:
            pass

    await insert_trade({
        "signal_id": pos.get("signal_id"),
        "symbol": pos["symbol"],
        "mode": pos.get("mode", "unknown"),
        "direction": pos["direction"],
        "entry_price": entry,
        "exit_price": current_price,
        "stop_loss": pos["stop_loss"],
        "tp1": pos["tp1"],
        "tp2": pos["tp2"],
        "tp3": pos["tp3"],
        "leverage": pos.get("leverage"),
        "position_size_usd": pos.get("position_size_usd"),
        "pnl_usd": round(total_pnl, 4),
        "pnl_pct": round(pnl_pct, 2),
        "result": result,
        "entry_time": entry_time,
        "exit_time": now,
        "duration_seconds": duration,
        "notes": f"manual_close tp1={pos.get('tp1_hit',0)} tp2={pos.get('tp2_hit',0)}",
        "bot_version": bv,
    })

    margin = paper_trader._open_positions.pop(position_id, pos.get("margin_required", 0))
    if margin:
        await update_paper_balance(total_pnl, total_pnl > 0, margin, bv)

    position_monitor._positions.pop(position_id, None)

    return {
        "success": True,
        "pnl_usd": round(total_pnl, 4),
        "pnl_pct": round(pnl_pct, 2),
        "exit_price": current_price,
        "result": result,
    }


@router.post("/test-signal")
async def send_test_signal():
    from app.database import insert_signal
    from app.services.telegram_bot import send_signal
    from app.core.signal_engine import analyze_pair
    from app.config import get_mode_config

    mode = "scalping"
    symbol = "SOL/USDT:USDT"
    mode_cfg = get_mode_config(mode)
    all_tfs = list(set(mode_cfg["timeframes"]["analysis"] + [mode_cfg["timeframes"]["filter"]]))

    data = await market_data.fetch_all_data(symbol, all_tfs)
    result = await analyze_pair(symbol, data, mode)

    if result["type"] != "signal":
        ticker = await market_data.fetch_ticker(symbol)
        price = ticker.get("price", 0)
        if price <= 0:
            return {"status": "error", "message": "Prix indisponible, reessayez"}
        direction = "long" if result.get("tradeability_score", 0) > 50 else "short"
        if direction == "long":
            sl = round(price * 0.998, 2)
            tp1 = round(price * 1.002, 2)
            tp2 = round(price * 1.003, 2)
            tp3 = round(price * 1.005, 2)
        else:
            sl = round(price * 1.002, 2)
            tp1 = round(price * 0.998, 2)
            tp2 = round(price * 0.997, 2)
            tp3 = round(price * 0.995, 2)
        result = {
            "type": "signal",
            "symbol": symbol,
            "mode": mode,
            "direction": direction,
            "score": 65,
            "entry_price": round(price, 2),
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "setup_type": "test",
            "leverage": 20,
            "tp1_close_pct": 40,
            "tp2_close_pct": 30,
            "tp3_close_pct": 30,
            "reasons": result.get("details", ["Signal test"]),
        }

    if not result.get("entry_price") or result["entry_price"] <= 0:
        return {"status": "error", "message": "Analyse n'a pas produit de prix valide"}

    signal_id = await insert_signal(result)
    result["id"] = signal_id
    await send_signal(result)

    return {"status": "ok", "message": f"Signal {result['direction'].upper()} {symbol}", "signal_id": signal_id}


# ============================================================
# FREQTRADE PROXY ENDPOINTS
# ============================================================

@router.get("/freqtrade/openTrades")
async def ft_open_trades():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{FT_URL}/api/v1/status", auth=FT_AUTH)
            trades = r.json()
        result = []
        for t in trades:
            is_short = t.get("is_short", False)
            result.append({
                "id": f"ft_{t['trade_id']}",
                "trade_id": t.get("trade_id"),
                "symbol": t.get("pair", ""),
                "direction": "short" if is_short else "long",
                "entry_price": t.get("open_rate", 0),
                "current_price": t.get("current_rate", 0),
                "pnl_usd": t.get("profit_abs", 0),
                "pnl_pct": t.get("profit_pct", 0),
                "stoploss": t.get("stop_loss_abs", 0),
                "stoploss_pct": t.get("stop_loss_pct", 0),
                "stake_amount": t.get("stake_amount", 0),
                "leverage": t.get("leverage", 1),
                "open_date": t.get("open_date_hum", ""),
                "strategy": t.get("strategy", ""),
                "timeframe": t.get("timeframe", "5m"),
                "min_rate": t.get("min_rate", 0),
                "max_rate": t.get("max_rate", 0),
            })
        return {"trades": result, "count": len(result)}
    except Exception as e:
        return {"trades": [], "count": 0, "error": str(e)}


@router.get("/freqtrade/trades")
async def ft_closed_trades(limit: int = Query(50, ge=1, le=200)):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{FT_URL}/api/v1/trades", params={"limit": limit}, auth=FT_AUTH)
            data = r.json()
        result = []
        for t in data.get("trades", []):
            is_short = t.get("is_short", False)
            result.append({
                "id": f"ft_{t['trade_id']}",
                "symbol": t.get("pair", ""),
                "direction": "short" if is_short else "long",
                "entry_price": t.get("open_rate", 0),
                "exit_price": t.get("close_rate", 0),
                "pnl_usd": t.get("profit_abs", 0),
                "pnl_pct": t.get("profit_pct", 0),
                "result": "win" if (t.get("profit_abs", 0) or 0) > 0 else "loss",
                "open_date": t.get("open_date_hum", "") or t.get("open_date", ""),
                "close_date": t.get("close_date_hum", "") or t.get("close_date", ""),
                "duration": t.get("trade_duration", ""),
                "strategy": t.get("strategy", ""),
                "close_reason": t.get("exit_reason", ""),
            })
        # Trier par close_date decroissant (plus recent d'abord) pour compat avec JS reverse()
        result.sort(key=lambda t: t.get("close_date") or t.get("open_date") or "", reverse=True)
        return {"trades": result, "count": len(result), "total": data.get("total_trades", 0)}
    except Exception as e:
        return {"trades": [], "count": 0, "error": str(e)}


@router.get("/freqtrade/stats")
async def ft_stats():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r_profit = await client.get(f"{FT_URL}/api/v1/profit", auth=FT_AUTH)
            r_balance = await client.get(f"{FT_URL}/api/v1/balance", auth=FT_AUTH)
            profit = r_profit.json()
            balance = r_balance.json()
        return {
            "balance": balance.get("total_bot", 0),
            "total_pnl": profit.get("profit_all_coin", 0),
            "closed_pnl": profit.get("profit_closed_coin", 0),
            "trade_count": profit.get("trade_count", 0),
            "closed_trades": profit.get("closed_trade_count", 0),
            "wins": profit.get("winning_trades", 0),
            "losses": profit.get("losing_trades", 0),
            "win_rate": round(profit.get("winrate", 0) * 100, 1),
            "best_pair": profit.get("best_pair", ""),
            "avg_duration": profit.get("avg_duration", "0:00:00"),
            "drawdown": round(profit.get("max_drawdown", 0) * 100, 2),
            "bot_running": True,
        }
    except Exception as e:
        return {"balance": 0, "total_pnl": 0, "trade_count": 0, "wins": 0, "losses": 0, "win_rate": 0, "bot_running": False, "error": str(e)}
