"""
Scanner : Worker qui tourne en boucle et analyse toutes les paires.
Accepte un name + settings pour supporter V1 et V2 en parallele.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from app.config import SETTINGS, get_enabled_pairs, get_mode_config
from app.core.market_data import market_data
from app.core.signal_engine import analyze_pair
from app.database import insert_signal, log_tradeability

logger = logging.getLogger(__name__)


class Scanner:
    def __init__(self, name="V2", settings=None):
        self.name = name
        self.settings = settings or SETTINGS
        self.running = False
        self.last_signals: dict[str, dict] = {}
        self.cooldowns: dict[str, datetime] = {}
        self.consecutive_losses: dict[str, int] = {}
        self._signal_timestamps: dict[str, datetime] = {}

    async def start(self):
        self.running = True
        interval = self.settings["scanner"]["interval_seconds"]
        pairs = get_enabled_pairs(self.settings)
        logger.info(f"Scanner [{self.name}] demarre - intervalle {interval}s - {len(pairs)} paires")

        while self.running:
            try:
                await self._scan_cycle()
            except Exception as e:
                logger.error(f"[{self.name}] Erreur cycle scan: {e}", exc_info=True)
            await asyncio.sleep(interval)

    async def stop(self):
        self.running = False
        logger.info(f"Scanner [{self.name}] arrete")

    async def _scan_cycle(self):
        if not market_data.is_connected():
            logger.info(f"[{self.name}] Tentative de reconnexion MEXC...")
            await market_data.connect()
            if not market_data.is_connected():
                logger.warning(f"[{self.name}] MEXC toujours indisponible, retry dans 30s")
                return

        pairs = get_enabled_pairs(self.settings)
        modes = self.settings["scanner"]["modes"]

        for symbol in pairs:
            for mode in modes:
                try:
                    key = f"{symbol}_{mode}"
                    if key in self.cooldowns:
                        if datetime.utcnow() < self.cooldowns[key]:
                            continue

                    mode_cfg = get_mode_config(mode, self.settings)
                    all_tfs = mode_cfg["timeframes"]["analysis"] + [mode_cfg["timeframes"]["filter"]]
                    all_tfs = list(set(all_tfs))

                    data = await market_data.fetch_all_data(symbol, all_tfs)

                    result = await analyze_pair(symbol, data, mode, settings=self.settings)

                    if result["type"] == "signal":
                        if self._is_duplicate_signal(key, result):
                            continue

                        if self._has_active_position(symbol):
                            logger.debug(f"[{self.name}] Signal {symbol} ignore: position deja ouverte")
                            continue

                        if self._has_recent_signal(symbol):
                            logger.debug(f"[{self.name}] Signal {symbol} ignore: cooldown anti flip-flop")
                            continue

                        # Ajouter bot_version au signal
                        result["bot_version"] = self.name

                        signal_id = await insert_signal(result)
                        result["id"] = signal_id
                        self.last_signals[key] = result
                        self._signal_timestamps[symbol] = datetime.utcnow()

                        logger.info(
                            f"[{self.name}] SIGNAL {result['direction'].upper()} {symbol} [{mode}] "
                            f"score={result['score']} entry={result['entry_price']}"
                        )

                        # AUTO-EXECUTE en paper trading
                        try:
                            if self._paper_trader:
                                executed = await self._paper_trader.auto_execute(result)
                                if executed:
                                    from app.database import update_signal_status
                                    await update_signal_status(signal_id, "executed")
                                    logger.info(f"[{self.name}] AUTO-TRADE: {result['direction'].upper()} {symbol} execute")
                        except Exception as e:
                            logger.error(f"[{self.name}] Erreur auto-execute: {e}")

                    else:
                        await log_tradeability(
                            symbol,
                            result.get("tradeability_score", 0),
                            False,
                            {"reason": result.get("reason", ""), "details": result.get("details", [])},
                            bot_version=self.name,
                        )

                except Exception as e:
                    logger.error(f"[{self.name}] Erreur analyse {symbol} {mode}: {e}", exc_info=True)

                await asyncio.sleep(1)

    @property
    def _paper_trader(self):
        """Lazy access au paper_trader associe."""
        return getattr(self, '_paper_trader_ref', None)

    def set_paper_trader(self, pt):
        """Associe un paper_trader a ce scanner."""
        self._paper_trader_ref = pt

    def _has_active_position(self, symbol: str) -> bool:
        pm = getattr(self, '_position_monitor_ref', None)
        if not pm:
            return False
        return any(
            p["symbol"] == symbol and p.get("state") != "closed"
            for p in pm._positions.values()
        )

    def set_position_monitor(self, pm):
        """Associe un position_monitor a ce scanner."""
        self._position_monitor_ref = pm

    def _has_recent_signal(self, symbol: str) -> bool:
        last = self._signal_timestamps.get(symbol)
        if not last:
            return False
        return (datetime.utcnow() - last).total_seconds() < 45

    def _is_duplicate_signal(self, key: str, new_signal: dict) -> bool:
        if key not in self.last_signals:
            return False
        last = self.last_signals[key]
        if (
            last["direction"] == new_signal["direction"]
            and last["setup_type"] == new_signal["setup_type"]
            and abs(last["entry_price"] - new_signal["entry_price"]) / new_signal["entry_price"] < 0.002
        ):
            return True
        return False

    def set_cooldown(self, symbol: str, mode: str, seconds: int):
        key = f"{symbol}_{mode}"
        self.cooldowns[key] = datetime.utcnow() + timedelta(seconds=seconds)

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "bot_version": self.name,
            "pairs": get_enabled_pairs(self.settings),
            "modes": self.settings["scanner"]["modes"],
            "active_signals": len(self.last_signals),
            "cooldowns": {k: v.isoformat() for k, v in self.cooldowns.items()},
        }
