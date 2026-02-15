"""
API REST endpoints.
"""
import asyncio
from fastapi import APIRouter, Query
from app.database import get_signals, get_trades, get_stats, get_active_positions
from app.core.scanner import scanner
from app.core.market_data import market_data
from app.config import get_enabled_pairs, SETTINGS, APP_MODE, reload_settings, get_mode_config
from app.core.signal_engine import analyze_pair

router = APIRouter(prefix="/api")


@router.get("/status")
async def get_status():
    return {
        "status": "running" if scanner.running else "stopped",
        "mode": APP_MODE,
        "scanner": scanner.get_status(),
    }


@router.get("/signals")
async def list_signals(
    limit: int = Query(50, ge=1, le=500),
    symbol: str = Query(None),
    mode: str = Query(None),
):
    signals = await get_signals(limit=limit, symbol=symbol, mode=mode)
    return {"signals": signals, "count": len(signals)}


@router.get("/trades")
async def list_trades(limit: int = Query(50, ge=1, le=500)):
    trades = await get_trades(limit=limit)
    return {"trades": trades, "count": len(trades)}


@router.get("/stats")
async def trading_stats():
    stats = await get_stats()
    return stats


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
async def debug_pair(symbol: str, mode: str = Query("scalping")):
    """Debug: analyse une paire et retourne le resultat complet."""
    symbol_fmt = symbol.replace("-", "/")
    mode_cfg = get_mode_config(mode)
    if not mode_cfg:
        return {"error": "Mode inconnu"}

    tfs = mode_cfg["timeframes"]["analysis"] + [mode_cfg["timeframes"]["filter"]]
    data = await market_data.fetch_all_data(symbol_fmt, tfs)
    result = await analyze_pair(symbol_fmt, data, mode)
    return result


@router.post("/execute/{signal_id}")
async def execute_from_web(signal_id: int, body: dict = {}):
    """Execute un signal en paper trading depuis le dashboard."""
    from app.database import get_signal_by_id, update_signal_status, get_paper_portfolio
    from app.core.paper_trader import paper_trader
    from app.core.position_monitor import position_monitor
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

    # Verifier le solde paper
    portfolio = await get_paper_portfolio()
    available = portfolio["current_balance"] - portfolio["reserved_margin"]
    if margin_usdt > available:
        return {"success": False, "error": f"Solde insuffisant ({available:.2f}$ dispo)"}

    # Executer en paper trading
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

    # Enregistrer dans le position monitor pour suivi temps reel
    pos_id = await position_monitor.register_trade(signal_data, fake_result)
    if pos_id is None:
        return {"success": False, "error": "Position deja active sur ce symbol"}

    # Reserver la marge paper
    from app.database import reserve_paper_margin
    await reserve_paper_margin(margin_usdt)
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


@router.get("/sentiment")
async def get_sentiment():
    """Retourne le sentiment actuel du marche."""
    from app.services.sentiment import sentiment_analyzer
    sentiment = await sentiment_analyzer.get_sentiment()
    return sentiment


@router.get("/positions")
async def list_positions():
    positions = await get_active_positions()
    return {"positions": positions, "count": len(positions)}


@router.get("/positions/live")
async def live_positions():
    """Positions actives avec prix en temps reel et P&L."""
    positions = await get_active_positions()
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

        # P&L realise (TP deja touches)
        realized_pnl = 0.0
        if pos.get("tp1_hit"):
            tp1_qty = original_qty * (pos.get("tp1_close_pct", 40) / 100)
            diff = (pos["tp1"] - entry) if direction == "long" else (entry - pos["tp1"])
            realized_pnl += diff * tp1_qty
        if pos.get("tp2_hit"):
            tp2_qty = original_qty * (pos.get("tp2_close_pct", 30) / 100)
            diff = (pos["tp2"] - entry) if direction == "long" else (entry - pos["tp2"])
            realized_pnl += diff * tp2_qty

        # P&L non realise (position restante)
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
async def paper_portfolio():
    """Retourne le portefeuille paper trading."""
    from app.database import get_paper_portfolio
    portfolio = await get_paper_portfolio()
    return portfolio


@router.post("/paper/reset")
async def paper_reset():
    """Remet le portefeuille paper a zero (100$)."""
    from app.database import reset_paper_portfolio
    from app.core.paper_trader import paper_trader
    await reset_paper_portfolio(100.0)
    paper_trader._open_positions.clear()
    return {"status": "ok", "message": "Portfolio reset a 100$"}


@router.post("/positions/{position_id}/close")
async def close_position_manual(position_id: int, body: dict = {}):
    """Ferme manuellement une position au prix actuel."""
    from app.database import get_active_positions, close_position, insert_trade, update_paper_balance
    from app.core.paper_trader import paper_trader
    from app.core.position_monitor import position_monitor
    from datetime import datetime

    # Trouver la position
    positions = await get_active_positions()
    pos = next((p for p in positions if p["id"] == position_id), None)
    if not pos:
        return {"success": False, "error": "Position introuvable ou deja fermee"}

    # Utiliser le prix envoye par le frontend (WS live) sinon fetcher
    current_price = body.get("price", 0)
    if not current_price or current_price <= 0:
        try:
            ticker = await market_data.fetch_ticker(pos["symbol"])
            current_price = ticker.get("price", 0)
        except Exception:
            current_price = 0

    if current_price <= 0:
        return {"success": False, "error": "Impossible de recuperer le prix actuel"}

    # Calculer le P&L
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

    # Fermer la position dans la DB
    now = datetime.utcnow().isoformat()
    pnl_pct = (total_pnl / pos.get("margin_required", 1)) * 100 if pos.get("margin_required") else 0
    result = "win" if total_pnl > 0 else "loss"

    await close_position(position_id, {
        "closed_at": now,
        "close_reason": "manual",
        "pnl_usd": round(total_pnl, 4),
    })

    # Journaliser le trade
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
    })

    # Mettre a jour le paper portfolio
    margin = paper_trader._open_positions.pop(position_id, pos.get("margin_required", 0))
    if margin:
        await update_paper_balance(total_pnl, total_pnl > 0, margin)

    # Retirer du cache du position_monitor
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
    """Lance une vraie analyse et envoie le resultat comme signal."""
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
        # Pas de signal valide, forcer un signal avec le prix actuel
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

    # Verifier que le signal a des prix valides
    if not result.get("entry_price") or result["entry_price"] <= 0:
        return {"status": "error", "message": "Analyse n'a pas produit de prix valide"}

    signal_id = await insert_signal(result)
    result["id"] = signal_id
    await send_signal(result)

    return {"status": "ok", "message": f"Signal {result['direction'].upper()} {symbol}", "signal_id": signal_id}
