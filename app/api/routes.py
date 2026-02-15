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


@router.post("/test-signal")
async def send_test_signal():
    """Envoie un faux signal sur Telegram pour tester le flow (rien n'est execute)."""
    from app.database import insert_signal
    from app.services.telegram_bot import send_signal

    # Recuperer le prix reel de SOL pour que ce soit realiste
    ticker = await market_data.fetch_ticker("SOL/USDT:USDT")
    price = ticker.get("price", 190.0)

    test_signal = {
        "type": "signal",
        "symbol": "SOL/USDT:USDT",
        "mode": "scalping",
        "direction": "long",
        "score": 72,
        "entry_price": round(price, 2),
        "stop_loss": round(price * 0.998, 2),
        "tp1": round(price * 1.002, 2),
        "tp2": round(price * 1.003, 2),
        "tp3": round(price * 1.005, 2),
        "setup_type": "ema_bounce",
        "leverage": 20,
        "risk_pct": 0.15,
        "rr_ratio": 1.5,
        "tp1_close_pct": 40,
        "tp2_close_pct": 30,
        "tp3_close_pct": 30,
        "reasons": [
            "EMA20 > EMA50 (+1.2%) + prix au-dessus",
            "Structure: Higher Highs + Higher Lows",
            "RSI 58.3 > 55 (momentum haussier)",
            "EMA bounce : prix rebondit sur EMA20",
            "Funding rate -0.0085%",
        ],
    }

    # Sauvegarder avec status "test" pour que l'execution soit bloquee
    signal_id = await insert_signal({**test_signal, "status_override": "test"})
    test_signal["id"] = signal_id

    # Marquer comme test dans la DB
    from app.database import update_signal_status
    await update_signal_status(signal_id, "test")

    # Envoyer sur Telegram avec les boutons
    await send_signal(test_signal)

    return {"status": "ok", "message": "Signal test envoye sur Telegram", "signal_id": signal_id}
