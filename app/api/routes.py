"""
API REST endpoints.
"""
from fastapi import APIRouter, Query
from app.database import get_signals, get_trades, get_stats
from app.core.scanner import scanner
from app.core.market_data import market_data
from app.config import get_enabled_pairs, SETTINGS, APP_MODE, reload_settings

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


@router.post("/config/reload")
async def reload_config():
    reload_settings()
    return {"status": "ok", "message": "Configuration rechargee"}
