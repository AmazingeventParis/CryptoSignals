"""
Paper Trader : Execute automatiquement chaque signal en simulation.
Portefeuille fictif avec suivi P&L en temps reel via les vrais prix MEXC.
"""
import logging
from app.database import (
    get_paper_portfolio,
    reserve_paper_margin,
    update_paper_balance,
    init_paper_portfolio,
)
from app.core.position_monitor import position_monitor

logger = logging.getLogger(__name__)

FIXED_MARGIN = 10.0  # 10$ fixe par trade
MAX_OPEN = 5         # Max 5 positions simultanées


class PaperTrader:
    def __init__(self):
        self._open_positions: dict[int, float] = {}  # pos_id -> margin

    async def start(self):
        """Initialise le portefeuille paper et enregistre le callback."""
        await init_paper_portfolio(100.0)
        position_monitor.add_on_close_callback(self._on_position_closed)
        portfolio = await get_paper_portfolio()
        logger.info(
            f"PaperTrader demarre - Balance: ${portfolio['current_balance']:.2f} "
            f"({portfolio['total_trades']} trades, P&L: ${portfolio['total_pnl']:.2f})"
        )

    async def auto_execute(self, signal: dict) -> bool:
        """Execute automatiquement un signal en paper trading."""
        if signal.get("type") != "signal" or signal.get("direction") == "none":
            return False

        # Verifier le nombre de positions ouvertes
        if len(self._open_positions) >= MAX_OPEN:
            logger.debug(f"Paper: max positions ({MAX_OPEN}) atteint, skip {signal['symbol']}")
            return False

        # Verifier qu'on n'a pas deja une position sur ce symbol/direction
        for pos_id, _ in self._open_positions.items():
            cached = position_monitor._positions.get(pos_id, {})
            if cached.get("symbol") == signal["symbol"] and cached.get("direction") == signal["direction"]:
                logger.debug(f"Paper: deja une position {signal['symbol']} {signal['direction']}")
                return False

        # Marge fixe 10$
        portfolio = await get_paper_portfolio()
        available = portfolio["current_balance"] - portfolio["reserved_margin"]
        margin = FIXED_MARGIN

        if margin > available:
            logger.warning(f"Paper: solde insuffisant ({available:.2f}$ dispo, {margin:.2f}$ requis)")
            return False

        leverage = signal.get("leverage", 10)
        entry_price = signal["entry_price"]
        position_size_usd = margin * leverage
        quantity = round(position_size_usd / entry_price, 6)

        if quantity <= 0:
            return False

        # Construire le fake result (compatible avec position_monitor.register_trade)
        fake_result = {
            "success": True,
            "order_type": "market",
            "entry_order_id": None,
            "actual_entry_price": entry_price,
            "sl_order_id": None,
            "tp_order_ids": [None, None, None],
            "quantity": quantity,
            "position_size_usd": round(position_size_usd, 2),
            "margin_required": round(margin, 2),
            "balance": round(available - margin, 2),
        }

        # Enregistrer dans le position_monitor pour suivi temps reel
        pos_id = await position_monitor.register_trade(signal, fake_result)
        if pos_id is None:
            return False

        # Reserver la marge
        await reserve_paper_margin(margin)
        self._open_positions[pos_id] = margin

        logger.info(
            f"PAPER TRADE: {signal['direction'].upper()} {signal['symbol']} "
            f"qty={quantity} marge={margin:.2f}$ pos={position_size_usd:.2f}$ "
            f"(balance: {available - margin:.2f}$)"
        )
        return True

    async def _on_position_closed(self, pos_id: int, pnl_usd: float):
        """Callback quand le position_monitor ferme une position."""
        margin = self._open_positions.pop(pos_id, None)
        if margin is None:
            return  # Pas une position paper

        is_win = pnl_usd > 0
        await update_paper_balance(pnl_usd, is_win, margin)

        portfolio = await get_paper_portfolio()
        sign = "+" if pnl_usd >= 0 else ""
        logger.info(
            f"PAPER CLOSE: PnL {sign}{pnl_usd:.2f}$ | "
            f"Balance: ${portfolio['current_balance']:.2f} | "
            f"Win/Loss: {portfolio['wins']}/{portfolio['losses']}"
        )


# Singleton
paper_trader = PaperTrader()
