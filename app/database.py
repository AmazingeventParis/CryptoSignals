import aiosqlite
import json
from datetime import datetime
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    direction TEXT NOT NULL,
    score INTEGER NOT NULL,
    entry_price REAL,
    stop_loss REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    setup_type TEXT,
    leverage INTEGER,
    reasons TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    stop_loss REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    leverage INTEGER,
    position_size_usd REAL,
    pnl_usd REAL,
    pnl_pct REAL,
    result TEXT,
    entry_time TEXT,
    exit_time TEXT,
    duration_seconds INTEGER,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tradeability_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    score REAL NOT NULL,
    is_tradable INTEGER NOT NULL,
    details TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    volume_24h REAL,
    spread_pct REAL,
    funding_rate REAL,
    open_interest REAL,
    atr REAL,
    rsi REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


async def init_db():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def insert_signal(signal: dict) -> int:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """INSERT INTO signals
            (timestamp, symbol, mode, direction, score, entry_price,
             stop_loss, tp1, tp2, tp3, setup_type, leverage, reasons, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                signal["symbol"],
                signal["mode"],
                signal["direction"],
                signal["score"],
                signal.get("entry_price"),
                signal.get("stop_loss"),
                signal.get("tp1"),
                signal.get("tp2"),
                signal.get("tp3"),
                signal.get("setup_type"),
                signal.get("leverage"),
                json.dumps(signal.get("reasons", []), ensure_ascii=False),
                "active",
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def insert_trade(trade: dict):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO trades_journal
            (signal_id, symbol, mode, direction, entry_price, exit_price,
             stop_loss, tp1, tp2, tp3, leverage, position_size_usd,
             pnl_usd, pnl_pct, result, entry_time, exit_time,
             duration_seconds, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.get("signal_id"),
                trade["symbol"],
                trade["mode"],
                trade["direction"],
                trade.get("entry_price"),
                trade.get("exit_price"),
                trade.get("stop_loss"),
                trade.get("tp1"),
                trade.get("tp2"),
                trade.get("tp3"),
                trade.get("leverage"),
                trade.get("position_size_usd"),
                trade.get("pnl_usd"),
                trade.get("pnl_pct"),
                trade.get("result"),
                trade.get("entry_time"),
                trade.get("exit_time"),
                trade.get("duration_seconds"),
                trade.get("notes"),
            ),
        )
        await db.commit()


async def log_tradeability(symbol: str, score: float, is_tradable: bool, details: dict):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO tradeability_log (timestamp, symbol, score, is_tradable, details)
            VALUES (?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                symbol,
                score,
                1 if is_tradable else 0,
                json.dumps(details, ensure_ascii=False),
            ),
        )
        await db.commit()


async def get_signals(limit: int = 50, symbol: str = None, mode: str = None) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_trades(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM trades_journal ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_stats() -> dict:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        total = await (await db.execute("SELECT COUNT(*) as c FROM trades_journal")).fetchone()
        wins = await (
            await db.execute("SELECT COUNT(*) as c FROM trades_journal WHERE result = 'win'")
        ).fetchone()
        losses = await (
            await db.execute("SELECT COUNT(*) as c FROM trades_journal WHERE result = 'loss'")
        ).fetchone()
        pnl = await (
            await db.execute("SELECT COALESCE(SUM(pnl_usd), 0) as total FROM trades_journal")
        ).fetchone()
        return {
            "total_trades": total["c"],
            "wins": wins["c"],
            "losses": losses["c"],
            "win_rate": round(wins["c"] / max(total["c"], 1) * 100, 1),
            "total_pnl_usd": round(pnl["total"], 2),
        }
