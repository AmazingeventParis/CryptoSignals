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

CREATE TABLE IF NOT EXISTS active_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    tp1 REAL NOT NULL,
    tp2 REAL NOT NULL,
    tp3 REAL NOT NULL,
    original_quantity REAL NOT NULL,
    remaining_quantity REAL NOT NULL,
    tp1_close_pct INTEGER DEFAULT 40,
    tp2_close_pct INTEGER DEFAULT 30,
    tp3_close_pct INTEGER DEFAULT 30,
    leverage INTEGER DEFAULT 10,
    position_size_usd REAL,
    margin_required REAL,
    sl_order_id TEXT,
    tp1_order_id TEXT,
    tp2_order_id TEXT,
    tp3_order_id TEXT,
    entry_order_id TEXT,
    state TEXT DEFAULT 'active',
    tp1_hit INTEGER DEFAULT 0,
    tp2_hit INTEGER DEFAULT 0,
    tp3_hit INTEGER DEFAULT 0,
    sl_hit INTEGER DEFAULT 0,
    mode TEXT,
    entry_time TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    close_reason TEXT,
    pnl_usd REAL,
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


async def get_signal_by_id(signal_id: int) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_signal_status(signal_id: int, status: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE signals SET status = ? WHERE id = ?", (status, signal_id)
        )
        await db.commit()


async def get_latest_active_signal() -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM signals WHERE status IN ('active', 'test') ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


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


# --- Active Positions (trailing stop) ---

async def insert_active_position(pos: dict) -> int:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """INSERT INTO active_positions
            (signal_id, symbol, direction, entry_price, stop_loss,
             tp1, tp2, tp3, original_quantity, remaining_quantity,
             tp1_close_pct, tp2_close_pct, tp3_close_pct,
             leverage, position_size_usd, margin_required,
             sl_order_id, tp1_order_id, tp2_order_id, tp3_order_id,
             entry_order_id, mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos.get("signal_id"),
                pos["symbol"], pos["direction"], pos["entry_price"], pos["stop_loss"],
                pos["tp1"], pos["tp2"], pos["tp3"],
                pos["original_quantity"], pos["remaining_quantity"],
                pos.get("tp1_close_pct", 40), pos.get("tp2_close_pct", 30), pos.get("tp3_close_pct", 30),
                pos.get("leverage", 10),
                pos.get("position_size_usd"), pos.get("margin_required"),
                pos.get("sl_order_id"), pos.get("tp1_order_id"),
                pos.get("tp2_order_id"), pos.get("tp3_order_id"),
                pos.get("entry_order_id"), pos.get("mode"),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_positions() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM active_positions WHERE state != 'closed' ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_position(position_id: int, updates: dict):
    if not updates:
        return
    set_clauses = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [position_id]
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE active_positions SET {set_clauses} WHERE id = ?",
            values,
        )
        await db.commit()


async def close_position(position_id: int, updates: dict):
    updates["state"] = "closed"
    await update_position(position_id, updates)
