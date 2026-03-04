"""
Session Edge : Analyse statistique des performances par session de trading.
Calcule le win rate par symbol/session a partir de l'historique des trades.
V4 only.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Sessions UTC
SESSIONS = {
    "asian": (0, 8),      # 00:00-08:00 UTC
    "european": (8, 16),  # 08:00-16:00 UTC
    "us": (16, 24),       # 16:00-24:00 UTC
}

MIN_TRADES_FOR_EDGE = 10  # Minimum trades to compute edge


def get_session(hour_utc: int) -> str:
    """Get session name from UTC hour."""
    for name, (start, end) in SESSIONS.items():
        if start <= hour_utc < end:
            return name
    return "asian"


class SessionEdge:
    """
    Tracks win rate per (symbol, session) from trade history.
    Refreshes cache periodically from database.
    """

    def __init__(self, bot_version: str = "V4"):
        self.bot_version = bot_version
        # Cache: {(symbol, session): {wins, losses, wr, total, avg_pnl}}
        self._cache: dict[tuple[str, str], dict] = {}
        self._last_refresh = 0

    async def refresh_cache(self):
        """Load trade history and compute per-session stats."""
        from app.database import get_trades
        try:
            trades = await get_trades(limit=500, bot_version=self.bot_version)
            if not trades:
                return

            stats: dict[tuple[str, str], dict] = {}

            for t in trades:
                symbol = t.get("symbol", "")
                exit_time = t.get("exit_time", "")
                result = t.get("result", "")
                pnl = t.get("pnl_usd", 0)

                if not exit_time or not symbol:
                    continue

                try:
                    dt = datetime.fromisoformat(exit_time)
                    session = get_session(dt.hour)
                except Exception:
                    continue

                key = (symbol, session)
                if key not in stats:
                    stats[key] = {"wins": 0, "losses": 0, "total_pnl": 0}

                if result == "win":
                    stats[key]["wins"] += 1
                elif result == "loss":
                    stats[key]["losses"] += 1
                stats[key]["total_pnl"] += pnl

            # Compute derived metrics
            self._cache.clear()
            for key, s in stats.items():
                total = s["wins"] + s["losses"]
                wr = s["wins"] / max(total, 1) * 100
                self._cache[key] = {
                    "wins": s["wins"],
                    "losses": s["losses"],
                    "total": total,
                    "wr": round(wr, 1),
                    "avg_pnl": round(s["total_pnl"] / max(total, 1), 4),
                    "total_pnl": round(s["total_pnl"], 4),
                }

            self._last_refresh = datetime.utcnow().timestamp()
            logger.info(f"SessionEdge refreshed: {len(self._cache)} entries")

        except Exception as e:
            logger.warning(f"SessionEdge refresh error: {e}")

    def get_edge(self, symbol: str, hour_utc: int = None) -> dict:
        """
        Get session edge for a symbol at current time.
        Returns: gate (bool), modifier (int), stats (dict).
        """
        if hour_utc is None:
            hour_utc = datetime.utcnow().hour

        session = get_session(hour_utc)
        key = (symbol, session)
        stats = self._cache.get(key)

        if not stats or stats["total"] < MIN_TRADES_FOR_EDGE:
            return {
                "session": session,
                "has_data": False,
                "gate": False,
                "modifier": 0,
                "stats": stats or {},
            }

        wr = stats["wr"]

        # Gate: block if WR < 30% on 10+ trades
        gate = wr < 30 and stats["total"] >= MIN_TRADES_FOR_EDGE

        # Modifier: +3 if WR > 65%, -2 if WR < 40%
        modifier = 0
        if wr >= 65 and stats["total"] >= MIN_TRADES_FOR_EDGE:
            modifier = 3
        elif wr <= 40 and stats["total"] >= MIN_TRADES_FOR_EDGE:
            modifier = -2

        return {
            "session": session,
            "has_data": True,
            "gate": gate,
            "modifier": modifier,
            "stats": stats,
        }

    def get_all_stats(self) -> dict:
        """Get all session edge data for dashboard."""
        result = {}
        for (symbol, session), stats in self._cache.items():
            if symbol not in result:
                result[symbol] = {}
            result[symbol][session] = stats
        return result
