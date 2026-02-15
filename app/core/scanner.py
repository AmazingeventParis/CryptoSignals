"""
Scanner : Worker qui tourne en boucle et analyse toutes les paires.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from app.config import SETTINGS, get_enabled_pairs, get_mode_config
from app.core.market_data import market_data
from app.core.signal_engine import analyze_pair
from app.database import insert_signal, log_tradeability
from app.services.telegram_bot import send_signal, send_no_trade_summary
from app.core.paper_trader import paper_trader

logger = logging.getLogger(__name__)


class Scanner:
    def __init__(self):
        self.running = False
        self.last_signals: dict[str, dict] = {}
        self.cooldowns: dict[str, datetime] = {}
        self.consecutive_losses: dict[str, int] = {}

    async def start(self):
        self.running = True
        interval = SETTINGS["scanner"]["interval_seconds"]
        logger.info(f"Scanner demarre - intervalle {interval}s - {len(get_enabled_pairs())} paires")

        while self.running:
            try:
                await self._scan_cycle()
            except Exception as e:
                logger.error(f"Erreur cycle scan: {e}", exc_info=True)
            await asyncio.sleep(interval)

    async def stop(self):
        self.running = False
        logger.info("Scanner arrete")

    async def _scan_cycle(self):
        # Retenter la connexion si pas connecte
        if not market_data.is_connected():
            logger.info("Tentative de reconnexion MEXC...")
            await market_data.connect()
            if not market_data.is_connected():
                logger.warning("MEXC toujours indisponible, retry dans 30s")
                return

        pairs = get_enabled_pairs()
        modes = SETTINGS["scanner"]["modes"]

        for symbol in pairs:
            for mode in modes:
                try:
                    # Verifier cooldown
                    key = f"{symbol}_{mode}"
                    if key in self.cooldowns:
                        if datetime.utcnow() < self.cooldowns[key]:
                            continue

                    # Recuperer les timeframes necessaires
                    mode_cfg = get_mode_config(mode)
                    all_tfs = mode_cfg["timeframes"]["analysis"] + [mode_cfg["timeframes"]["filter"]]
                    all_tfs = list(set(all_tfs))

                    # Fetch data
                    data = await market_data.fetch_all_data(symbol, all_tfs)

                    # Analyse
                    result = await analyze_pair(symbol, data, mode)

                    if result["type"] == "signal":
                        # Eviter de spammer le meme signal
                        if self._is_duplicate_signal(key, result):
                            continue

                        # Sauvegarder
                        signal_id = await insert_signal(result)
                        result["id"] = signal_id
                        self.last_signals[key] = result

                        # Envoyer sur Telegram
                        await send_signal(result)
                        logger.info(
                            f"SIGNAL {result['direction'].upper()} {symbol} [{mode}] "
                            f"score={result['score']} entry={result['entry_price']}"
                        )

                        # Auto-execute en paper trading
                        try:
                            await paper_trader.auto_execute(result)
                        except Exception as pe:
                            logger.error(f"Paper trade erreur: {pe}")

                    else:
                        # Log tradeability
                        await log_tradeability(
                            symbol,
                            result.get("tradeability_score", 0),
                            False,
                            {"reason": result.get("reason", ""), "details": result.get("details", [])},
                        )

                except Exception as e:
                    logger.error(f"Erreur analyse {symbol} {mode}: {e}", exc_info=True)

                # Petit delai entre chaque paire pour eviter rate limit
                await asyncio.sleep(1)

    def _is_duplicate_signal(self, key: str, new_signal: dict) -> bool:
        if key not in self.last_signals:
            return False
        last = self.last_signals[key]
        # Meme direction et meme setup dans les 5 dernieres minutes
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
            "pairs": get_enabled_pairs(),
            "modes": SETTINGS["scanner"]["modes"],
            "active_signals": len(self.last_signals),
            "cooldowns": {k: v.isoformat() for k, v in self.cooldowns.items()},
        }


# Singleton
scanner = Scanner()
