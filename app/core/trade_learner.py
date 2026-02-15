"""
Trade Learner : Niveau 1 d'apprentissage.
Suit le win rate par combo (setup_type, symbol, mode).
Desactive automatiquement les combos perdantes.
"""
import logging
from app.database import (
    update_setup_performance,
    set_setup_disabled,
    get_disabled_setups,
    get_all_setup_performance,
)

logger = logging.getLogger(__name__)

MIN_TRADES = 5          # Minimum de trades avant de juger
DISABLE_WINRATE = 0.30  # Win rate < 30% apres 5+ trades → desactiver
REENABLE_WINRATE = 0.40 # Win rate > 40% → reactiver


class TradeLearner:
    async def record_trade(self, setup_type: str, symbol: str, mode: str, is_win: bool, pnl: float):
        """Enregistre un trade et verifie si la combo doit etre desactivee/reactivee."""
        if not setup_type or setup_type == "unknown":
            return

        await update_setup_performance(setup_type, symbol, mode, is_win, pnl)

        # Verifier les seuils
        stats = await get_all_setup_performance()
        combo = next(
            (s for s in stats if s["setup_type"] == setup_type and s["symbol"] == symbol and s["mode"] == mode),
            None,
        )
        if not combo or combo["total_trades"] < MIN_TRADES:
            return

        win_rate = combo["wins"] / combo["total_trades"]

        if win_rate < DISABLE_WINRATE and not combo["disabled"]:
            await set_setup_disabled(setup_type, symbol, mode, True)
            logger.warning(
                f"LEARNING: {setup_type} sur {symbol} ({mode}) DESACTIVE "
                f"- win rate {win_rate:.0%} ({combo['wins']}/{combo['total_trades']})"
            )
        elif win_rate >= REENABLE_WINRATE and combo["disabled"]:
            await set_setup_disabled(setup_type, symbol, mode, False)
            logger.info(
                f"LEARNING: {setup_type} sur {symbol} ({mode}) REACTIVE "
                f"- win rate {win_rate:.0%} ({combo['wins']}/{combo['total_trades']})"
            )

    async def filter_setups(self, allowed_setups: list[str], symbol: str, mode: str) -> list[str]:
        """Filtre les setups desactives par l'apprentissage."""
        disabled = await get_disabled_setups(symbol, mode)
        if not disabled:
            return allowed_setups

        filtered = [s for s in allowed_setups if s not in disabled]
        if len(filtered) < len(allowed_setups):
            skipped = [s for s in allowed_setups if s in disabled]
            logger.debug(f"LEARNING: {symbol} ({mode}) - skip {skipped}")
        return filtered

    async def get_all_stats(self) -> list[dict]:
        """Retourne toutes les stats pour l'API."""
        stats = await get_all_setup_performance()
        for s in stats:
            s["win_rate"] = round(s["wins"] / max(s["total_trades"], 1) * 100, 1)
            s["avg_pnl"] = round(s["total_pnl"] / max(s["total_trades"], 1), 4)
        return stats


# Singleton
trade_learner = TradeLearner()
